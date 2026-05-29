#!/usr/bin/env python3
"""
Crypto Magic Nine scanner.

Scans CoinGecko market-cap top coins, pulls Binance spot OHLCV data, detects
TD-style 9 setup counts on daily and weekly candles, writes reports, and can
send the result to Telegram.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass(frozen=True)
class Coin:
    rank: int
    coin_id: str
    symbol: str
    name: str
    market_cap: float | None
    volume_24h: float | None


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass(frozen=True)
class MarketSymbol:
    provider: str
    symbol: str
    display_symbol: str


@dataclass(frozen=True)
class Signal:
    rank: int
    coin: str
    name: str
    symbol: str
    provider: str
    timeframe: str
    direction: str
    count: int
    status: str
    close: float
    candle_time: str
    volume_change: float | None
    market_cap: float | None
    volume_24h: float | None
    chart: list[dict[str, Any]]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def request_json(url: str, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    headers = {
        "Accept": "application/json",
        "User-Agent": "crypto-magic-nine-scanner/0.1",
    }
    req = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(1.5 * attempt)

    raise RuntimeError(f"Request failed after {retries} attempts: {url}") from last_error


def fetch_top_coins(limit: int) -> list[Coin]:
    data = request_json(
        COINGECKO_MARKETS_URL,
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h,7d",
        },
    )
    coins: list[Coin] = []
    for row in data:
        rank = int(row.get("market_cap_rank") or len(coins) + 1)
        coins.append(
            Coin(
                rank=rank,
                coin_id=str(row["id"]),
                symbol=str(row["symbol"]).upper(),
                name=str(row["name"]),
                market_cap=row.get("market_cap"),
                volume_24h=row.get("total_volume"),
            )
        )
    return coins


def fetch_binance_symbols(quote_asset: str) -> set[str]:
    data = request_json(BINANCE_EXCHANGE_INFO_URL)
    symbols: set[str] = set()
    for row in data.get("symbols", []):
        if row.get("status") != "TRADING":
            continue
        if row.get("quoteAsset") != quote_asset:
            continue
        if not row.get("isSpotTradingAllowed", False):
            continue
        symbols.add(str(row["symbol"]))
    return symbols


def fetch_okx_symbols(quote_asset: str) -> set[str]:
    data = request_json(OKX_INSTRUMENTS_URL, {"instType": "SPOT"})
    symbols: set[str] = set()
    for row in data.get("data", []):
        if row.get("state") != "live":
            continue
        if row.get("quoteCcy") != quote_asset:
            continue
        symbols.add(str(row["instId"]))
    return symbols


def fetch_provider_symbols(provider: str, quote_asset: str) -> set[str]:
    if provider == "binance":
        return fetch_binance_symbols(quote_asset)
    if provider == "okx":
        return fetch_okx_symbols(quote_asset)
    raise ValueError(f"Unsupported provider: {provider}")


def candidate_symbol(provider: str, coin_symbol: str, quote_asset: str) -> MarketSymbol:
    if provider == "binance":
        symbol = f"{coin_symbol}{quote_asset}"
        return MarketSymbol(provider=provider, symbol=symbol, display_symbol=symbol)
    if provider == "okx":
        symbol = f"{coin_symbol}-{quote_asset}"
        return MarketSymbol(provider=provider, symbol=symbol, display_symbol=symbol)
    raise ValueError(f"Unsupported provider: {provider}")


def fetch_binance_klines(symbol: str, interval: str, limit: int) -> list[Candle]:
    data = request_json(
        BINANCE_KLINES_URL,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
    )
    candles: list[Candle] = []
    for item in data:
        candles.append(
            Candle(
                open_time=int(item[0]),
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
                close_time=int(item[6]),
            )
        )
    return candles


def okx_bar(interval: str) -> str:
    mapping = {
        "1d": "1Dutc",
        "1w": "1Wutc",
    }
    if interval not in mapping:
        raise ValueError(f"Unsupported OKX interval: {interval}")
    return mapping[interval]


def fetch_okx_klines(symbol: str, interval: str, limit: int) -> list[Candle]:
    data = request_json(
        OKX_CANDLES_URL,
        {
            "instId": symbol,
            "bar": okx_bar(interval),
            "limit": min(limit, 300),
        },
    )
    candles: list[Candle] = []
    for item in reversed(data.get("data", [])):
        open_time = int(item[0])
        candles.append(
            Candle(
                open_time=open_time,
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
                close_time=open_time,
            )
        )
    return candles


def fetch_klines(provider: str, symbol: str, interval: str, limit: int) -> list[Candle]:
    if provider == "binance":
        return fetch_binance_klines(symbol, interval, limit)
    if provider == "okx":
        return fetch_okx_klines(symbol, interval, limit)
    raise ValueError(f"Unsupported provider: {provider}")


def setup_counts(candles: list[Candle]) -> tuple[list[int], list[int]]:
    buy_counts = [0] * len(candles)
    sell_counts = [0] * len(candles)

    for i in range(4, len(candles)):
        close = candles[i].close
        compare_close = candles[i - 4].close
        if close < compare_close:
            buy_counts[i] = buy_counts[i - 1] + 1
        if close > compare_close:
            sell_counts[i] = sell_counts[i - 1] + 1

    return buy_counts, sell_counts


def classify_count(current: int, previous: int, near_counts: set[int]) -> str | None:
    if current == 9 and previous == 8:
        return "fresh_9"
    if current > 9:
        return "extended_9_plus"
    if current in near_counts:
        return f"near_{current}"
    return None


def volume_change(candles: list[Candle], lookback: int = 20) -> float | None:
    if len(candles) < lookback + 1:
        return None
    recent = candles[-1].volume
    baseline_values = [c.volume for c in candles[-lookback - 1 : -1]]
    baseline = sum(baseline_values) / len(baseline_values)
    if baseline == 0:
        return None
    return recent / baseline - 1


def iso_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def chart_points(candles: list[Candle], buy_counts: list[int], sell_counts: list[int]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for candle, buy_count, sell_count in zip(candles, buy_counts, sell_counts):
        points.append(
            {
                "time": iso_date(candle.open_time),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "buy": buy_count,
                "sell": sell_count,
            }
        )
    return points


def detect_signals(
    coin: Coin,
    market_symbol: MarketSymbol,
    timeframe: str,
    candles: list[Candle],
    near_counts: set[int],
) -> list[Signal]:
    if len(candles) < 20:
        return []

    buy_counts, sell_counts = setup_counts(candles)
    chart = chart_points(candles, buy_counts, sell_counts)
    signals: list[Signal] = []
    vc = volume_change(candles)

    candidates = [
        ("buy", buy_counts[-1], buy_counts[-2]),
        ("sell", sell_counts[-1], sell_counts[-2]),
    ]
    for direction, current, previous in candidates:
        status = classify_count(current, previous, near_counts)
        if not status:
            continue
        signals.append(
            Signal(
                rank=coin.rank,
                coin=coin.coin_id,
                name=coin.name,
                symbol=market_symbol.display_symbol,
                provider=market_symbol.provider,
                timeframe=timeframe,
                direction=direction,
                count=current,
                status=status,
                close=candles[-1].close,
                candle_time=iso_date(candles[-1].close_time),
                volume_change=vc,
                market_cap=coin.market_cap,
                volume_24h=coin.volume_24h,
                chart=chart,
            )
        )
    return signals


def signal_sort_key(signal: Signal) -> tuple[int, int, int]:
    status_rank = {"fresh_9": 0, "extended_9_plus": 1}.get(signal.status, 2)
    timeframe_rank = {"1w": 0, "1d": 1}.get(signal.timeframe, 2)
    return (timeframe_rank, status_rank, signal.rank)


def format_money(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    return f"${value:,.0f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:+.1f}%"


def grouped_signal_sections(signals: list[Signal]) -> list[tuple[str, str, list[Signal]]]:
    sections = [
        ("Weekly Signals", "1w", []),
        ("Daily Signals", "1d", []),
    ]
    by_timeframe = {timeframe: bucket for _, timeframe, bucket in sections}
    for signal in sorted(signals, key=signal_sort_key):
        by_timeframe.setdefault(signal.timeframe, []).append(signal)
    return sections


def append_signal_table(lines: list[str], section_signals: list[Signal]) -> None:
    lines.extend(
        [
            "| Rank | Coin | Source | TF | Direction | Count | Status | Close | Candle | Vol vs 20 | Market Cap |",
            "|---:|---|---|---|---|---:|---|---:|---|---:|---:|",
        ]
    )
    for signal in section_signals:
        lines.append(
            "| "
            f"{signal.rank} | {signal.name} `{signal.symbol}` | {signal.provider} | {signal.timeframe} | "
            f"{signal.direction} | {signal.count} | {signal.status} | "
            f"{signal.close:.8g} | {signal.candle_time} | {format_pct(signal.volume_change)} | "
            f"{format_money(signal.market_cap)} |"
        )
    lines.append("")


def render_markdown(signals: list[Signal], skipped: list[Coin], generated_at: str) -> str:
    lines = [
        "# Crypto Magic Nine Scan",
        "",
        f"Generated at: {generated_at} UTC",
        "",
        "Rule: buy setup means the close is lower than the close 4 candles earlier; "
        "sell setup means the close is higher than the close 4 candles earlier.",
        "",
    ]

    if not signals:
        lines.extend(["No fresh or near Magic Nine setups found.", ""])
    else:
        for title, _, section_signals in grouped_signal_sections(signals):
            lines.extend([f"## {title}", ""])
            if section_signals:
                append_signal_table(lines, section_signals)
            else:
                lines.extend(["No signals in this timeframe.", ""])

    lines.extend(
        [
            "## Coverage",
            "",
            f"- Signals found: {len(signals)}",
            f"- Spot USDT pairs skipped: {len(skipped)}",
            "",
        ]
    )

    if skipped:
        skipped_preview = ", ".join(f"{coin.name} ({coin.symbol})" for coin in skipped[:30])
        suffix = "..." if len(skipped) > 30 else ""
        lines.append(f"Skipped sample: {skipped_preview}{suffix}")
        lines.append("")

    lines.append("This is a technical signal scan, not financial advice.")
    lines.append("")
    return "\n".join(lines)


def write_csv(path: Path, signals: list[Signal]) -> None:
    fieldnames = [
        "rank",
        "coin",
        "name",
        "symbol",
        "provider",
        "timeframe",
        "direction",
        "count",
        "status",
        "close",
        "candle_time",
        "volume_change",
        "market_cap",
        "volume_24h",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for signal in sorted(signals, key=signal_sort_key):
            writer.writerow({key: getattr(signal, key) for key in fieldnames})


def render_html(signals: list[Signal], skipped: list[Coin], generated_at: str) -> str:
    sorted_signals = sorted(signals, key=signal_sort_key)
    payload = []
    for idx, signal in enumerate(sorted_signals):
        payload.append(
            {
                "idx": idx + 1,
                "rank": signal.rank,
                "name": signal.name,
                "symbol": signal.symbol,
                "provider": signal.provider,
                "timeframe": signal.timeframe,
                "direction": signal.direction,
                "count": signal.count,
                "status": signal.status,
                "close": signal.close,
                "candle_time": signal.candle_time,
                "volume_change": format_pct(signal.volume_change),
                "market_cap": format_money(signal.market_cap),
                "chart": signal.chart,
            }
        )

    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    generated = html.escape(generated_at)
    skipped_count = len(skipped)
    signal_count = len(signals)
    weekly_count = sum(1 for signal in signals if signal.timeframe == "1w")
    daily_count = sum(1 for signal in signals if signal.timeframe == "1d")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Magic Nine Scan</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0b0e11;
  --panel: #10151c;
  --panel-2: #151b24;
  --grid: #202938;
  --text: #d7dde8;
  --muted: #7d8796;
  --green: #26a69a;
  --red: #ef5350;
  --blue: #4c8bf5;
  --amber: #f5b84c;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0;
}}
.app {{
  display: grid;
  grid-template-columns: 360px 1fr;
  min-height: 100vh;
}}
aside {{
  border-right: 1px solid var(--grid);
  background: var(--panel);
  min-height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}}
header {{
  padding: 18px 18px 14px;
  border-bottom: 1px solid var(--grid);
}}
h1 {{
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 680;
}}
.meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
}}
.toolbar {{
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--grid);
}}
.segments {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  padding: 10px 12px 0;
}}
input, button {{
  height: 34px;
  border: 1px solid var(--grid);
  background: var(--panel-2);
  color: var(--text);
  border-radius: 6px;
  font: inherit;
}}
input {{ min-width: 0; padding: 0 10px; }}
button {{
  min-width: 38px;
  padding: 0 10px;
  cursor: pointer;
}}
button:hover {{ border-color: var(--blue); }}
.segment.active {{
  border-color: var(--blue);
  background: #172232;
  color: #ffffff;
}}
.list {{
  overflow: auto;
  padding: 8px;
}}
.row {{
  width: 100%;
  display: grid;
  grid-template-columns: 44px 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text);
  background: transparent;
  text-align: left;
}}
.row:hover {{ background: #131a23; }}
.row.active {{
  background: #172232;
  border-color: #2b5b9f;
}}
.rank {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
.coin {{ min-width: 0; }}
.name {{
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}}
.sub {{
  color: var(--muted);
  font-size: 12px;
  margin-top: 2px;
}}
.badge {{
  font-size: 12px;
  border-radius: 999px;
  padding: 4px 8px;
  background: #202938;
  color: var(--muted);
}}
.badge.buy {{ color: var(--green); }}
.badge.sell {{ color: var(--red); }}
main {{
  min-width: 0;
  display: grid;
  grid-template-rows: auto minmax(360px, 1fr) 130px;
}}
.chart-head {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
  padding: 18px 22px 12px;
  border-bottom: 1px solid var(--grid);
}}
.title {{
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}}
.title strong {{ font-size: 22px; }}
.title span {{ color: var(--muted); }}
.stats {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}}
.stat {{
  background: var(--panel);
  border: 1px solid var(--grid);
  border-radius: 6px;
  padding: 7px 10px;
  font-size: 12px;
  color: var(--muted);
}}
.stat b {{ color: var(--text); font-weight: 620; }}
.canvas-wrap {{
  position: relative;
  min-height: 360px;
}}
canvas {{
  width: 100%;
  height: 100%;
  display: block;
}}
.foot {{
  border-top: 1px solid var(--grid);
  background: var(--panel);
  padding: 14px 22px;
  color: var(--muted);
  font-size: 12px;
  display: grid;
  align-content: center;
  gap: 5px;
}}
.empty {{
  display: grid;
  min-height: 100vh;
  place-items: center;
  color: var(--muted);
}}
@media (max-width: 900px) {{
  .app {{ grid-template-columns: 1fr; }}
  aside {{ min-height: auto; max-height: 42vh; border-right: 0; border-bottom: 1px solid var(--grid); }}
  main {{ min-height: 58vh; }}
  .chart-head {{ grid-template-columns: 1fr; }}
  .stats {{ justify-content: flex-start; }}
}}
</style>
</head>
<body>
<div id="app"></div>
<script>
const SIGNALS = {data_json};
const generatedAt = "{generated}";
const skippedCount = {skipped_count};
const signalCount = {signal_count};
const weeklyCount = {weekly_count};
const dailyCount = {daily_count};
const app = document.getElementById("app");
let active = 0;
let query = "";
let timeframeFilter = "all";
let filtered = SIGNALS.map((_, i) => i);

function fmtPrice(value) {{
  if (value >= 100) return value.toFixed(2);
  if (value >= 1) return value.toFixed(4);
  return value.toPrecision(5);
}}

function renderShell() {{
  if (!SIGNALS.length) {{
    app.innerHTML = `<div class="empty">No Magic Nine signals found. Generated at ${{generatedAt}} UTC.</div>`;
    return;
  }}
  app.innerHTML = `
    <div class="app">
      <aside>
        <header>
          <h1>Crypto Magic Nine Scan</h1>
          <div class="meta"><span>${{generatedAt}} UTC</span><span>${{weeklyCount}} weekly</span><span>${{dailyCount}} daily</span><span>${{skippedCount}} skipped</span></div>
        </header>
        <div class="segments">
          <button class="segment active" data-timeframe="all">All ${{signalCount}}</button>
          <button class="segment" data-timeframe="1w">Weekly ${{weeklyCount}}</button>
          <button class="segment" data-timeframe="1d">Daily ${{dailyCount}}</button>
        </div>
        <div class="toolbar">
          <input id="search" placeholder="Search symbol or name" autocomplete="off">
          <button id="prev" title="Previous">↑</button>
          <button id="next" title="Next">↓</button>
        </div>
        <div id="list" class="list"></div>
      </aside>
      <main>
        <section class="chart-head">
          <div class="title"><strong id="chartTitle"></strong><span id="chartSub"></span></div>
          <div id="stats" class="stats"></div>
        </section>
        <section class="canvas-wrap"><canvas id="chart"></canvas></section>
        <section class="foot">
          <div>Use ↑ / ↓ or J / K to flip through candidates. Data is embedded in this file, so navigation is instant after opening.</div>
          <div>Rule: buy setup closes below the close 4 candles earlier; sell setup closes above it. This is a technical signal scan, not financial advice.</div>
        </section>
      </main>
    </div>`;

  document.getElementById("search").addEventListener("input", (event) => {{
    query = event.target.value.trim().toLowerCase();
    applyFilters(true);
  }});
  document.querySelectorAll(".segment").forEach(button => {{
    button.addEventListener("click", () => {{
      timeframeFilter = button.dataset.timeframe;
      document.querySelectorAll(".segment").forEach(item => item.classList.toggle("active", item === button));
      applyFilters(true);
    }});
  }});
  document.getElementById("prev").addEventListener("click", () => step(-1));
  document.getElementById("next").addEventListener("click", () => step(1));
  window.addEventListener("resize", () => drawChart(SIGNALS[active]));
  window.addEventListener("keydown", (event) => {{
    if (event.target.tagName === "INPUT") return;
    if (event.key === "ArrowUp" || event.key.toLowerCase() === "k") step(-1);
    if (event.key === "ArrowDown" || event.key.toLowerCase() === "j") step(1);
  }});
  renderList();
  renderActive();
}}

function applyFilters(resetActive) {{
  filtered = SIGNALS
    .map((signal, index) => [signal, index])
    .filter(([signal]) => timeframeFilter === "all" || signal.timeframe === timeframeFilter)
    .filter(([signal]) => !query || `${{signal.name}} ${{signal.symbol}} ${{signal.status}} ${{signal.direction}} ${{signal.timeframe}}`.toLowerCase().includes(query))
    .map(([, index]) => index);
  if (resetActive || !filtered.includes(active)) active = filtered[0] ?? 0;
  renderList();
  renderActive();
}}

function step(delta) {{
  if (!filtered.length) return;
  const pos = Math.max(0, filtered.indexOf(active));
  const nextPos = (pos + delta + filtered.length) % filtered.length;
  active = filtered[nextPos];
  renderList();
  renderActive();
}}

function renderList() {{
  const list = document.getElementById("list");
  if (!filtered.length) {{
    list.innerHTML = `<div class="sub" style="padding:12px">No signals match this filter.</div>`;
    return;
  }}
  list.innerHTML = filtered.map(index => {{
    const s = SIGNALS[index];
    return `<button class="row ${{index === active ? "active" : ""}}" data-index="${{index}}">
      <span class="rank">#${{s.rank}}</span>
      <span class="coin"><span class="name">${{s.name}}</span><span class="sub">${{s.symbol}} · ${{s.timeframe}} · ${{s.status}}</span></span>
      <span class="badge ${{s.direction}}">${{s.direction}} ${{s.count}}</span>
    </button>`;
  }}).join("");
  list.querySelectorAll(".row").forEach(row => {{
    row.addEventListener("click", () => {{
      active = Number(row.dataset.index);
      renderList();
      renderActive();
    }});
  }});
  const activeRow = list.querySelector(".row.active");
  if (activeRow) activeRow.scrollIntoView({{ block: "nearest" }});
}}

function renderActive() {{
  const s = SIGNALS[active];
  if (!s) return;
  document.getElementById("chartTitle").textContent = `${{s.name}} · ${{s.symbol}}`;
  document.getElementById("chartSub").textContent = `${{s.provider}} · ${{s.timeframe}} · candle ${{s.candle_time}}`;
  document.getElementById("stats").innerHTML = `
    <span class="stat">Signal <b>${{s.direction}} ${{s.count}}</b></span>
    <span class="stat">Status <b>${{s.status}}</b></span>
    <span class="stat">Close <b>${{fmtPrice(s.close)}}</b></span>
    <span class="stat">Vol vs 20 <b>${{s.volume_change}}</b></span>
    <span class="stat">Market cap <b>${{s.market_cap}}</b></span>`;
  drawChart(s);
}}

function drawChart(signal) {{
  const canvas = document.getElementById("chart");
  if (!canvas) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(600, Math.floor(rect.width * dpr));
  canvas.height = Math.max(360, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = canvas.width / dpr;
  const h = canvas.height / dpr;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b0e11";
  ctx.fillRect(0, 0, w, h);

  const data = signal.chart.slice(-160);
  const pad = {{ left: 56, right: 76, top: 22, bottom: 28 }};
  const volH = Math.max(70, h * 0.18);
  const priceBottom = h - pad.bottom - volH - 18;
  const priceH = priceBottom - pad.top;
  const plotW = w - pad.left - pad.right;
  const highs = data.map(d => d.high);
  const lows = data.map(d => d.low);
  const maxP = Math.max(...highs);
  const minP = Math.min(...lows);
  const span = Math.max(1e-12, maxP - minP);
  const topP = maxP + span * 0.08;
  const botP = minP - span * 0.08;
  const maxVol = Math.max(...data.map(d => d.volume), 1);
  const xStep = plotW / data.length;
  const candleW = Math.max(3, Math.min(10, xStep * 0.62));
  const y = value => pad.top + (topP - value) / (topP - botP) * priceH;
  const x = i => pad.left + i * xStep + xStep / 2;

  ctx.strokeStyle = "#202938";
  ctx.lineWidth = 1;
  ctx.font = "12px Inter, Segoe UI, sans-serif";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#7d8796";
  for (let i = 0; i <= 5; i++) {{
    const yy = pad.top + (priceH / 5) * i;
    const price = topP - ((topP - botP) / 5) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(w - pad.right, yy);
    ctx.stroke();
    ctx.fillText(fmtPrice(price), w - pad.right + 10, yy);
  }}

  data.forEach((d, i) => {{
    const xx = x(i);
    const up = d.close >= d.open;
    const color = up ? "#26a69a" : "#ef5350";
    const openY = y(d.open);
    const closeY = y(d.close);
    const highY = y(d.high);
    const lowY = y(d.low);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(xx, highY);
    ctx.lineTo(xx, lowY);
    ctx.stroke();
    const bodyTop = Math.min(openY, closeY);
    const bodyH = Math.max(1, Math.abs(closeY - openY));
    ctx.fillRect(xx - candleW / 2, bodyTop, candleW, bodyH);

    const volTop = h - pad.bottom - (d.volume / maxVol) * volH;
    ctx.globalAlpha = 0.35;
    ctx.fillRect(xx - candleW / 2, volTop, candleW, h - pad.bottom - volTop);
    ctx.globalAlpha = 1;

    const count = signal.direction === "buy" ? d.buy : d.sell;
    if (count >= 7) {{
      ctx.fillStyle = signal.direction === "buy" ? "#26a69a" : "#ef5350";
      ctx.font = "11px Inter, Segoe UI, sans-serif";
      ctx.fillText(String(count), xx - 3, signal.direction === "buy" ? lowY + 14 : highY - 14);
    }}
  }});

  const last = data[data.length - 1];
  const lastY = y(last.close);
  ctx.strokeStyle = "#4c8bf5";
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(pad.left, lastY);
  ctx.lineTo(w - pad.right, lastY);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#4c8bf5";
  ctx.fillRect(w - pad.right + 6, lastY - 10, 64, 20);
  ctx.fillStyle = "#ffffff";
  ctx.fillText(fmtPrice(last.close), w - pad.right + 10, lastY);

  ctx.fillStyle = "#7d8796";
  ctx.textBaseline = "alphabetic";
  const ticks = [0, Math.floor(data.length / 3), Math.floor(data.length * 2 / 3), data.length - 1];
  ticks.forEach(i => ctx.fillText(data[i].time, Math.min(w - pad.right - 78, x(i) - 32), h - 8));
}}

renderShell();
</script>
</body>
</html>
"""


def telegram_chunks(text: str, max_len: int = 3900) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.splitlines():
        extra = len(line) + 1
        if current and current_len + extra > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for Telegram push.")

    url = TELEGRAM_SEND_URL.format(token=token)
    for chunk in telegram_chunks(text):
        payload = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Telegram push failed with HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram push failed with HTTP {exc.code}: {body}") from exc


def scan(config: dict[str, Any]) -> tuple[list[Signal], list[Coin], str]:
    limit = int(config.get("market_cap_limit", 200))
    quote_asset = str(config.get("quote_asset", "USDT")).upper()
    near_counts = {int(x) for x in config.get("near_counts", [7, 8])}
    kline_limit = int(config.get("kline_limit", 260))
    timeframes: dict[str, str] = config.get("timeframes", {"1d": "daily", "1w": "weekly"})
    providers = [str(provider).lower() for provider in config.get("providers", ["binance", "okx"])]

    coins = fetch_top_coins(limit)
    provider_symbols: dict[str, set[str]] = {}
    for provider in providers:
        try:
            provider_symbols[provider] = fetch_provider_symbols(provider, quote_asset)
        except Exception as exc:
            print(f"Warning: provider {provider} unavailable: {exc}", file=sys.stderr)
            provider_symbols[provider] = set()

    signals: list[Signal] = []
    skipped: list[Coin] = []

    for coin in coins:
        market_symbol = None
        for provider in providers:
            symbol_candidate = candidate_symbol(provider, coin.symbol, quote_asset)
            if symbol_candidate.symbol in provider_symbols.get(provider, set()):
                market_symbol = symbol_candidate
                break
        if market_symbol is None:
            skipped.append(coin)
            continue

        for interval in timeframes.keys():
            candles = fetch_klines(market_symbol.provider, market_symbol.symbol, interval, kline_limit)
            signals.extend(detect_signals(coin, market_symbol, interval, candles, near_counts))
            time.sleep(0.05)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return signals, skipped, generated_at


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan market-cap top crypto assets for daily and weekly Magic Nine setups."
    )
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--out-dir", default="reports", help="Directory for report files.")
    parser.add_argument("--telegram", action="store_true", help="Push markdown summary to Telegram.")
    parser.add_argument("--top", type=int, help="Override market-cap scan limit.")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = load_config(config_path)
    if args.top:
        config["market_cap_limit"] = args.top

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(Path(".env"))

    signals, skipped, generated_at = scan(config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    markdown = render_markdown(signals, skipped, generated_at)
    html_report = render_html(signals, skipped, generated_at)
    md_path = out_dir / f"magic-nine-{stamp}.md"
    csv_path = out_dir / f"magic-nine-{stamp}.csv"
    html_path = out_dir / f"magic-nine-{stamp}.html"
    latest_md = out_dir / "latest.md"
    latest_csv = out_dir / "latest.csv"
    latest_html = out_dir / "latest.html"

    md_path.write_text(markdown, encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_report, encoding="utf-8")
    latest_html.write_text(html_report, encoding="utf-8")
    write_csv(csv_path, signals)
    write_csv(latest_csv, signals)

    if args.telegram:
        send_telegram(markdown)

    print(
        textwrap.dedent(
            f"""
            Scan complete.
            Signals: {len(signals)}
            Skipped spot pairs: {len(skipped)}
            Markdown: {md_path}
            CSV: {csv_path}
            HTML: {html_path}
            """
        ).strip()
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
