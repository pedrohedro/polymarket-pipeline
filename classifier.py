"""
Pluggable classification engine — replaces probability estimation with direction
classification. Asks "does this news confirm or deny the market question?" instead
of "what's the probability?".

Engines (selected via config.CLASSIFIER_ENGINE):
  - "anthropic": Claude via the Anthropic API   (requires ANTHROPIC_API_KEY)
  - "openai":    GPT via the OpenAI API          (requires OPENAI_API_KEY)
  - "local":     offline heuristic, no API key   (see local_classifier.py)
  - "auto":      anthropic if a key is set, else local
"""
from __future__ import annotations

import os
import json
import time
import shutil
import logging
import tempfile
import subprocess
from dataclasses import dataclass

import config
import local_classifier
from markets import Market

log = logging.getLogger(__name__)

_PLACEHOLDER_KEYS = {"", "sk-ant-...", "sk-..."}

# Cached result of the (relatively slow) Codex CLI availability probe.
_codex_available_cache: bool | None = None


def _has_key(key: str) -> bool:
    return bool(key) and key not in _PLACEHOLDER_KEYS


def codex_available() -> bool:
    """True if the Codex CLI is installed AND authenticated (via `codex login`)."""
    global _codex_available_cache
    if _codex_available_cache is None:
        if shutil.which(config.CODEX_BIN) is None:
            _codex_available_cache = False
        else:
            try:
                r = subprocess.run(
                    [config.CODEX_BIN, "login", "status"],
                    capture_output=True, timeout=15,
                )
                _codex_available_cache = r.returncode == 0
            except Exception:
                _codex_available_cache = False
    return _codex_available_cache


def resolve_engine() -> str:
    """Resolve the effective engine name, honoring 'auto' and missing credentials."""
    engine = config.CLASSIFIER_ENGINE
    if engine == "auto":
        if _has_key(config.ANTHROPIC_API_KEY):
            return "anthropic"
        if _has_key(config.OPENAI_API_KEY):
            return "openai"
        return "local"
    # Explicit engines gracefully fall back to local if their credentials are missing.
    if engine == "anthropic" and not _has_key(config.ANTHROPIC_API_KEY):
        log.warning("[classifier] CLASSIFIER_ENGINE=anthropic but no API key; using local engine.")
        return "local"
    if engine == "openai" and not _has_key(config.OPENAI_API_KEY):
        log.warning("[classifier] CLASSIFIER_ENGINE=openai but no API key; using local engine.")
        return "local"
    if engine == "codex" and not codex_available():
        log.warning(
            "[classifier] CLASSIFIER_ENGINE=codex but Codex CLI is missing or not logged in "
            "(run `codex login`); using local engine."
        )
        return "local"
    if engine not in ("anthropic", "openai", "codex", "local"):
        log.warning(f"[classifier] Unknown CLASSIFIER_ENGINE '{engine}'; using local engine.")
        return "local"
    return engine


# Lazily-created API clients (only built when actually needed).
_anthropic_client = None
_openai_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


CLASSIFICATION_PROMPT = """You are a news classifier for prediction markets.

## Market Question
{question}

## Current Market Price
YES: {yes_price:.2f} (implied probability: {yes_price:.0%})

## Breaking News
{headline}
Source: {source}

## Task
Does this news make the market question MORE likely to resolve YES, MORE likely to resolve NO, or is it NOT RELEVANT?

Also rate the MATERIALITY — how much should this move the price? 0.0 means no impact, 1.0 means this is definitive evidence.

Respond with ONLY valid JSON:
{{
  "direction": "bullish" | "bearish" | "neutral",
  "materiality": <float 0.0 to 1.0>,
  "reasoning": "<1 sentence>"
}}"""


@dataclass
class Classification:
    direction: str  # "bullish", "bearish", "neutral"
    materiality: float  # 0.0-1.0
    reasoning: str
    latency_ms: int
    model: str


def _extract_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def _normalize(result: dict, model: str, start: float) -> Classification:
    latency = int((time.time() - start) * 1000)
    direction = result.get("direction", "neutral")
    if direction not in ("bullish", "bearish", "neutral"):
        direction = "neutral"
    materiality = max(0.0, min(1.0, float(result.get("materiality", 0))))
    return Classification(
        direction=direction,
        materiality=materiality,
        reasoning=result.get("reasoning", ""),
        latency_ms=latency,
        model=model,
    )


def _classify_anthropic(prompt: str, start: float) -> Classification:
    response = _get_anthropic().messages.create(
        model=config.CLASSIFICATION_MODEL,
        max_tokens=200,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    result = _extract_json(response.content[0].text)
    return _normalize(result, config.CLASSIFICATION_MODEL, start)


def _classify_openai(prompt: str, start: float) -> Classification:
    response = _get_openai().chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=200,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    result = _extract_json(response.choices[0].message.content)
    return _normalize(result, config.OPENAI_MODEL, start)


def _classify_codex(prompt: str, start: float) -> Classification:
    """Classify via the Codex CLI (ChatGPT OAuth subscription, no API key).

    Runs `codex exec` non-interactively in a read-only sandbox and reads the
    agent's final message (the JSON answer) from a temp file.
    """
    fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        cmd = [
            config.CODEX_BIN, "exec",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color", "never",
            "-o", out_path,
        ]
        if config.CODEX_MODEL:
            cmd += ["-m", config.CODEX_MODEL]
        cmd.append(prompt + "\n\nReturn ONLY the raw JSON object, no prose, no code fences.")

        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.CODEX_TIMEOUT,
        )
        with open(out_path, "r") as fh:
            text = fh.read().strip()
        if not text:
            # Fall back to stdout if the last-message file is empty.
            text = proc.stdout.strip()
        if not text:
            raise RuntimeError(f"empty Codex output (rc={proc.returncode}): {proc.stderr[-300:]}")

        result = _extract_json(text)
        model = f"codex:{config.CODEX_MODEL}" if config.CODEX_MODEL else "codex"
        return _normalize(result, model, start)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _classify_local(headline: str, market: Market, start: float) -> Classification:
    result = local_classifier.classify(headline, market.question, market.yes_price)
    return _normalize(result, "local-heuristic", start)


def classify(headline: str, market: Market, source: str = "unknown") -> Classification:
    """Classify a news headline against a market question. Synchronous."""
    start = time.time()
    engine = resolve_engine()

    try:
        if engine == "local":
            return _classify_local(headline, market, start)

        prompt = CLASSIFICATION_PROMPT.format(
            question=market.question,
            yes_price=market.yes_price,
            headline=headline,
            source=source,
        )
        if engine == "anthropic":
            return _classify_anthropic(prompt, start)
        if engine == "openai":
            return _classify_openai(prompt, start)
        if engine == "codex":
            return _classify_codex(prompt, start)
        # Should not happen — resolve_engine guarantees a valid value.
        return _classify_local(headline, market, start)

    except Exception as e:
        latency = int((time.time() - start) * 1000)
        log.warning(f"[classifier] {engine} error: {e} — falling back to local engine")
        try:
            return _classify_local(headline, market, start)
        except Exception:
            return Classification(
                direction="neutral",
                materiality=0.0,
                reasoning=f"Classification error: {type(e).__name__}",
                latency_ms=latency,
                model=engine,
            )


async def classify_async(headline: str, market: Market, source: str = "unknown") -> Classification:
    """Async wrapper around classify()."""
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, classify, headline, market, source
    )


if __name__ == "__main__":
    test_market = Market(
        condition_id="test",
        question="Will OpenAI release GPT-5 before August 2026?",
        category="ai",
        yes_price=0.62,
        no_price=0.38,
        volume=500000,
        end_date="2026-08-01",
        active=True,
        tokens=[],
    )

    print(f"Engine: {resolve_engine()}\n")
    result = classify(
        headline="OpenAI reportedly testing GPT-5 internally with select partners",
        market=test_market,
        source="The Information",
    )
    print(f"Direction: {result.direction}")
    print(f"Materiality: {result.materiality}")
    print(f"Reasoning: {result.reasoning}")
    print(f"Latency: {result.latency_ms}ms")
    print(f"Model: {result.model}")
