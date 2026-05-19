# GEX App — Build Progress

## Session: May 18, 2026

### What we built
- **Schwab API auth** (`src/auth.py`) — OAuth2 flow, token exchange, saves to `data/tokens.json`
- **GEX calculator** (`src/gex.py`) — fetches SPX/SPY/QQQ/NDX options chain, calculates gamma exposure by strike, identifies flip level / put wall / call wall / pin
- **Price history** (`src/price_history.py`) — fetches 1-min OHLCV candles from Schwab, saves to CSV per ticker, gap-fills on startup
- **Flask dashboard** (`src/dashboard.py`) — serves live GEX + price data via API endpoints, background refresh every 60s
- **Dashboard UI** (`templates/dashboard.html`) — GEX charts on top, price charts on bottom, double-click to expand any chart fullscreen, 1-min ET timestamps

### Data files (gitignored)
- `data/price_history_SPY.csv` — 1-min candles from May 4
- `data/price_history_QQQ.csv` — 1-min candles from May 4
- `data/tokens.json` — Schwab API tokens (never commit)

### Symbols
- **GEX**: SPX, SPY, QQQ, NDX (options chain — market hours only)
- **Price**: SPY, QQQ, SPX, NDX, ES (1-min candles)

### Known limitations
- SPX/NDX GEX only works during market hours (OI zeroed after close)
- ES price history not confirmed working (Schwab may not support futures)
- Historical data only goes back 10 days via Schwab API (max for 1-min)

### Next steps
- Import historical 1-min data from Kaggle/FirstRate for SPY/QQQ
- Wire up Schwab WebSocket for real-time price streaming
- Add price alerts when crossing GEX key levels
- Test full GEX output during market hours (9:30–16:00 ET)
- Add daily candle chart alongside 1-min

### How to run
```
cd "C:\Claude\Trading App\TradingApp"
.venv\Scripts\activate
python src/auth.py       # only needed first time or after 7 days
python src/dashboard.py  # starts the app at http://127.0.0.1:5000
```

### Schwab API
- App: GEX App (developer.schwab.com)
- Credentials in `.env` (gitignored)
- Access token: 30 min expiry, auto-refreshed via refresh token (7 days)
