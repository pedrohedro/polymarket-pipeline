from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

import config


class AIProviderError(RuntimeError):
    """Raised when the selected AI provider cannot complete a request."""


@dataclass(frozen=True)
class ProviderStatus:
    ok: bool
    label: str
    detail: str


def provider_label() -> str:
    """Human-readable provider/model label for logs and persisted metadata."""
    provider = config.AI_PROVIDER
    model = _model_for(provider)
    if provider == "codex":
        model = config.CODEX_MODEL or "default"
    return f"{provider}:{model}" if model else provider


def check_provider() -> ProviderStatus:
    """Check local configuration without spending model tokens."""
    provider = config.AI_PROVIDER
    supported = {"codex", "openai", "openrouter", "anthropic", "generic"}
    if provider not in supported:
        return ProviderStatus(False, provider_label(), f"unsupported AI_PROVIDER `{provider}`")

    if provider == "codex":
        binary = shutil.which(config.CODEX_BIN)
        if not binary:
            return ProviderStatus(False, provider_label(), f"Codex CLI not found: {config.CODEX_BIN}")
        try:
            result = subprocess.run(
                [config.CODEX_BIN, "login", "status"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception as exc:
            return ProviderStatus(False, provider_label(), f"Codex login check failed: {type(exc).__name__}: {exc}")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "run `codex login`").strip()
            return ProviderStatus(False, provider_label(), detail)
        return ProviderStatus(True, provider_label(), (result.stdout or "Codex authenticated").strip())

    api_key = _api_key_for(provider)
    if provider != "generic" and not api_key:
        return ProviderStatus(False, provider_label(), f"missing API key for provider `{provider}`")
    if not _model_for(provider):
        return ProviderStatus(False, provider_label(), f"missing AI_MODEL for provider `{provider}`")
    if provider == "generic" and not config.AI_BASE_URL:
        return ProviderStatus(False, provider_label(), "generic provider requires AI_BASE_URL")

    return ProviderStatus(True, provider_label(), "API key and model configured")


def complete_json(prompt: str, *, max_tokens: int | None = None, temperature: float | None = None) -> dict[str, Any]:
    text = complete_text(prompt, max_tokens=max_tokens, temperature=temperature)
    return extract_json(text)


def complete_text(prompt: str, *, max_tokens: int | None = None, temperature: float | None = None) -> str:
    provider = config.AI_PROVIDER
    max_tokens = max_tokens if max_tokens is not None else config.AI_MAX_TOKENS
    temperature = temperature if temperature is not None else config.AI_TEMPERATURE

    if provider == "codex":
        return _complete_codex(prompt)
    if provider in ("openai", "openrouter", "generic"):
        return _complete_openai_compatible(provider, prompt, max_tokens, temperature)
    if provider == "anthropic":
        return _complete_anthropic(prompt, max_tokens, temperature)

    raise AIProviderError(
        f"Unsupported AI_PROVIDER `{provider}`. Use codex, openai, openrouter, anthropic, or generic."
    )


def extract_json(text: str) -> dict[str, Any]:
    """Extract one JSON object from provider output."""
    cleaned = text.strip()
    if "```" in cleaned:
        blocks = cleaned.split("```")
        for block in blocks:
            candidate = block.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise AIProviderError(f"AI response did not contain JSON: {cleaned[:300]}")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise AIProviderError("AI response JSON must be an object")
    return parsed


def _complete_codex(prompt: str) -> str:
    output_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            output_path = handle.name

        cmd = [
            config.CODEX_BIN,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-o",
            output_path,
        ]
        if config.CODEX_MODEL:
            cmd.extend(["--model", config.CODEX_MODEL])
        if config.CODEX_PROFILE:
            cmd.extend(["--profile", config.CODEX_PROFILE])
        cmd.append(prompt)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=config.CODEX_TIMEOUT,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no output").strip()
            raise AIProviderError(f"Codex CLI failed: {detail}")

        text = Path(output_path).read_text(encoding="utf-8").strip()
        return text or result.stdout.strip()
    except subprocess.TimeoutExpired as exc:
        raise AIProviderError(f"Codex CLI timed out after {config.CODEX_TIMEOUT}s") from exc
    finally:
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass


def _complete_openai_compatible(provider: str, prompt: str, max_tokens: int, temperature: float) -> str:
    base_url = _base_url_for(provider)
    api_key = _api_key_for(provider)
    model = _model_for(provider)
    if provider != "generic" and not api_key:
        raise AIProviderError(f"Missing API key for provider `{provider}`")
    if not model:
        raise AIProviderError(f"Missing AI_MODEL for provider `{provider}`")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        if config.OPENROUTER_SITE_URL:
            headers["HTTP-Referer"] = config.OPENROUTER_SITE_URL
        if config.OPENROUTER_APP_NAME:
            headers["X-Title"] = config.OPENROUTER_APP_NAME

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    with httpx.Client(timeout=config.AI_TIMEOUT) as client:
        response = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError(f"Unexpected OpenAI-compatible response: {data}") from exc


def _complete_anthropic(prompt: str, max_tokens: int, temperature: float) -> str:
    api_key = _api_key_for("anthropic")
    model = _model_for("anthropic")
    if not api_key:
        raise AIProviderError("Missing ANTHROPIC_API_KEY or AI_API_KEY")
    if not model:
        raise AIProviderError("Missing AI_MODEL or ANTHROPIC_MODEL for anthropic provider")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }

    with httpx.Client(timeout=config.AI_TIMEOUT) as client:
        response = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    try:
        return data["content"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError(f"Unexpected Anthropic response: {data}") from exc


def _base_url_for(provider: str) -> str:
    if provider == "openrouter":
        return config.AI_BASE_URL or "https://openrouter.ai/api/v1"
    if provider == "openai":
        return config.AI_BASE_URL or "https://api.openai.com/v1"
    return config.AI_BASE_URL.rstrip("/")


def _api_key_for(provider: str) -> str:
    if provider == "openrouter":
        return config.OPENROUTER_API_KEY or config.AI_API_KEY
    if provider == "openai":
        return config.OPENAI_API_KEY or config.AI_API_KEY
    if provider == "anthropic":
        return config.ANTHROPIC_API_KEY or config.AI_API_KEY
    return config.AI_API_KEY


def _model_for(provider: str) -> str:
    if provider == "openrouter":
        return config.OPENROUTER_MODEL or config.AI_MODEL
    if provider == "openai":
        return config.OPENAI_MODEL or config.AI_MODEL
    if provider == "anthropic":
        return config.ANTHROPIC_MODEL or config.AI_MODEL
    if provider == "codex":
        return config.CODEX_MODEL or config.AI_MODEL
    return config.AI_MODEL
