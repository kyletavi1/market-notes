#!/usr/bin/env python3
"""
Fetches market data for Kyle's tickers, writes data.json for the dashboard,
and sends urgent alerts to a private ntfy topic.

Runs on GitHub Actions every ~15 minutes during US market hours.
No personal data (share counts, cost basis) ever touches this repo —
only ticker symbols and public market data.
"""

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TICKERS = ["ARTY", "AAPL", "VUG", "QQQ", "SDY", "SWPPX"]
BENCHMARK = "SPY"  # S&P 500 tracker used for "vs the market" comparisons

NAMES = {
    "ARTY": "iShares Future AI & Tech ETF",
    "AAPL": "Apple Inc.",
    "VUG": "Vanguard Growth ETF",
    "QQQ": "Invesco QQQ (Nasdaq-100)",
    "SDY": "SPDR S&P Dividend ETF",
    "SWPPX": "Schwab S&P 500 Index Fund",
    "SPY": "S&P 500 (the market)",
}

UA = {"User-Agent": "Mozilla/5.0 (personal portfolio dashboard)"}


def get_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == retries - 1:
                print(f"WARN: failed {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def get_text(url, retries=2):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode(errors="replace")
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(2)


def fetch_history(symbol):
    """5 years of daily closes + latest quote from Yahoo Finance."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range=5y&interval=1d&events=div")
    data = get_json(url)
    try:
        res = data["chart"]["result"][0]
        meta = res["meta"]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        return {
            "symbol": symbol,
            "name": NAMES.get(symbol, meta.get("longName", symbol)),
            "price": meta.get("regularMarketPrice"),
            # NOTE: chartPreviousClose is relative to the 5y range start — wrong for
            # daily change. Use the true previous close instead.
            "prevClose": meta.get("regularMarketPreviousClose")
                         or (pairs[-2][1] if len(pairs) >= 2 else None),
            "currency": meta.get("currency", "USD"),
            "marketState": meta.get("marketState", ""),
            "dates": [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
                      for t, _ in pairs],
            "closes": [round(c, 4) for _, c in pairs],
        }
    except (TypeError, KeyError, IndexError) as e:
        print(f"WARN: bad history for {symbol}: {e}", file=sys.stderr)
        return None


def fetch_news(symbol, limit=6):
    """Recent headlines + snippets from Yahoo Finance RSS."""
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    xml = get_text(url)
    if not xml:
        return []
    items = []
    try:
        root = ET.fromstring(xml)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if title and link:
                items.append({"title": title, "link": link, "date": pub, "desc": desc})
            if len(items) >= limit:
                break
    except ET.ParseError:
        pass
    return items


def build_overview(items):
    """Distill RSS snippets into a short plain-text overview paragraph."""
    import html as htmllib
    sents = []
    for it in items[:5]:
        txt = re.sub(r"<[^>]+>", "", it.get("desc") or "")
        txt = htmllib.unescape(txt).strip()
        if not txt:
            continue
        first = re.split(r"(?<=[.!?])\s", txt)[0].strip()
        if 30 < len(first) < 300 and first not in sents:
            sents.append(first)
        if len(sents) >= 3:
            break
    return " ".join(sents)


# ---------------- alerts ----------------

def send_ntfy(topic, title, message, priority="high", tags="chart_with_downwards_trend"):
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=15)
        print(f"ALERT SENT: {title}")
    except Exception as e:
        print(f"WARN: ntfy failed: {e}", file=sys.stderr)


def pct(a, b):
    return (a - b) / b * 100 if b else 0.0


def check_alerts(quotes, state):
    """Price-based urgency rules. Deduped per condition per day."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("No NTFY_TOPIC secret set; skipping alerts.")
        return state

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sent = state.get(today, [])

    def fire(key, title, msg, tags):
        if key in sent:
            return
        send_ntfy(topic, title, msg, tags=tags)
        sent.append(key)

    spy = quotes.get(BENCHMARK)
    if spy and spy["price"] and spy["prevClose"]:
        m = pct(spy["price"], spy["prevClose"])
        if m <= -2.5:
            fire("mkt_down", f"Market alert: stocks broadly down {abs(m):.1f}%",
                 "The overall market (S&P 500) is having an unusually bad day. "
                 "Your funds will likely be down too. This is market-wide, "
                 "not something wrong with your specific picks.",
                 "rotating_light")
        elif m >= 2.5:
            fire("mkt_up", f"Market alert: stocks broadly up {m:.1f}%",
                 "The overall market is having an unusually strong day.",
                 "chart_with_upwards_trend")

    for sym, q in quotes.items():
        if sym == BENCHMARK or not q or not q["price"] or not q["prevClose"]:
            continue
        move = pct(q["price"], q["prevClose"])
        name = NAMES.get(sym, sym)
        if move <= -5:
            fire(f"{sym}_bigdown", f"{sym} is down {abs(move):.1f}% today",
                 f"{name} has dropped sharply today. Worth checking the news tab "
                 f"in your dashboard to see why before reacting.",
                 "warning")
        elif move >= 5:
            fire(f"{sym}_bigup", f"{sym} is up {move:.1f}% today",
                 f"{name} has jumped sharply today.",
                 "tada")
        # drop from recent high (last ~7 trading days)
        closes = q.get("closes") or []
        if len(closes) >= 7 and q["price"]:
            recent_high = max(closes[-7:])
            dd = pct(q["price"], recent_high)
            if dd <= -8:
                fire(f"{sym}_drawdown", f"{sym}: down {abs(dd):.1f}% from last week's high",
                     f"{name} has fallen notably over recent days, not just today.",
                     "warning")

    # keep only today's dedupe records
    return {today: sent}


def main():
    quotes = {}
    for sym in TICKERS + [BENCHMARK]:
        h = fetch_history(sym)
        if h:
            quotes[sym] = h
        time.sleep(0.5)

    if not quotes:
        print("ERROR: no data fetched at all; keeping previous data.json", file=sys.stderr)
        sys.exit(1)

    news = {sym: fetch_news(sym) for sym in TICKERS}

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "preview": False,
        "benchmark": BENCHMARK,
        "tickers": {},
    }
    for sym, q in quotes.items():
        items = news.get(sym, [])
        out["tickers"][sym] = {
            "name": q["name"],
            "price": q["price"],
            "prevClose": q["prevClose"],
            "marketState": q["marketState"],
            "dates": q["dates"],
            "closes": q["closes"],
            "overview": build_overview(items),
            "news": [{"title": n["title"], "link": n["link"], "date": n["date"]}
                     for n in items],
        }

    (ROOT / "data.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote data.json with {len(quotes)} symbols.")

    state_file = ROOT / "alert_state.json"
    try:
        state = json.loads(state_file.read_text())
    except Exception:
        state = {}
    state = check_alerts(quotes, state)
    state_file.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
