# Polymarket Pipeline V2

An AI-powered breaking news detector that classifies events against prediction markets and trades automatically when it finds edge.

```
Breaking News (Twitter / Telegram / RSS)
        ↓ (< 5 seconds)
Match to niche markets (< $500K volume)
        ↓
Claude Classification: bullish / bearish / neutral + materiality
        ↓
Edge detection + quarter-Kelly sizing
        ↓
Instant execution → SQLite log → calibration tracking
```

## What Changed From V1

V1 scraped RSS feeds (5-60 min delay), asked Claude "what's the probability?" (wrong question for LLMs), and competed on high-volume markets (where every bot already operates).

V2 inverts all three:
- **Speed**: Real-time Twitter/Telegram streams instead of stale RSS
- **Classification**: Claude classifies "bullish or bearish?" instead of estimating probability — a task LLMs are actually good at
- **Niche markets**: Only trades markets under $500K volume where the crowd is small and slow

---

## Setup (2 minutes)

### Quickstart with Make (recommended)

One command does everything — installs deps, authenticates, and starts operating:

```bash
make dev      # setup + Codex login (ChatGPT OAuth) + run in DRY-RUN (no real money)
make prod     # same, but LIVE trading with real money (asks for confirmation)
make local    # run fully offline with the heuristic engine (no login, no API key)
```

Other targets: `make setup`, `make auth`, `make verify`, `make dashboard`,
`make backtest`, `make trades`, `make stats`, `make clean`.

Pick the classification engine with `ENGINE` (default `codex`):

```bash
make dev ENGINE=codex        # ChatGPT subscription via OAuth (no API key)
make dev ENGINE=local        # offline heuristic
make dev ENGINE=anthropic    # Claude API key
```

`make dev`/`make prod` auto-run `codex login` only when `ENGINE=codex` and you are
not already authenticated.

### One-Command Setup

```bash
git clone https://github.com/brodyautomates/polymarket-pipeline.git
cd polymarket-pipeline
bash setup.sh
```

### Manual Setup

```bash
git clone https://github.com/brodyautomates/polymarket-pipeline.git
cd polymarket-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add your keys to `.env`:

```
CLASSIFIER_ENGINE=auto               # auto | anthropic | openai | local
ANTHROPIC_API_KEY=sk-ant-...         # Required for the Claude engine
OPENAI_API_KEY=...                   # Optional — alternative LLM engine
TWITTER_BEARER_TOKEN=...             # Optional — real-time news stream
TELEGRAM_BOT_TOKEN=...               # Optional — channel monitoring
POLYMARKET_API_KEY=...               # Optional — live trading only
```

### Classification Engines (No paid API required)

The classifier is pluggable via `CLASSIFIER_ENGINE`:

| Engine | Needs API key? | Notes |
|---|---|---|
| `auto` (default) | No | Uses Claude if `ANTHROPIC_API_KEY` is set, otherwise falls back to `local` |
| `anthropic` | Yes | Claude (best accuracy) |
| `openai` | Yes | GPT via OpenAI (`OPENAI_MODEL`, default `gpt-4o-mini`) |
| `codex` | **No (OAuth)** | Codex CLI using your ChatGPT subscription — no API key |
| `local` | **No** | Offline heuristic — runs the whole pipeline with no API key or network |

Set `CLASSIFIER_ENGINE=local` to run everything (`watch`, `backtest`, `dashboard`)
end to end without any LLM spend. It is less accurate than an LLM, but fully
deterministic and free — ideal for development and testing.

#### Codex engine (ChatGPT OAuth, no API key)

Use your ChatGPT plan (Plus/Pro/Business) instead of a pay-per-token API key:

```bash
# 1. Install the Codex CLI (one time)
npm install -g @openai/codex

# 2. Sign in with ChatGPT (OAuth) — no API key needed
codex login            # opens a browser; use `codex login --device-auth` on headless boxes

# 3. Point the pipeline at Codex
CLASSIFIER_ENGINE=codex python cli.py watch
```

Under the hood each classification runs `codex exec` non-interactively in a
read-only sandbox and reads back a JSON verdict. Notes:

- **No API key** — it reuses the OAuth credentials in `~/.codex/auth.json`.
- Optional: `CODEX_MODEL` to pick a model, `CODEX_TIMEOUT` (seconds) per call.
- Slower than a direct API call (it spins up the CLI agent per request), so it is
  best for local runs and testing rather than the sub-5s latency target.
- OpenAI's docs recommend API keys for automation; use subscription/OAuth auth
  only if that fits your account's terms.
- If the CLI is missing or not logged in, the pipeline automatically falls back to
  the `local` engine.

### Verify

```bash
python cli.py verify
```

---

## How to Use

### V2: Event-Driven Pipeline (Recommended)

```bash
# Start the real-time pipeline — monitors news streams, classifies, trades
python cli.py watch

# Enable live trading
python cli.py watch --live
```

The `watch` command runs indefinitely. It connects to your configured news sources (Twitter, Telegram, RSS fallback), matches breaking headlines to niche Polymarket markets, classifies each with Claude, and executes trades when it finds edge.

### V1: Synchronous Pipeline

```bash
# Single scan — scrape RSS, score markets, log signals
python cli.py run

python cli.py run --max 15 --hours 12
```

### Live Dashboard

```bash
python cli.py dashboard
```

### Backtest

```bash
# Validate the V2 strategy against resolved markets
python cli.py backtest

python cli.py backtest --limit 50 --category ai
```

### All Commands

| Command | What it does |
|---|---|
| `python cli.py watch` | V2: Real-time event-driven pipeline |
| `python cli.py run` | V1: Synchronous RSS-based pipeline |
| `python cli.py dashboard` | Live terminal dashboard |
| `python cli.py backtest` | Backtest against resolved markets |
| `python cli.py calibrate` | Classification accuracy report |
| `python cli.py niche` | Browse niche markets (volume-filtered) |
| `python cli.py verify` | Check all API keys and connections |
| `python cli.py scrape` | Test news scraper |
| `python cli.py markets` | Browse all active markets |
| `python cli.py trades` | View trade log |
| `python cli.py stats` | Performance + latency + calibration stats |

---

## Architecture

### V2 Pipeline (Event-Driven)

```
news_stream.py      Real-time news — Twitter API v2, Telegram, RSS fallback
market_watcher.py   Polymarket WebSocket — live prices, niche filter, momentum
classifier.py       Claude classification — bullish/bearish/neutral + materiality
matcher.py          Routes breaking news to relevant markets
edge.py             Edge detection + Kelly sizing (V2: classification-based)
executor.py         Trade execution — dry-run + live CLOB orders (async)
pipeline.py         Event-driven orchestrator (asyncio)
calibrator.py       Tracks classification accuracy over time
backtest.py         Historical replay for strategy validation
```

### Shared Infrastructure

```
logger.py           SQLite — trades, news events, calibration, latency tracking
config.py           All settings, API keys, thresholds
dashboard.py        Bloomberg Terminal-style live dashboard
cli.py              CLI — watch, run, backtest, calibrate, niche, verify, etc.
```

---

## How It Actually Works

### 1. News Detection
Real-time streams from Twitter (filtered by keywords: OpenAI, Bitcoin, Fed rate, etc.), Telegram channels, and RSS fallback. Events are deduplicated and timestamped with receive latency.

### 2. Market Matching
Each headline is matched to active niche markets (<$500K volume) by keyword overlap. Only relevant markets proceed to classification.

### 3. Classification (The Key Shift)
Instead of "what's the probability?", Claude is asked: *"Does this news make the market MORE likely to resolve YES, MORE likely to resolve NO, or is it NOT RELEVANT?"*

This is a classification task — something LLMs are genuinely good at. Claude also rates materiality (0-1): how much should this move the price?

### 4. Edge Detection
If direction is bullish/bearish AND materiality exceeds threshold (default 0.6) AND the market price has room to move — that's a signal. Position sizing uses quarter-Kelly.

### 5. Execution
Dry-run by default. Live mode places orders via Polymarket CLOB API. Safety: $25 max bet, $100 daily limit.

### 6. Calibration
Every trade is tracked. As markets resolve, the system measures whether its classifications were correct. Accuracy by source and category informs future confidence.

---

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `DRY_RUN` | `true` | Set to `false` for live trading |
| `MAX_BET_USD` | `25` | Maximum single bet |
| `DAILY_LOSS_LIMIT_USD` | `100` | Pipeline halts if breached |
| `EDGE_THRESHOLD` | `0.10` | Minimum edge to trigger trade |
| `MAX_VOLUME_USD` | `500000` | Only trade markets below this volume |
| `MIN_VOLUME_USD` | `1000` | Skip dead markets |
| `MATERIALITY_THRESHOLD` | `0.6` | Minimum materiality to act on |
| `SPEED_TARGET_SECONDS` | `5` | Target news-to-trade latency |

---

## Safety

- Dry-run mode ON by default
- $25 max single bet, $100 daily limit
- Quarter-Kelly position sizing
- Niche market filter prevents competing against sophisticated bots
- Calibration tracking — auto-detects if strategy accuracy drops
- All API keys in `.env`, never committed

---

Built by [@brodyautomates](https://github.com/brodyautomates)

---

## Disclaimer

This project is for **entertainment and educational purposes only**. It is not financial advice. The authors are not responsible for any financial losses incurred through the use of this software. Prediction market trading carries significant risk — you can lose money. Never trade with funds you cannot afford to lose. Past performance of any strategy does not guarantee future results. Use at your own risk.
