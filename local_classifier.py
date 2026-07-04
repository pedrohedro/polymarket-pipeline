"""
Local, offline classification engine.

A deterministic, dependency-free heuristic that mirrors the Claude classifier's
contract: given a breaking headline and a market question, it decides whether the
news makes the market MORE likely to resolve YES (bullish), MORE likely to resolve
NO (bearish), or is NOT RELEVANT (neutral), plus a materiality score in [0, 1].

It requires no API key and no network access, so it is ideal for local development,
testing, and backtesting without spending on a paid LLM.

The heuristic works in two steps:
  1. Relevance — keyword overlap between the market question and the headline.
     No overlap => neutral / zero materiality.
  2. Direction — a small affirmative/negative lexicon estimates whether the news
     pushes the underlying event toward happening (bullish) or away (bearish).

This is intentionally simple. It is NOT as accurate as an LLM, but it lets the whole
pipeline run end to end and produces sane, explainable signals.
"""
from __future__ import annotations

# Words suggesting the event in question is MORE likely to happen / be true.
AFFIRMATIVE = {
    "announce", "announces", "announced", "launch", "launches", "launched",
    "release", "releases", "released", "confirm", "confirms", "confirmed",
    "approve", "approves", "approved", "win", "wins", "won", "beat", "beats",
    "surge", "surges", "surged", "soar", "soars", "rise", "rises", "rose",
    "jump", "jumps", "jumped", "record", "records", "success", "successful",
    "agree", "agrees", "agreed", "deal", "signs", "signed", "pass", "passes",
    "passed", "reach", "reaches", "reached", "hit", "hits", "unveil", "unveils",
    "unveiled", "gain", "gains", "gained", "advance", "advances", "boost",
    "boosts", "wins", "lead", "leads", "leading", "ahead", "up", "grow",
    "grows", "growth", "expand", "expands", "ships", "shipped", "greenlight",
}

# Words suggesting the event is LESS likely to happen / be true.
NEGATIVE = {
    "deny", "denies", "denied", "delay", "delays", "delayed", "cancel",
    "cancels", "cancelled", "canceled", "fail", "fails", "failed", "drop",
    "drops", "dropped", "reject", "rejects", "rejected", "postpone",
    "postpones", "postponed", "lose", "loses", "lost", "fall", "falls",
    "fell", "plunge", "plunges", "plunged", "crash", "crashes", "crashed",
    "sink", "sinks", "block", "blocks", "blocked", "halt", "halts", "halted",
    "ban", "bans", "banned", "sue", "sues", "sued", "lawsuit", "recall",
    "recalls", "scrap", "scraps", "scrapped", "abandon", "abandons",
    "withdraw", "withdraws", "withdrawn", "down", "decline", "declines",
    "shut", "shutdown", "loss", "losses", "miss", "misses", "missed",
    "slump", "slumps", "weak", "weaker", "behind", "collapse", "collapses",
    "quit", "resign", "resigns", "resigned", "oppose", "opposes",
}

# Negators that flip the polarity of a nearby sentiment word.
NEGATORS = {"not", "no", "never", "without", "wont", "won't", "cannot", "can't", "isn't", "won"}

STOPWORDS = {
    "will", "the", "a", "an", "be", "by", "in", "on", "at", "to", "of", "for",
    "is", "it", "this", "that", "and", "or", "not", "before", "after", "end",
    "yes", "no", "any", "has", "have", "does", "do", "than", "more", "less",
    "over", "under", "with", "from", "as", "are", "was", "were", "how", "much",
    "many", "who", "what", "when", "where", "which", "price",
}


def _tokens(text: str) -> list[str]:
    cleaned = []
    for w in text.lower().replace("’", "'").split():
        w = w.strip("?.,!\"'()[]:;")
        if w:
            cleaned.append(w)
    return cleaned


def _keywords(question: str) -> set[str]:
    return {w for w in _tokens(question) if w not in STOPWORDS and len(w) > 2}


def classify(headline: str, question: str, yes_price: float) -> dict:
    """Return {'direction', 'materiality', 'reasoning'} for the given inputs.

    Pure function — no side effects, no network, fully deterministic.
    """
    q_keywords = _keywords(question)
    h_tokens = _tokens(headline)
    h_token_set = set(h_tokens)

    if not q_keywords:
        return {
            "direction": "neutral",
            "materiality": 0.0,
            "reasoning": "No usable keywords in market question.",
        }

    overlap = q_keywords & h_token_set
    relevance = len(overlap) / len(q_keywords)

    if not overlap:
        return {
            "direction": "neutral",
            "materiality": 0.0,
            "reasoning": "Headline shares no keywords with the market question.",
        }

    # Sentiment scan with simple negation handling.
    score = 0
    for i, tok in enumerate(h_tokens):
        polarity = 0
        if tok in AFFIRMATIVE:
            polarity = 1
        elif tok in NEGATIVE:
            polarity = -1
        if polarity == 0:
            continue
        # Flip polarity if a negator sits within the previous two tokens.
        window = h_tokens[max(0, i - 2):i]
        if any(w in NEGATORS for w in window):
            polarity *= -1
        score += polarity

    if score > 0:
        direction = "bullish"
    elif score < 0:
        direction = "bearish"
    else:
        direction = "neutral"

    # Materiality scales with relevance and the strength of the sentiment signal.
    sentiment_strength = min(1.0, abs(score) / 2.0)
    if direction == "neutral":
        materiality = round(min(0.4, relevance * 0.4), 2)
    else:
        materiality = round(min(1.0, 0.35 * relevance + 0.65 * sentiment_strength * relevance + 0.2), 2)

    matched = ", ".join(sorted(overlap)[:4]) or "—"
    reasoning = (
        f"Heuristic: {int(relevance * 100)}% keyword overlap ({matched}); "
        f"sentiment score {score:+d} -> {direction}."
    )

    return {
        "direction": direction,
        "materiality": materiality,
        "reasoning": reasoning,
    }


if __name__ == "__main__":
    tests = [
        ("OpenAI officially launches GPT-5 for all users", "Will OpenAI release GPT-5 before August 2026?", 0.62),
        ("OpenAI delays GPT-5 launch indefinitely amid safety review", "Will OpenAI release GPT-5 before August 2026?", 0.62),
        ("NASA announces new Mars rover mission", "Will OpenAI release GPT-5 before August 2026?", 0.62),
        ("Bitcoin surges past $70,000 in record rally", "Will the price of Bitcoin be above $64,000 on July?", 0.10),
    ]
    for headline, question, price in tests:
        r = classify(headline, question, price)
        print(f"\nQ: {question}\nH: {headline}")
        print(f"  -> {r['direction']} (materiality {r['materiality']}) | {r['reasoning']}")
