# Crypto Magic Nine Scanner

Scan the crypto market-cap top 200 for TD-style Magic Nine setups on daily and
weekly candles, then write Markdown/CSV/HTML reports and optionally push the
summary to Telegram.

Market-cap ranking comes from CoinGecko. OHLCV data uses Binance spot first and
falls back to OKX spot when Binance is unavailable or a pair is missing.

## Signal Rule

- Buy setup: current close is lower than the close 4 candles earlier.
- Sell setup: current close is higher than the close 4 candles earlier.
- `fresh_9`: count just moved from 8 to 9.
- `extended_9_plus`: count is above 9 and the setup is still extending.
- `near_7` / `near_8`: count is close to a 9 setup.

This scanner is a technical watchlist tool, not financial advice.

## Setup

Copy the example config if you want to customize defaults:

```powershell
Copy-Item config.example.json config.json
```

For Telegram push, create a bot with BotFather and set these environment
variables:

```powershell
$env:TELEGRAM_BOT_TOKEN="123456:your_bot_token"
$env:TELEGRAM_CHAT_ID="your_chat_id"
```

Or copy `.env.example` to `.env` and fill in the values:

```powershell
Copy-Item .env.example .env
```

## Run

Scan top 200 and write reports:

```powershell
python scanner.py
```

Scan and push to Telegram:

```powershell
python scanner.py --telegram
```

Run through the `.env` loader:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_scanner.ps1
```

Quick smoke test with fewer assets:

```powershell
python scanner.py --top 20
```

Reports are written to:

- `reports/latest.md`
- `reports/latest.csv`
- `reports/latest.html`
- timestamped Markdown and CSV files

The HTML report embeds the scan data and renders TradingView-style dark
candlestick charts locally with Canvas. Weekly and daily signals are separated
with quick filters. Use the list, search box, buttons, or keyboard shortcuts to
flip through candidates quickly without loading a remote chart for every symbol.

## Automation

Use Windows Task Scheduler to run it daily and weekly, or run one daily job
because the script scans both daily and weekly candles every time.

Register a twice-daily task. Times are interpreted in the Windows machine's
local timezone, so on this machine they are Beijing time:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_task.ps1
```

The generated task runs:

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Administrator\Documents\Codex\2026-05-29\200\run_scanner.ps1
```

Recommended schedule:

- Daily: after the UTC daily candle has closed.
- Weekly: after the UTC weekly candle has closed, or simply rely on the daily
  run because weekly candles are included.
