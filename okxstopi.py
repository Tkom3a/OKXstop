#!/usr/bin/env python3
"""
okxstopi.py
Emergency OKX watchdog for MoonTrader.
Use at your own risk and test on demo/small account first.
"""

import os
import time
import json
import hmac
import base64
import hashlib
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WATCHDOG_TIMEOUT = int(os.getenv("WATCHDOG_TIMEOUT", "120"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))  # How often to check orders
STARTUP_GRACE = int(os.getenv("STARTUP_GRACE", "30"))
REPORT_TZ = ZoneInfo(os.getenv("REPORT_TIMEZONE", "UTC"))
PANIC_THRESHOLD = int(os.getenv("PANIC_THRESHOLD", "3"))  # Максимум паник до "сна"

_now = time.time()

class State:
    started_at = _now
    last_order_activity = _now
    last_check_time = _now
    panic_events = 0
    last_report_time = _now
    last_report_balance = 0.0
    last_hour_reported = None
    orders_cache = []  # Cache for order comparison
    last_orders_count = 0
    is_sleeping = False  # Режим сна после превышения паник
    sleep_start_time = None  # Когда начался сон
    has_activity_since_sleep = False  # Была ли активность во время сна

state = State()

def log(msg):
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts_now}] {msg}", flush=True)

def mark_order_activity():
    state.last_order_activity = time.time()
    # Если бот был в режиме сна и появилась активность - пробуждаемся
    if state.is_sleeping:
        state.is_sleeping = False
        state.sleep_start_time = None
        state.panic_events = 0
        state.has_activity_since_sleep = False
        log("🔄 ACTIVITY DETECTED - OKXSTOPI RESUMED (woke from sleep)")
        asyncio.create_task(tg("🟢 OKXSTOPI RESUMED\n\nActivity detected - Watchdog is active again!"))
    log("Order activity detected")

def ts():
    """Generate timestamp for REST API with milliseconds"""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def sign(timestamp, method, path, body=''):
    msg = timestamp + method.upper() + path + body
    mac = hmac.new(
        OKX_SECRET_KEY.encode(),
        msg.encode(),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

async def okx_request(method, endpoint, payload=None, params=None):
    query = ""
    if params:
        query = "?" + urlencode(
            {k: str(v) for k, v in params.items() if v is not None}
        )

    path = endpoint + query
    body = json.dumps(payload) if payload else ""
    timestamp = ts()

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign(timestamp, method, path, body),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        if method == "GET":
            async with session.get(
                f"https://www.okx.com{path}",
                headers=headers
            ) as r:
                return await r.json()

        async with session.post(
            f"https://www.okx.com{path}",
            headers=headers,
            json=payload
        ) as r:
            return await r.json()

async def tg(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    # Если бот в режиме сна и это не сообщение о пробуждении - пропускаем уведомления
    if state.is_sleeping and "RESUMED" not in msg and "STARTED" not in msg:
        log(f"💤 Telegram notification suppressed (sleeping mode): {msg[:50]}...")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg
            })
    except Exception as e:
        log(f"Failed to send Telegram message: {e}")

async def get_balance():
    data = await okx_request("GET", "/api/v5/account/balance")

    try:
        for d in data["data"][0]["details"]:
            if d["ccy"] == "USDT":
                return float(d["eq"])
    except Exception as e:
        log(f"Error getting balance: {e}")

    return 0.0

async def get_pending_orders():
    data = await okx_request("GET", "/api/v5/trade/orders-pending")
    return data.get("data", [])

async def get_orders_history(before_ms=None):
    """Get recent order history to detect activity"""
    params = {
        "instType": "SWAP",
        "limit": 50
    }
    if before_ms:
        params["before"] = before_ms
    
    data = await okx_request("GET", "/api/v5/trade/orders-history", params=params)
    return data.get("data", [])

async def get_positions():
    data = await okx_request("GET", "/api/v5/account/positions")
    return [p for p in data.get("data", []) if float(p.get("pos", "0")) != 0]

async def get_fills_since(begin_ms, end_ms):
    fills = []
    after = None

    while True:
        params = {
            "begin": begin_ms,
            "end": end_ms,
            "limit": 100,
        }
        if after:
            params["after"] = after

        data = await okx_request("GET", "/api/v5/trade/fills", params=params)

        if data.get("code") != "0":
            break

        batch = data.get("data", [])
        if not batch:
            break

        fills.extend(batch)

        if len(batch) < 100:
            break

        after = batch[-1]["tradeId"]

    return fills

def aggregate_fills(fills):
    realized = 0.0
    fees = 0.0
    volume = 0.0

    for f in fills:
        realized += float(f.get("fillPnl") or 0)
        fees += float(f.get("fee") or 0)
        volume += float(f.get("fillSz") or 0) * float(f.get("fillPx") or 0)

    return len(fills), realized, fees, volume

def fmt_pnl(value):
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}"

def fmt_utc(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, tz=REPORT_TZ).strftime(
        "%Y-%m-%d %H:%M %Z"
    )

def get_current_hour():
    """Get current hour as string for tracking"""
    return datetime.now(REPORT_TZ).strftime("%Y-%m-%d %H:00")

def seconds_until_next_hour():
    now = datetime.now(REPORT_TZ)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(0, (next_hour - now).total_seconds())

async def cancel_all_orders():
    orders = await get_pending_orders()

    count = 0

    for o in orders:
        payload = {
            "instId": o["instId"],
            "ordId": o["ordId"]
        }

        r = await okx_request(
            "POST",
            "/api/v5/trade/cancel-order",
            payload
        )

        if r.get("code") == "0":
            count += 1

    return count

async def close_position(pos):
    body = {
        "instId": pos["instId"],
        "mgnMode": "isolated",
        "autoCxl": True
    }

    pos_side = pos.get("posSide", "")

    if pos_side and pos_side != "net":
        body["posSide"] = pos_side

    return await okx_request(
        "POST",
        "/api/v5/trade/close-position",
        body
    )

async def close_all_positions():
    positions = await get_positions()
    closed = 0

    for p in positions:
        r = await close_position(p)

        if r.get("code") == "0":
            closed += 1

    await asyncio.sleep(5)

    positions = await get_positions()

    for p in positions:
        r = await close_position(p)

        if r.get("code") == "0":
            closed += 1

    return closed

async def enter_sleep_mode():
    """Переход в режим сна после превышения порога паник"""
    state.is_sleeping = True
    state.sleep_start_time = time.time()
    state.has_activity_since_sleep = False
    
    sleep_msg = (
        f"💤 OKXSTOPI ENTERING SLEEP MODE\n\n"
        f"Panic events: {state.panic_events} (threshold: {PANIC_THRESHOLD})\n"
        f"Watchdog is now sleeping until new trading activity is detected.\n"
        f"No more panic notifications will be sent.\n\n"
        f"Current balance: {state.last_report_balance:.2f} USDT"
    )
    
    await tg(sleep_msg)
    log("💤 OKXSTOPI entering sleep mode - no more panic notifications")

async def panic(reason):
    state.panic_events += 1
    
    # Проверяем, не достигли ли порога паник
    if state.panic_events >= PANIC_THRESHOLD:
        # Если паник уже много - переходим в сон вместо новой паники
        if not state.is_sleeping:
            await enter_sleep_mode()
        return

    cancelled = await cancel_all_orders()
    closed = await close_all_positions()
    bal = await get_balance()
    remaining = PANIC_THRESHOLD - state.panic_events

    await tg(
        f"🚨 OKXSTOPI PANIC #{state.panic_events}\n\n"
        f"{reason}\n\n"
        f"Orders cancelled: {cancelled}\n"
        f"Positions closed: {closed}\n"
        f"Balance: {bal:.2f} USDT\n"
        f"Remaining panics until sleep: {remaining}"
    )

    # Если это была последняя паника до порога - готовимся ко сну
    if state.panic_events >= PANIC_THRESHOLD:
        await enter_sleep_mode()

async def startup_report():
    bal = await get_balance()
    orders = len(await get_pending_orders())
    pos = len(await get_positions())

    state.last_report_time = time.time()
    state.last_report_balance = bal
    state.last_hour_reported = get_current_hour()
    state.last_orders_count = orders

    await tg(
        f"🟢 OKXSTOPI STARTED\n\n"
        f"Balance: {bal:.2f} USDT\n"
        f"Orders: {orders}\n"
        f"Positions: {pos}\n"
        f"Timeout: {WATCHDOG_TIMEOUT}s\n"
        f"Check interval: {CHECK_INTERVAL}s\n"
        f"Panic threshold: {PANIC_THRESHOLD} (sleep mode after)"
    )

async def build_hourly_report():
    now = time.time()
    begin_ms = int(state.last_report_time * 1000)
    end_ms = int(now * 1000)
    period_start = fmt_utc(state.last_report_time)
    period_end = fmt_utc(now)

    fills = await get_fills_since(begin_ms, end_ms)
    trades, realized, fees, volume = aggregate_fills(fills)
    net_pnl = realized + fees

    bal = await get_balance()
    bal_change = bal - state.last_report_balance
    orders = len(await get_pending_orders())
    positions = await get_positions()
    unrealized = sum(float(p.get("upl") or 0) for p in positions)

    idle = int(now - state.last_order_activity)

    status_icon = "💤 SLEEPING" if state.is_sleeping else ("✅ OK" if idle < WATCHDOG_TIMEOUT else "⚠️ WARNING")
    
    report = (
        f"📊 OKXSTOPI HOURLY REPORT\n\n"
        f"Period: {period_start} — {period_end}\n\n"
        f"Trades: {trades}\n"
        f"Volume: {volume:.2f} USDT\n"
        f"Realized PnL: {fmt_pnl(realized)} USDT\n"
        f"Fees: {fmt_pnl(fees)} USDT\n"
        f"Net PnL: {fmt_pnl(net_pnl)} USDT\n"
        f"Balance change: {fmt_pnl(bal_change)} USDT\n\n"
        f"💰 Balance: {bal:.2f} USDT\n"
        f"Unrealized PnL: {fmt_pnl(unrealized)} USDT\n"
        f"Open orders: {orders}\n"
        f"Open positions: {len(positions)}\n\n"
        f"Status: {status_icon}\n"
        f"Last order activity: {idle}s ago\n"
        f"Panic events: {state.panic_events}/{PANIC_THRESHOLD}"
    )

    state.last_report_time = now
    state.last_report_balance = bal
    state.last_orders_count = orders

    return report

async def hourly_report():
    while True:
        delay = seconds_until_next_hour()
        log(f"Next hourly report in {int(delay)}s ({REPORT_TZ})")
        await asyncio.sleep(delay)
        
        current_hour = get_current_hour()
        if current_hour != state.last_hour_reported:
            state.last_hour_reported = current_hour
            # Отправляем отчет даже в режиме сна (это важно для мониторинга)
            report = await build_hourly_report()
            if state.is_sleeping:
                report = report.replace("📊 OKXSTOPI HOURLY REPORT", "💤 OKXSTOPI HOURLY REPORT (SLEEP MODE)")
            await tg(report)
        else:
            log("Skipping duplicate hourly report")

async def check_order_activity():
    """Check for order activity using REST API"""
    try:
        # Get current pending orders
        orders = await get_pending_orders()
        current_count = len(orders)
        
        # Check if order count changed
        if current_count != state.last_orders_count:
            state.last_orders_count = current_count
            mark_order_activity()
            log(f"Order count changed: {current_count} orders")
        
        # Also check order history for recent changes
        history = await get_orders_history()
        if history:
            # Check if there are any recent orders (last minute)
            for order in history:
                try:
                    order_time = int(order.get("cTime", "0"))
                    if order_time and (time.time() * 1000 - order_time) < 60000:  # Last minute
                        mark_order_activity()
                        log(f"Recent order found in history: {order.get('ordId', 'unknown')}")
                        break
                except:
                    pass
        
        return True
    except Exception as e:
        log(f"Error checking order activity: {e}")
        return False

async def watchdog():
    """Main watchdog loop"""
    while True:
        now = time.time()

        # Skip check during startup grace period
        if now - state.started_at < STARTUP_GRACE:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # Если бот в спящем режиме - проверяем только активность, не паникуем
        if state.is_sleeping:
            try:
                # Проверяем активность без паники
                await check_order_activity()
                
                # Дополнительная проверка: есть ли открытые позиции или ордера?
                orders = await get_pending_orders()
                positions = await get_positions()
                
                # Если есть активность и бот в сне - пробуждаемся через mark_order_activity
                if orders or positions:
                    # Проверяем, не появились ли ордера недавно
                    await check_order_activity()
                
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            except Exception as e:
                log(f"Error in sleep mode check: {e}")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

        # Check for order activity
        try:
            await check_order_activity()
        except Exception as e:
            log(f"Check order activity error: {e}")

        # Check if timeout exceeded
        if now - state.last_order_activity > WATCHDOG_TIMEOUT:
            await panic(
                f"No order activity for {WATCHDOG_TIMEOUT}s\n"
                f"Last activity: {fmt_utc(state.last_order_activity)}"
            )

        await asyncio.sleep(CHECK_INTERVAL)

async def ping_service():
    """Periodic ping to keep connection alive and log status"""
    while True:
        await asyncio.sleep(60)  # Every minute
        if state.panic_events == 0 and not state.is_sleeping:
            idle = int(time.time() - state.last_order_activity)
            if idle > WATCHDOG_TIMEOUT - 30:  # Warning when close to timeout
                log(f"⚠️ Warning: {idle}s since last order activity")
        
        # Log current status every 5 minutes
        if int(time.time() / 300) != int((time.time() - 60) / 300):
            orders = len(await get_pending_orders())
            positions = len(await get_positions())
            status = "SLEEPING" if state.is_sleeping else "ACTIVE"
            log(f"Status: {status} | {orders} orders, {positions} positions, idle: {int(time.time() - state.last_order_activity)}s, panics: {state.panic_events}/{PANIC_THRESHOLD}")

async def main():
    if not all([
        OKX_API_KEY,
        OKX_SECRET_KEY,
        OKX_PASSPHRASE
    ]):
        raise RuntimeError("Missing OKX credentials")

    # Test credentials before starting
    log("Testing API credentials...")
    try:
        bal = await get_balance()
        log(f"Balance check successful: {bal:.2f} USDT")
    except Exception as e:
        log(f"⚠️ Warning: Could not fetch balance: {e}")

    await startup_report()
    log(
        f"OKXSTOPI started: order timeout={WATCHDOG_TIMEOUT}s, "
        f"check interval={CHECK_INTERVAL}s, startup grace={STARTUP_GRACE}s, "
        f"panic threshold={PANIC_THRESHOLD}"
    )

    # Run all tasks
    await asyncio.gather(
        watchdog(),
        hourly_report(),
        ping_service()
    )

if __name__ == "__main__":
    asyncio.run(main())
