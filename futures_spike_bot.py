import os
import asyncio
import time
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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))
PERCENT_THRESHOLD = float(os.getenv("PERCENT_THRESHOLD", "3.5"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "12"))
SYMBOL_WHITELIST = os.getenv("SYMBOL_WHITELIST", "")

last_alerted_candle = {}

async def fetch_json(session, url, params=None):
    async with session.get(url, params=params, timeout=20) as resp:
        resp.raise_for_status()
        return await resp.json()

async def get_usdt_perpetual_symbols(session) -> List[str]:
    data = await fetch_json(session, BINANCE_FAPI + EXCHANGE_INFO)
    symbols = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        name = s.get("symbol")
        if name and name.endswith("USDT"):
            symbols.append(name)
    if SYMBOL_WHITELIST:
        allowed = {x.strip().upper() for x in SYMBOL_WHITELIST.split(",") if x.strip()}
        symbols = [s for s in symbols if s in allowed]
    return symbols

async def get_last_1m_candle(session, symbol: str):
    params = {"symbol": symbol, "interval": "1m", "limit": 2}

    data = await fetch_json(session, BINANCE_FAPI + KLINE_ENDPOINT, params=params)
    if not data:
        return None
    k = data[-2] if len(data) >= 2 else data[-1]
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": int(k[6]),
    }

async def send_telegram(session, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram config missing")
        return
    url = f"{TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "MarkdownV2"}
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            txt = await resp.text()
            print("Telegram error", resp.status, txt)

def percent_change(open_p, close_p):
    return 0.0 if open_p == 0 else (close_p - open_p) / open_p * 100.0

async def check_symbol(session, symbol: str):
    try:
        candle = await get_last_1m_candle(session, symbol)
        if not candle:
            return
        ot = candle["open_time"]
        if last_alerted_candle.get(symbol) == ot:
            return
        ch = percent_change(candle["open"], candle["close"])
        if abs(ch) >= PERCENT_THRESHOLD:
            direction = "UP" if ch > 0 else "DOWN"
            msg = (
                f"*Spike detected* `{symbol}`\n"
                f"Direction: *{direction}*\n"
                f"Open: `{candle['open']}` Close: `{candle['close']}`\n"
                f"Change: `{ch:.2f}%` in 1m\n"
                f"High/Low: `{candle['high']}` / `{candle['low']}`\n"
                f"Volume: `{candle['volume']}`"
            )
            await send_telegram(session, msg)
            last_alerted_candle[symbol] = ot
            print(f"Alert {symbol} {ch:.2f}%")
    except Exception as e:
        print(f"Error checking {symbol}: {e}")

async def main_loop():
    connector = aiohttp.TCPConnector(limit_per_host=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        symbols = await get_usdt_perpetual_symbols(session)
        print(f"Found {len(symbols)} USDT symbols (sample): {symbols[:12]}")
        if not symbols:
            print("No symbols found. Exiting.")
            return
        sem = asyncio.Semaphore(CONCURRENCY)
        while True:
            start = time.time()
            tasks = []
            for sym in symbols:
                async def run(s=sym):
                    async with sem:
                        await check_symbol(session, s)
                tasks.append(asyncio.create_task(run()))
            await asyncio.gather(*tasks, return_exceptions=True)
            elapsed = time.time() - start
            await asyncio.sleep(max(1, CHECK_INTERVAL - elapsed))

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Stopped by user")
