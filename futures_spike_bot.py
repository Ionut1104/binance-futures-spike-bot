import os
import asyncio
import time
import re
from datetime import datetime
from typing import List
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BINANCE_FAPI = "https://fapi.binance.com"
KLINE_ENDPOINT = "/fapi/v1/klines"
EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
TELEGRAM_API = "https://api.telegram.org"

TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "")
CONCURRENCY = int(os.getenv("CONCURRENCY", "15"))

# === Config personalizat ===
ONE_MIN_THRESHOLD = 3.5     # % pentru 1 minut
FIVE_MIN_THRESHOLD = 5.0    # % pentru 5 minute
ONE_MIN_INTERVAL = 30       # secunde între verificări 1m
FIVE_MIN_INTERVAL = 240     # secunde între verificări 5m (4 min)

last_alerted_1m = {}
last_alerted_5m = {}

# ========== FUNCTII UTILE ==========

def log(msg: str):
    """Afișează mesaj cu oră în consolă."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def escape_md(text: str) -> str:
    """Curăță textul pentru Telegram MarkdownV2."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def now_str() -> str:
    """Returnează ora UTC pentru mesaje."""
    return datetime.utcnow().strftime("%H:%M:%S UTC")

def percent_change(o, c):
    return 0.0 if o == 0 else (c - o) / o * 100.0

# ========== REQUEST BINANCE ==========

async def fetch_json(session, url, params=None, retries=2):
    """Fetch JSON cu retry automat la erori Binance."""
    for i in range(retries + 1):
        try:
            async with session.get(url, params=params, timeout=20) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            if i == retries:
                log(f"Fetch error {url}: {e}")
                return None
            await asyncio.sleep(1)

async def get_usdt_perpetual_symbols(session) -> List[str]:
    data = await fetch_json(session, BINANCE_FAPI + EXCHANGE_INFO)
    return [
        s["symbol"] for s in data.get("symbols", [])
        if s.get("status") == "TRADING" and s["symbol"].endswith("USDT")
    ]

async def get_last_candle(session, symbol: str, interval: str):
    params = {"symbol": symbol, "interval": interval, "limit": 2}
    data = await fetch_json(session, BINANCE_FAPI + KLINE_ENDPOINT, params=params)
    if not data:
        return None
    k = data[-1]  # ✅ Folosește lumânarea curentă (semnal instant)
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
    }

# ========== TELEGRAM ==========

async def send_telegram(session, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram config missing")
        return
    url = f"{TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": escape_md(text),
        "parse_mode": "MarkdownV2",
    }
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            txt = await resp.text()
            log(f"Telegram error {resp.status}: {txt}")

# ========== VERIFICARE 1 MINUT ==========

async def check_1m(session, symbol):
    try:
        c = await get_last_candle(session, symbol, "1m")
        if not c:
            return
        ot = c["open_time"]
        if last_alerted_1m.get(symbol) == ot:
            return
        ch = percent_change(c["open"], c["close"])
        if abs(ch) >= ONE_MIN_THRESHOLD:
            dir = "UP" if ch > 0 else "DOWN"
            msg = (
                f"⚡ *1m Spike* `{symbol}`\n"
                f"🕒 {now_str()}\n"
                f"Direction: *{dir}*\n"
                f"Change: `{ch:.2f}%` in 1m\n"
                f"Open: `{c['open']}` → Close: `{c['close']}`"
            )
            await send_telegram(session, msg)
            last_alerted_1m[symbol] = ot
            log(f"[1m] {symbol}: {ch:.2f}% {dir}")
    except Exception as e:
        log(f"Error 1m {symbol}: {e}")

# ========== VERIFICARE 5 MINUTE ==========

async def check_5m(session, symbol):
    try:
        c = await get_last_candle(session, symbol, "5m")
        if not c:
            return
        ot = c["open_time"]
        if last_alerted_5m.get(symbol) == ot:
            return
        ch = percent_change(c["open"], c["close"])
        if abs(ch) >= FIVE_MIN_THRESHOLD:
            dir = "UP" if ch > 0 else "DOWN"
            msg = (
                f"🚀 *5m Spike* `{symbol}`\n"
                f"🕒 {now_str()}\n"
                f"Direction: *{dir}*\n"
                f"Change: `{ch:.2f}%` in 5m\n"
                f"Open: `{c['open']}` → Close: `{c['close']}`"
            )
            await send_telegram(session, msg)
            last_alerted_5m[symbol] = ot
            log(f"[5m] {symbol}: {ch:.2f}% {dir}")
    except Exception as e:
        log(f"Error 5m {symbol}: {e}")

# ========== LOOPURI PARALELE ==========

async def run_check(session, s, func, sem):
    async with sem:
        await func(session, s)
        await asyncio.sleep(0.05)  # ✅ anti-rate-limit Binance

async def monitor_1m(symbols, session):
    sem = asyncio.Semaphore(CONCURRENCY)
    while True:
        start = time.time()
        tasks = [asyncio.create_task(run_check(session, s, check_1m, sem)) for s in symbols]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(max(1, ONE_MIN_INTERVAL - (time.time() - start)))

async def monitor_5m(symbols, session):
    sem = asyncio.Semaphore(CONCURRENCY)
    while True:
        start = time.time()
        tasks = [asyncio.create_task(run_check(session, s, check_5m, sem)) for s in symbols]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(max(1, FIVE_MIN_INTERVAL - (time.time() - start)))

# ========== MAIN ==========

async def main():
    connector = aiohttp.TCPConnector(limit_per_host=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        symbols = await get_usdt_perpetual_symbols(session)
        log(f"✅ Monitoring {len(symbols)} USDT symbols (1m & 5m)...")
        await send_telegram(session, f"🤖 Bot started: monitoring {len(symbols)} pairs (1m + 5m).")
        await asyncio.gather(
            monitor_1m(symbols, session),
            monitor_5m(symbols, session),
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Stopped by user")
