# 🔥 Indian Options — Unusual Flow Scanner

Detects anomalous options activity on NSE/BSE. Runs automatically via GitHub Actions,
saves an HTML report you can open anytime. No server, no subscription, no polling loop.

---

## How it works

1. **You push `watchlist.txt`** with your symbols.
2. **GitHub Actions runs the scanner** at 9:45 AM, 12:00 PM, and 3:15 PM IST on weekdays.
3. **Scanner fetches NSE option chains** for every symbol (one snapshot, no polling).
4. **Anomaly engine scores every contract** across 4 signals (see below).
5. **HTML + CSV report is committed** back to `reports/latest.html` in your repo.
6. **You open the report** in your browser whenever you want.

---

## Setup (5 minutes)

### 1. Fork or clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/options-flow-scanner
cd options-flow-scanner
```

### 2. Edit your watchlist

Open `watchlist.txt` and add/remove symbols — one per line, lines with `#` are comments:

```
NIFTY
BANKNIFTY
RELIANCE
HDFCBANK
# This line is ignored
ZOMATO
```

Use the **exact NSE ticker** as shown on nseindia.com.

### 3. Push to GitHub

```bash
git add .
git commit -m "Initial setup"
git push
```

GitHub Actions will now run automatically at the scheduled times.

### 4. View your report

Go to your repo → `reports/latest.html` → click **Raw** → save and open in browser.

Or enable **GitHub Pages** (repo Settings → Pages → deploy from `main` branch `/reports` folder)
and visit `https://YOUR_USERNAME.github.io/options-flow-scanner/latest.html` directly.

---

## Telegram alerts (optional)

To receive a summary on Telegram after each run:

1. Create a bot via [@BotFather](https://t.me/BotFather) → copy the token
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot)
3. Go to your GitHub repo → **Settings → Secrets → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID`   = your chat ID

The top 10 signals will be sent as a formatted message after each run.

---

## Running manually

```bash
pip install -r requirements.txt

# Basic run — all expiries 1–90 days out
python scanner.py

# Only look at near-term expiry (next 7 days)
python scanner.py --expiry-days 1 7

# Only look at far-dated flow (1–6 months) — more institutional
python scanner.py --expiry-days 30 180

# Calls only, high-conviction signals only
python scanner.py --call-only --min-score 60

# Custom watchlist
python scanner.py --watchlist my_stocks.txt --output my_reports/
```

---

## Anomaly signals explained

| Signal | What it means |
|---|---|
| **VOL_OI_SPIKE** | Volume crossed 2.5× the open interest in one session. More contracts traded than currently held — unusual accumulation. |
| **ASK_SKEW** | LTP is consistently near the ask price. Buyers are aggressively lifting offers rather than waiting — urgency signal. |
| **PREMIUM_SURGE** | Option premium moved 12%+ from its day-open price alongside volume. |
| **OI_BUILDUP** | Open interest grew 15%+ today — fresh money opening new positions, not just day-trading. |
| **COMBINED** | Three or more of the above firing together, or score ≥ 70. Highest conviction signal. |

**Far-dated anomalies score higher** — buying 60-day options costs more, so unusual flow there implies stronger conviction than the same pattern in weekly expiries.

---

## Adjusting thresholds

Edit the `DEFAULTS` dict at the top of `scanner.py`, or pass CLI flags:

| Flag | Default | Description |
|---|---|---|
| `--min-score` | 35 | Drop signals below this score |
| `--expiry-days 1 90` | 1–90 | Expiry window in calendar days |
| `--vol-oi-ratio` | 2.5 | Vol/OI spike threshold |
| `--call-only` | off | Only show CE signals |
| `--put-only` | off | Only show PE signals |

---

## Scheduling notes

- The cron runs **3× per day** on weekdays. You can add/remove runs in `.github/workflows/scanner.yml`.
- GitHub Actions has a ~2000 free minutes/month on public repos; this scanner uses ~1–2 min per run, so ~60 runs/month ≈ 60–120 minutes — well within the free tier.
- NSE data is ~15 minutes delayed on the free web API. For real-time data, replace `fetch_chain()` with a Zerodha Kite or Upstox API call.

---

## Data source

Uses NSE's public option chain API (same data as nseindia.com).
Free, no API key needed, ~15 min delayed during market hours.
