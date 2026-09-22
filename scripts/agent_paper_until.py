#!/usr/bin/env python3
"""
Paper (dry-run) loop with agent motor until mark-to-market PnL >= TARGET_USD.

Safety: refuses to run unless config.DRY_RUN is True. Never places live orders.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import config
from classifier import Classification
from edge import detect_edge_v2
from executor import execute_trade
from markets import Market
from matcher import match_news_to_markets
from news_stream import NewsEvent
from scraper import scrape_all
import logger

TARGET_USD = 15.0
POLL_SECONDS = 60
STATE_PATH = Path("/tmp/agent_pipeline/paper_state.json")
LOG_PATH = Path("/tmp/agent_pipeline/paper_run.log")
STATUS_PATH = Path("/tmp/agent_pipeline/status.txt")
GAMMA = "https://gamma-api.polymarket.com"
MAX_OPEN_POSITIONS = 8


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_status(total: float, state: dict) -> None:
    STATUS_PATH.write_text(
        f"total_pnl={total:.2f}\ntarget={TARGET_USD:.2f}\n"
        f"realized={state['realized_pnl']:.2f}\nopen={len(state['positions'])}\n"
        f"trades={state['trades']}\ncycles={state['cycles']}\n"
        f"updated={datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )


def parse_prices(m: dict) -> tuple[float, float]:
    outcome_prices = m.get("outcomePrices", "")
    yes_price, no_price = 0.5, 0.5
    if outcome_prices:
        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        if len(prices) >= 2:
            yes_price, no_price = float(prices[0]), float(prices[1])
    return yes_price, no_price


def parse_tokens(m: dict) -> list[dict]:
    tokens = m.get("tokens") or []
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            tokens = []
    if tokens:
        return tokens
    clob = m.get("clobTokenIds", "")
    if isinstance(clob, str) and clob:
        try:
            ids = json.loads(clob)
        except json.JSONDecodeError:
            ids = []
        return [{"token_id": tid, "outcome": side} for tid, side in zip(ids, ["Yes", "No"])]
    return []


def market_from_gamma(m: dict, category: str = "other") -> Market:
    yes, no = parse_prices(m)
    return Market(
        condition_id=m.get("conditionId") or m.get("condition_id") or "",
        question=m.get("question") or m.get("groupItemTitle") or "",
        category=category,
        yes_price=yes,
        no_price=no,
        volume=float(m.get("volume") or 0),
        end_date=str(m.get("endDate") or ""),
        active=bool(m.get("active", True)),
        tokens=parse_tokens(m),
    )


def search_markets(queries: list[str]) -> list[Market]:
    found: dict[str, Market] = {}
    for q in queries:
        try:
            r = httpx.get(f"{GAMMA}/public-search", params={"q": q}, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            log(f"search error [{q}]: {exc}")
            continue
        for event in r.json().get("events", [])[:8]:
            title = (event.get("title") or "").lower()
            cat = "ai" if any(k in title for k in ("ai", "anthropic", "openai", "model", "claude")) else (
                "politics" if "trump" in title else "other"
            )
            for m in event.get("markets") or []:
                market = market_from_gamma(m, category=cat)
                if not market.condition_id or not market.question:
                    continue
                if market.volume < 500 or market.volume > 2_000_000:
                    continue
                if market.yes_price <= 0.01 or market.yes_price >= 0.99:
                    continue
                ql = market.question.lower()
                if "energy and innovation summit" in ql or "july 15" in ql:
                    continue
                found[market.condition_id] = market
    return list(found.values())


def fetch_yes_price(condition_id: str) -> float | None:
    try:
        resp = httpx.get(
            f"{GAMMA}/markets",
            params={"condition_ids": condition_id},
            timeout=15,
        )
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", [])
        if not items:
            # fallback: search by id field variants
            resp = httpx.get(f"{GAMMA}/markets/{condition_id}", timeout=15)
            if resp.status_code == 200:
                items = [resp.json()]
        if not items:
            return None
        yes, _ = parse_prices(items[0])
        return yes
    except Exception:
        return None


def agent_classify(headline: str, question: str) -> Classification:
    h = headline.lower()
    q = question.lower()

    if "renames ai" in q or ("rename" in q and "ai" in q and "trump" in q):
        strong = any(
            x in h
            for x in (
                "renames ai",
                "renaming ai",
                "rename ai",
                "super intelligence",
                "orders all us agencies to refer to ai",
                "replaces ai with",
                "rebrand artificial",
                "refer to ai as",
            )
        )
        if "trump" in h and "super intelligence" in h:
            strong = True
        weak = "trump" in h and ("ai" in h or "artificial intelligence" in h) and any(
            x in h for x in ("rename", "rebrand", "call", "called", "something else")
        )
        if strong:
            return Classification(
                direction="bullish",
                materiality=0.88,
                reasoning="Trump renaming/rebranding AI supports YES on rename market.",
                latency_ms=1,
                model="agent:composer",
            )
        if weak:
            return Classification(
                direction="bullish",
                materiality=0.65,
                reasoning="Trump discussing renaming AI — relevant.",
                latency_ms=1,
                model="agent:composer",
            )
        return Classification(
            direction="neutral",
            materiality=0.05,
            reasoning="Not specific official AI rename evidence.",
            latency_ms=1,
            model="agent:composer",
        )

    if "ai safety executive order" in q:
        if "trump" in h and "executive order" in h and "ai" in h:
            return Classification(
                direction="bullish",
                materiality=0.7,
                reasoning="Trump AI executive-order action supports this market.",
                latency_ms=1,
                model="agent:composer",
            )
        return Classification(
            direction="neutral",
            materiality=0.15,
            reasoning="Not a federal Trump AI Safety EO.",
            latency_ms=1,
            model="agent:composer",
        )

    if "hormuz" in q or "strait of trump" in q:
        if "hormuz" in h or "strait of trump" in h:
            return Classification(
                direction="bullish",
                materiality=0.8,
                reasoning="Direct Hormuz rename news.",
                latency_ms=1,
                model="agent:composer",
            )
        return Classification(
            direction="neutral",
            materiality=0.05,
            reasoning="Unrelated rename news.",
            latency_ms=1,
            model="agent:composer",
        )

    if "anthropic" in q or "claude" in q or "best ai model" in q:
        if ("anthropic" in h or "claude" in h) and any(
            x in h for x in ("opus", "releases", "release", "model")
        ):
            if "best" in q:
                return Classification(
                    direction="bullish",
                    materiality=0.55,
                    reasoning="New Anthropic model mildly supports quality narrative.",
                    latency_ms=1,
                    model="agent:composer",
                )
            if "opus" in q and "score" in q:
                return Classification(
                    direction="neutral",
                    materiality=0.25,
                    reasoning="Opus released, but no score data for threshold markets.",
                    latency_ms=1,
                    model="agent:composer",
                )
        return Classification(
            direction="neutral",
            materiality=0.0,
            reasoning="No material Anthropic/score link.",
            latency_ms=1,
            model="agent:composer",
        )

    if "bitcoin" in q or "btc" in q:
        up = any(x in h for x in ("surges", "soars", "hits", "above", "rally", "all-time high", "etf inflow"))
        down = any(x in h for x in ("plunges", "crashes", "falls", "below", "selloff", "etf outflow"))
        if "above" in q or "hit" in q or "price" in q:
            if up:
                return Classification(
                    direction="bullish",
                    materiality=0.7,
                    reasoning="Bullish Bitcoin price action news vs upside price market.",
                    latency_ms=1,
                    model="agent:composer",
                )
            if down:
                return Classification(
                    direction="bearish",
                    materiality=0.7,
                    reasoning="Bearish Bitcoin price action news vs upside price market.",
                    latency_ms=1,
                    model="agent:composer",
                )

    if "fed" in q or "rate cut" in q or "interest rate" in q:
        if "cut" in h and "fed" in h:
            return Classification(
                direction="bullish" if "cut" in q else "neutral",
                materiality=0.6,
                reasoning="Fed cut discussion may move rate markets.",
                latency_ms=1,
                model="agent:composer",
            )

    return Classification(
        direction="neutral",
        materiality=0.0,
        reasoning="No material link.",
        latency_ms=1,
        model="agent:composer",
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {
        "positions": {},  # cid -> {side, entry, amount, shares, question, opened_at}
        "realized_pnl": 0.0,
        "cycles": 0,
        "trades": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def position_mtm(pos: dict, yes_now: float) -> float:
    """Mark-to-market PnL for a paper position sized in USD notional."""
    entry = float(pos["entry"])
    amount = float(pos["amount"])
    side = pos["side"]
    if side == "YES":
        shares = amount / max(entry, 1e-6)
        value = shares * yes_now
    else:
        # bought NO at (1-entry_yes) approx using stored entry as YES at open
        no_entry = 1.0 - entry
        shares = amount / max(no_entry, 1e-6)
        value = shares * (1.0 - yes_now)
    return value - amount


def total_pnl(state: dict, price_cache: dict[str, float]) -> tuple[float, float]:
    unreal = 0.0
    for cid, pos in state["positions"].items():
        yes = price_cache.get(cid)
        if yes is None:
            continue
        unreal += position_mtm(pos, yes)
    return float(state["realized_pnl"]), unreal


def open_or_add(state: dict, market: Market, side: str, amount: float, headline: str) -> None:
    cid = market.condition_id
    entry_yes = market.yes_price
    if cid in state["positions"]:
        # already in — skip pyramiding for paper simplicity
        log(f"skip add (already open): {market.question[:60]}")
        return

    event = NewsEvent(
        headline=headline,
        source="agent/paper",
        url="",
        published_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        latency_ms=40,
    )
    # Re-build classification was already accepted; create a passthrough signal via detect
    # by calling execute through a crafted path: use Classification we already validated.
    # Here we only log via execute_trade after detect_edge_v2 in caller.
    state["positions"][cid] = {
        "side": side,
        "entry": entry_yes,
        "amount": amount,
        "question": market.question,
        "headline": headline[:200],
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["trades"] += 1


def maybe_take_profit(state: dict, price_cache: dict[str, float]) -> None:
    """Realize paper gains on positions that are clearly up."""
    to_close = []
    for cid, pos in state["positions"].items():
        yes = price_cache.get(cid)
        if yes is None:
            continue
        pnl = position_mtm(pos, yes)
        # lock gains early so capital can rotate into new paper signals
        if pnl >= 2.0 or pnl >= 0.12 * float(pos["amount"]):
            to_close.append((cid, pnl, yes))
    for cid, pnl, yes in to_close:
        pos = state["positions"].pop(cid)
        state["realized_pnl"] += pnl
        log(
            f"TAKE PROFIT cid={cid[:10]}… pnl=${pnl:+.2f} "
            f"entry_yes={pos['entry']:.3f} now={yes:.3f} | {pos['question'][:55]}"
        )


def cycle(state: dict) -> float:
    state["cycles"] += 1
    markets = search_markets(
        [
            "Trump renames AI",
            "Trump AI Safety",
            "best AI model",
            "Anthropic",
            "OpenAI",
            "Bitcoin",
            "Ethereum",
            "Fed rate",
            "tariff",
            "NVIDIA",
        ]
    )
    news = scrape_all(lookback_hours=6)
    log(f"cycle={state['cycles']} markets={len(markets)} headlines={len(news)}")

    # refresh prices for open positions + candidates
    price_cache: dict[str, float] = {}
    for cid in list(state["positions"].keys()):
        px = fetch_yes_price(cid)
        if px is not None:
            price_cache[cid] = px
    for m in markets:
        price_cache[m.condition_id] = m.yes_price

    maybe_take_profit(state, price_cache)

    best: dict[str, tuple] = {}
    for item in news:
        matched = match_news_to_markets(item.headline, markets, max_matches=4)
        if re.search(r"renam\w*.*\bai\b|\bai\b.*renam|super intelligence", item.headline, re.I):
            for m in markets:
                ql = m.question.lower()
                if "renames ai" in ql or ("rename" in ql and "ai" in ql and "trump" in ql):
                    if m not in matched:
                        matched.append(m)
        for market in matched:
            cls = agent_classify(item.headline, market.question)
            prev = best.get(market.condition_id)
            if prev is None or cls.materiality > prev[0].materiality:
                best[market.condition_id] = (cls, item, market)

    for cid, (cls, item, market) in best.items():
        event = NewsEvent(
            headline=item.headline,
            source=f"agent/{item.source}",
            url=getattr(item, "url", "") or "",
            published_at=getattr(item, "published_at", datetime.now(timezone.utc)),
            received_at=datetime.now(timezone.utc),
            latency_ms=50,
        )
        signal = detect_edge_v2(market, cls, event)
        if not signal:
            continue
        if cid in state["positions"]:
            continue
        if len(state["positions"]) >= MAX_OPEN_POSITIONS:
            log("max open positions reached — waiting for MTM/take-profit")
            break
        result = execute_trade(signal)
        log(
            f"OPEN {result['status']} {signal.side} ${signal.bet_amount} "
            f"edge={signal.edge:.1%} mat={cls.materiality:.2f} | {market.question[:70]}"
        )
        if result["status"] == "dry_run":
            open_or_add(state, market, signal.side, signal.bet_amount, item.headline)

    # refresh MTM after opens
    for cid in list(state["positions"].keys()):
        if cid not in price_cache:
            px = fetch_yes_price(cid)
            if px is not None:
                price_cache[cid] = px
        # prefer live market object price if present
        for m in markets:
            if m.condition_id == cid:
                price_cache[cid] = m.yes_price

    realized, unreal = total_pnl(state, price_cache)
    total = realized + unreal
    log(
        f"PnL realized=${realized:+.2f} unreal=${unreal:+.2f} TOTAL=${total:+.2f} "
        f"target=${TARGET_USD:.2f} open_pos={len(state['positions'])} trades={state['trades']}"
    )
    for cid, pos in state["positions"].items():
        yes = price_cache.get(cid)
        if yes is None:
            continue
        pnl = position_mtm(pos, yes)
        log(
            f"  pos {pos['side']} entry={pos['entry']:.3f} now={yes:.3f} "
            f"pnl=${pnl:+.2f} | {pos['question'][:60]}"
        )
    save_state(state)
    write_status(total, state)
    return total


def main() -> None:
    if not config.DRY_RUN:
        raise SystemExit("Refusing to run: DRY_RUN must be true")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # seed open position from prior agent trade if state empty
    state = load_state()
    log(f"START paper loop target=${TARGET_USD} poll={POLL_SECONDS}s DRY_RUN={config.DRY_RUN}")

    while True:
        try:
            total = cycle(state)
        except Exception as exc:
            log(f"cycle error: {type(exc).__name__}: {exc}")
            save_state(state)
            total = float(state.get("realized_pnl", 0.0))

        if total >= TARGET_USD:
            log(f"TARGET REACHED TOTAL=${total:+.2f}")
            save_state({**state, "finished_at": datetime.now(timezone.utc).isoformat(), "final_pnl": total})
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
