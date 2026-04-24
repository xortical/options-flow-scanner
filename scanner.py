"""
Indian Options Anomaly Flow Scanner
====================================
Designed to run ONCE (via GitHub Actions cron, task scheduler, or manually).
No polling. Fetches NSE option chain snapshot, scores every contract,
filters by expiry window, and saves an HTML + CSV report.

Usage:
    python scanner.py                        # uses watchlist.txt in same folder
    python scanner.py --watchlist my.txt     # custom watchlist path
    python scanner.py --min-score 40         # override anomaly threshold
    python scanner.py --expiry-days 1 60     # only show expiries 1–60 days out
    python scanner.py --output reports/      # custom output folder
"""

import os
import sys
import time
import datetime
import argparse
import sqlite3
import requests
import pandas as pd
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────
# CONFIG  (all overridable via CLI args or env vars)
# ─────────────────────────────────────────────────────────────────
DEFAULTS = dict(
    watchlist_path   = "watchlist.txt",
    output_dir       = "reports",
    min_score        = 35,          # 0–100, signals below this are dropped
    expiry_days_min  = 1,           # ignore expiries sooner than this
    expiry_days_max  = 90,          # ignore expiries further than this
    vol_oi_ratio     = 2.5,         # vol > N× OI = unusual
    ask_skew_thresh  = 0.68,        # LTP this fraction toward ask = aggressive
    ltp_move_thresh  = 0.12,        # 12% move from open = premium surge
    min_oi           = 200,         # skip illiquid strikes
    min_notional     = 25_000,      # skip tiny contracts (OI × LTP)
    call_only        = False,       # set True to show only CE anomalies
    put_only         = False,       # set True to show only PE anomalies
    telegram_token   = "",          # optional — set via TELEGRAM_BOT_TOKEN env var
    telegram_chat_id = "",          # optional — set via TELEGRAM_CHAT_ID env var
)


# ─────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────
@dataclass
class Contract:
    symbol: str
    expiry: str           # "DD-Mon-YYYY"
    expiry_days: int      # calendar days to expiry
    strike: float
    opt_type: str         # "CE" or "PE"
    ltp: float
    open_price: float
    bid: float
    ask: float
    volume: int
    oi: int
    oi_change: int
    iv: float             # implied volatility from NSE (may be 0)

@dataclass
class Signal:
    contract: Contract
    signal_type: str      # VOL_OI_SPIKE | ASK_SKEW | PREMIUM_SURGE | COMBINED
    score: float
    reason: str


# ─────────────────────────────────────────────────────────────────
# WATCHLIST LOADER
# ─────────────────────────────────────────────────────────────────
def load_watchlist(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Watchlist file not found: {path}")
        sys.exit(1)
    symbols = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            symbols.append(line.upper())
    print(f"[INFO] Loaded {len(symbols)} symbols from {path}")
    return symbols


# ─────────────────────────────────────────────────────────────────
# NSE FETCHER
# ─────────────────────────────────────────────────────────────────
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT"}

def make_session() -> requests.Session:
    """NSE needs a live cookie from the homepage before it accepts API calls."""
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1.5)
        s.get("https://www.nseindia.com/market-data/equity-derivatives-watch", timeout=10)
        time.sleep(1)
    except Exception as e:
        print(f"[WARN] Session warm-up issue: {e}")
    return s

def fetch_chain(symbol: str, session: requests.Session,
                expiry_min: int, expiry_max: int) -> List[Contract]:
    """Fetch option chain for one symbol; return contracts filtered by expiry window."""
    if symbol in INDICES:
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    else:
        url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"

    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            print(f"[WARN] {symbol}: session expired, refreshing...")
            session = make_session()
            try:
                resp = session.get(url, timeout=15)
                data = resp.json()
            except Exception:
                print(f"[WARN] {symbol}: retry failed, skipping")
                return []
        else:
            print(f"[WARN] {symbol}: HTTP {resp.status_code}, skipping")
            return []
    except Exception as e:
        print(f"[WARN] {symbol}: {e}, skipping")
        return []

    today = datetime.date.today()
    contracts = []

    for item in data.get("records", {}).get("data", []):
        raw_expiry = item.get("expiryDate", "")
        try:
            expiry_date = datetime.datetime.strptime(raw_expiry, "%d-%b-%Y").date()
        except ValueError:
            continue

        days_to_expiry = (expiry_date - today).days
        if not (expiry_min <= days_to_expiry <= expiry_max):
            continue

        strike = float(item.get("strikePrice", 0))

        for opt_type in ("CE", "PE"):
            opt = item.get(opt_type)
            if not opt:
                continue

            ltp        = float(opt.get("lastPrice", 0) or 0)
            open_price = float(opt.get("openPrice", 0) or ltp)
            bid        = float(opt.get("bidprice", 0) or 0)
            ask        = float(opt.get("askPrice", 0) or 0)
            volume     = int(opt.get("totalTradedVolume", 0) or 0)
            oi         = int(opt.get("openInterest", 0) or 0)
            oi_change  = int(opt.get("changeinOpenInterest", 0) or 0)
            iv         = float(opt.get("impliedVolatility", 0) or 0)

            contracts.append(Contract(
                symbol=symbol,
                expiry=raw_expiry,
                expiry_days=days_to_expiry,
                strike=strike,
                opt_type=opt_type,
                ltp=ltp,
                open_price=open_price,
                bid=bid,
                ask=ask,
                volume=volume,
                oi=oi,
                oi_change=oi_change,
                iv=iv,
            ))

    return contracts


# ─────────────────────────────────────────────────────────────────
# ANOMALY SCORING ENGINE
# ─────────────────────────────────────────────────────────────────
def score(c: Contract, cfg: dict) -> Optional[Signal]:
    # Liquidity gates
    if c.oi < cfg["min_oi"]:
        return None
    if c.ltp * c.oi < cfg["min_notional"]:
        return None
    if c.ltp <= 0:
        return None

    pts = 0.0
    reasons = []

    # ── Signal 1: Vol / OI spike ─────────────────────────────────
    # Classic "someone is loading up" signal.
    # Far-dated options have lower normal vol, so the same ratio is MORE unusual.
    # We scale the score bonus by expiry distance.
    if c.oi > 0 and c.volume > 0:
        ratio = c.volume / c.oi
        if ratio >= cfg["vol_oi_ratio"]:
            base = min(40, (ratio / cfg["vol_oi_ratio"]) * 18)
            # bonus for far-dated expiry (institutional conviction)
            expiry_bonus = min(10, c.expiry_days / 10)
            pts += base + expiry_bonus
            reasons.append(f"Vol/OI={ratio:.1f}×")

    # ── Signal 2: Ask-side aggression ────────────────────────────
    # Buyers lifting the ask = urgency, not passive limit orders.
    spread = c.ask - c.bid
    if spread > 0 and c.bid > 0:
        ask_ratio = (c.ltp - c.bid) / spread
        if ask_ratio >= cfg["ask_skew_thresh"]:
            pts += ask_ratio * 28
            reasons.append(f"AskSkew={ask_ratio:.0%}")

    # ── Signal 3: Premium surge from open ────────────────────────
    if c.open_price > 0:
        move = (c.ltp - c.open_price) / c.open_price
        if move >= cfg["ltp_move_thresh"]:
            pts += min(25, move * 90)
            reasons.append(f"Premium+{move:.0%}")

    # ── Signal 4: OI build-up (new positions opening) ────────────
    # Large positive OI change alongside volume = fresh money, not closing.
    if c.oi_change > 0 and c.oi > 0:
        oi_build = c.oi_change / c.oi
        if oi_build >= 0.15:   # OI grew 15%+ today
            pts += min(15, oi_build * 50)
            reasons.append(f"OI+{oi_build:.0%}")

    if pts < cfg["min_score"]:
        return None

    score_val = min(100.0, pts)
    n_signals = len(reasons)
    if n_signals >= 3 or score_val >= 70:
        sig_type = "COMBINED"
    elif "Vol/OI" in " ".join(reasons):
        sig_type = "VOL_OI_SPIKE"
    elif "AskSkew" in " ".join(reasons):
        sig_type = "ASK_SKEW"
    elif "OI+" in " ".join(reasons):
        sig_type = "OI_BUILDUP"
    else:
        sig_type = "PREMIUM_SURGE"

    return Signal(contract=c, signal_type=sig_type, score=score_val,
                  reason=" · ".join(reasons))


# ─────────────────────────────────────────────────────────────────
# REPORT GENERATION
# ─────────────────────────────────────────────────────────────────
def signals_to_df(signals: List[Signal]) -> pd.DataFrame:
    rows = []
    for s in signals:
        c = s.contract
        rows.append({
            "Symbol":       c.symbol,
            "Expiry":       c.expiry,
            "Days Left":    c.expiry_days,
            "Strike":       c.strike,
            "Type":         c.opt_type,
            "LTP (₹)":      c.ltp,
            "Open (₹)":     c.open_price,
            "Volume":       c.volume,
            "OI":           c.oi,
            "OI Change":    c.oi_change,
            "IV (%)":       c.iv,
            "Signal":       s.signal_type,
            "Score":        round(s.score, 1),
            "Reason":       s.reason,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Score", "Days Left"], ascending=[False, True])
    return df


def save_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False)
    print(f"[SAVED] CSV  → {path}")


def save_html(df: pd.DataFrame, path: str, run_time: str,
              cfg: dict, total_scanned: int):
    if df.empty:
        html_table = "<p style='color:#888'>No anomalies detected in this run.</p>"
    else:
        # Color-code rows by signal type and score
        def row_style(row):
            base = ""
            if row["Score"] >= 75:
                base = "background:#2a1a0a;"
            elif row["Score"] >= 50:
                base = "background:#1a1a2a;"
            return [base] * len(row)

        styled = df.style \
            .apply(row_style, axis=1) \
            .format({
                "LTP (₹)":   "₹{:.2f}",
                "Open (₹)":  "₹{:.2f}",
                "Volume":    "{:,.0f}",
                "OI":        "{:,.0f}",
                "OI Change": "{:+,.0f}",
                "IV (%)":    "{:.1f}",
                "Score":     "{:.1f}",
            }) \
            .bar(subset=["Score"], color="#ff6b35", vmin=0, vmax=100) \
            .set_table_styles([
                {"selector": "thead th", "props": [
                    ("background", "#1e1e2e"), ("color", "#cdd6f4"),
                    ("padding", "10px 14px"), ("font-size", "13px"),
                    ("border-bottom", "2px solid #313244"),
                ]},
                {"selector": "tbody td", "props": [
                    ("padding", "8px 14px"), ("font-size", "13px"),
                    ("color", "#cdd6f4"), ("border-bottom", "1px solid #313244"),
                ]},
                {"selector": "table", "props": [
                    ("border-collapse", "collapse"), ("width", "100%"),
                ]},
            ])

        html_table = styled.to_html()

    # Build summary stats
    n_signals = len(df)
    n_combined = len(df[df["Signal"] == "COMBINED"]) if not df.empty else 0
    n_ce = len(df[df["Type"] == "CE"]) if not df.empty else 0
    n_pe = len(df[df["Type"] == "PE"]) if not df.empty else 0
    top_symbols = (
        df["Symbol"].value_counts().head(5).to_dict()
        if not df.empty else {}
    )
    top_sym_str = ", ".join(f"{k} ({v})" for k, v in top_symbols.items()) or "—"

    expiry_filter = f"{cfg['expiry_days_min']}–{cfg['expiry_days_max']} days"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Options Flow Report — {run_time}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #11111b; color: #cdd6f4; padding: 24px;
    line-height: 1.5;
  }}
  h1 {{ font-size: 22px; font-weight: 600; color: #cba6f7; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: #6c7086; margin-bottom: 24px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }}
  .card {{
    background: #1e1e2e; border: 1px solid #313244;
    border-radius: 10px; padding: 16px 20px; min-width: 140px;
  }}
  .card .label {{ font-size: 11px; color: #6c7086; text-transform: uppercase;
                  letter-spacing: .06em; margin-bottom: 4px; }}
  .card .value {{ font-size: 26px; font-weight: 700; color: #cba6f7; }}
  .card .sub {{ font-size: 11px; color: #6c7086; margin-top: 2px; }}
  .section-title {{
    font-size: 15px; font-weight: 600; color: #a6adc8;
    margin: 28px 0 12px; border-left: 3px solid #cba6f7;
    padding-left: 10px;
  }}
  .config-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 8px; margin-bottom: 28px;
  }}
  .config-item {{
    background: #181825; border: 1px solid #313244; border-radius: 6px;
    padding: 8px 12px; font-size: 12px; color: #a6adc8;
  }}
  .config-item span {{ color: #cba6f7; font-weight: 600; }}
  .table-wrap {{ overflow-x: auto; border-radius: 10px;
                 border: 1px solid #313244; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600;
  }}
  .badge-combined  {{ background: #ff6b3522; color: #ff6b35; }}
  .badge-vol       {{ background: #a6e3a122; color: #a6e3a1; }}
  .badge-ask       {{ background: #89dceb22; color: #89dceb; }}
  .badge-oi        {{ background: #f9e2af22; color: #f9e2af; }}
  .badge-premium   {{ background: #cba6f722; color: #cba6f7; }}
  footer {{ margin-top: 40px; font-size: 11px; color: #45475a; text-align: center; }}
</style>
</head>
<body>

<h1>🔥 Indian Options — Unusual Flow Report</h1>
<div class="subtitle">
  Generated: {run_time} IST &nbsp;·&nbsp;
  Expiry window: {expiry_filter} &nbsp;·&nbsp;
  Contracts scanned: {total_scanned:,}
</div>

<div class="cards">
  <div class="card">
    <div class="label">Signals</div>
    <div class="value">{n_signals}</div>
    <div class="sub">above score {cfg["min_score"]}</div>
  </div>
  <div class="card">
    <div class="label">Combined</div>
    <div class="value">{n_combined}</div>
    <div class="sub">multi-signal hits</div>
  </div>
  <div class="card">
    <div class="label">Calls (CE)</div>
    <div class="value" style="color:#a6e3a1">{n_ce}</div>
    <div class="sub">bullish flow</div>
  </div>
  <div class="card">
    <div class="label">Puts (PE)</div>
    <div class="value" style="color:#f38ba8">{n_pe}</div>
    <div class="sub">bearish flow</div>
  </div>
  <div class="card" style="min-width:220px">
    <div class="label">Top symbols</div>
    <div class="value" style="font-size:14px;margin-top:4px">{top_sym_str}</div>
  </div>
</div>

<div class="section-title">Run configuration</div>
<div class="config-grid">
  <div class="config-item">Min score: <span>{cfg["min_score"]}</span></div>
  <div class="config-item">Expiry window: <span>{expiry_filter}</span></div>
  <div class="config-item">Vol/OI threshold: <span>{cfg["vol_oi_ratio"]}×</span></div>
  <div class="config-item">Ask skew: <span>{cfg["ask_skew_thresh"]:.0%}</span></div>
  <div class="config-item">Min LTP move: <span>{cfg["ltp_move_thresh"]:.0%}</span></div>
  <div class="config-item">Min OI: <span>{cfg["min_oi"]:,}</span></div>
</div>

<div class="section-title">Anomaly signals</div>
<div class="table-wrap">
{html_table}
</div>

<footer>
  Options flow scanner · NSE data (15-min delayed on free feed) ·
  Not financial advice · For informational use only
</footer>
</body>
</html>"""

    Path(path).write_text(html, encoding="utf-8")
    print(f"[SAVED] HTML → {path}")


# ─────────────────────────────────────────────────────────────────
# TELEGRAM ALERT (optional)
# ─────────────────────────────────────────────────────────────────
def telegram_summary(signals: List[Signal], cfg: dict, run_time: str):
    token    = cfg["telegram_token"] or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id  = cfg["telegram_chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    top = sorted(signals, key=lambda s: s.score, reverse=True)[:10]
    lines = [f"🔥 *Options Flow Report* — {run_time} IST",
             f"Signals: {len(signals)} | Top 10 below\n"]

    for s in top:
        c = s.contract
        icon = "🟢" if c.opt_type == "CE" else "🔴"
        lines.append(
            f"{icon} `{c.symbol}` {c.strike}{c.opt_type}  "
            f"Exp: {c.expiry} ({c.expiry_days}d)\n"
            f"   Score: *{s.score:.0f}*  {s.signal_type}\n"
            f"   LTP: ₹{c.ltp}  Vol: {c.volume:,}  OI: {c.oi:,}\n"
            f"   {s.reason}"
        )

    msg = "\n".join(lines)
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10,
        )
        print(f"[INFO] Telegram summary sent ({len(top)} signals)")
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Indian options anomaly scanner")
    p.add_argument("--watchlist",     default=DEFAULTS["watchlist_path"])
    p.add_argument("--output",        default=DEFAULTS["output_dir"])
    p.add_argument("--min-score",     type=float, default=DEFAULTS["min_score"])
    p.add_argument("--expiry-days",   type=int, nargs=2,
                   default=[DEFAULTS["expiry_days_min"], DEFAULTS["expiry_days_max"]],
                   metavar=("MIN", "MAX"),
                   help="Expiry window in days, e.g. --expiry-days 1 30")
    p.add_argument("--vol-oi-ratio",  type=float, default=DEFAULTS["vol_oi_ratio"])
    p.add_argument("--call-only",     action="store_true")
    p.add_argument("--put-only",      action="store_true")
    p.add_argument("--telegram-token",   default=DEFAULTS["telegram_token"])
    p.add_argument("--telegram-chat-id", default=DEFAULTS["telegram_chat_id"])
    return p.parse_args()


def main():
    args = parse_args()

    cfg = {
        **DEFAULTS,
        "watchlist_path":   args.watchlist,
        "output_dir":       args.output,
        "min_score":        args.min_score,
        "expiry_days_min":  args.expiry_days[0],
        "expiry_days_max":  args.expiry_days[1],
        "vol_oi_ratio":     args.vol_oi_ratio,
        "call_only":        args.call_only,
        "put_only":         args.put_only,
        "telegram_token":   args.telegram_token,
        "telegram_chat_id": args.telegram_chat_id,
    }

    # ── Setup ──────────────────────────────────────────────────
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    now_ist  = datetime.datetime.now(IST)
    run_time = now_ist.strftime("%Y-%m-%d %H:%M")
    stamp    = now_ist.strftime("%Y%m%d_%H%M")

    symbols  = load_watchlist(cfg["watchlist_path"])
    session  = make_session()

    # ── Scan ───────────────────────────────────────────────────
    all_signals: List[Signal] = []
    total_contracts = 0

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] Scanning {symbol} ...", end=" ", flush=True)
        contracts = fetch_chain(
            symbol, session,
            cfg["expiry_days_min"], cfg["expiry_days_max"]
        )

        # Apply CE/PE filter
        if cfg["call_only"]:
            contracts = [c for c in contracts if c.opt_type == "CE"]
        elif cfg["put_only"]:
            contracts = [c for c in contracts if c.opt_type == "PE"]

        total_contracts += len(contracts)
        hits = 0
        for contract in contracts:
            sig = score(contract, cfg)
            if sig:
                all_signals.append(sig)
                hits += 1

        print(f"{len(contracts)} contracts → {hits} signals")
        # Polite delay between symbols so NSE doesn't block us
        if i < len(symbols):
            time.sleep(2.5)

    print(f"\n[DONE] {len(all_signals)} signals from {total_contracts:,} contracts")

    # ── Save reports ───────────────────────────────────────────
    df = signals_to_df(all_signals)

    csv_path  = out_dir / f"flow_{stamp}.csv"
    html_path = out_dir / f"flow_{stamp}.html"
    latest_html = out_dir / "latest.html"
    latest_csv  = out_dir / "latest.csv"

    save_csv(df, str(csv_path))
    save_html(df, str(html_path), run_time, cfg, total_contracts)

    # Always overwrite "latest" so you have a fixed URL to bookmark
    save_csv(df, str(latest_csv))
    save_html(df, str(latest_html), run_time, cfg, total_contracts)

    # ── Telegram summary ───────────────────────────────────────
    if all_signals:
        telegram_summary(all_signals, cfg, run_time)

    print(f"\n✅ Report ready: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
