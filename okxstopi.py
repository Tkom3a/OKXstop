#!/usr/bin/env python3
"""
okxstopi.py
Emergency OKX watchdog for MoonTrader / MoonBot.
Also reports newly closed futures trades to Telegram.
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
TRADE_LOG_PATH = os.getenv("TRADE_LOG_PATH", os.path.join("data", "trade_events.json"))

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
    position_snapshots = {}  # posId -> last seen closed-position snapshot
    trade_events = []  # closed trades already taken from exchange history
    next_report_at = None  # next clock-hour boundary in REPORT_TZ
    last_daily_date = None  # date on which the previous day was already reported

state = State()

def log(msg):
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts_now}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)

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

async def tg(msg, force=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    # Если бот в режиме сна и это не сообщение о пробуждении - пропускаем уведомления.
    # force=True оставляем для отчётов по сделкам: их нужно присылать и в сне.
    if (
        state.is_sleeping
        and not force
        and "RESUMED" not in msg
        and "STARTED" not in msg
    ):
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

def fmt_pct(ratio):
    """OKX pnlRatio is a fraction: 0.01 == +1.00%."""
    pct = ratio * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"

def fnum(value):
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

_inst_specs = {}

CLOSE_TYPE_LABELS = {
    "1": "partial",
    "2": "closed",
    "3": "liquidation",
    "4": "partial liquidation",
    "5": "ADL",
    "6": "ADL",
}

def position_snapshot(row):
    return {
        "uTime": str(row.get("uTime") or ""),
        "closeTotalPos": str(row.get("closeTotalPos") or "0"),
        "realizedPnl": str(row.get("realizedPnl") or "0"),
        "pnl": str(row.get("pnl") or "0"),
        "pnlRatio": str(row.get("pnlRatio") or "0"),
        "type": str(row.get("type") or ""),
    }

def snapshot_changed(prev, snap):
    return (
        prev["uTime"] != snap["uTime"]
        or prev["closeTotalPos"] != snap["closeTotalPos"]
        or prev["realizedPnl"] != snap["realizedPnl"]
        or prev["pnl"] != snap["pnl"]
    )

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

async def get_positions_history(inst_type):
    """Closed/updated futures positions, newest uTime first. None on API error."""
    data = await okx_request(
        "GET",
        "/api/v5/account/positions-history",
        params={"instType": inst_type, "limit": 100},
    )
    if not isinstance(data, dict):
        log(f"positions-history {inst_type}: unexpected response")
        return None
    if data.get("code") != "0":
        log(
            f"positions-history {inst_type} error: "
            f"{data.get('code')} {data.get('msg')}"
        )
        return None
    return data.get("data") or []

async def get_inst_spec(inst_id):
    """Contract size used to turn closed contracts into a USD notional."""
    if inst_id in _inst_specs:
        return _inst_specs[inst_id]

    inst_type = "SWAP" if str(inst_id).endswith("-SWAP") else "FUTURES"
    data = await okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={"instType": inst_type, "instId": inst_id},
    )
    row = None
    if isinstance(data, dict) and data.get("code") == "0":
        rows = data.get("data") or []
        row = rows[0] if rows else None

    if not row:
        log(f"Instrument spec unavailable for {inst_id}, using linear ctVal=1")
        return {"ctVal": 1.0, "ctType": "linear", "settleCcy": "USDT"}

    spec = {
        "ctVal": fnum(row.get("ctVal")) or 1.0,
        "ctType": row.get("ctType") or "linear",
        "settleCcy": (row.get("settleCcy") or "USDT").upper(),
    }
    _inst_specs[inst_id] = spec
    return spec

def contract_notional_usd(spec, contracts, price):
    """USD size of a futures fill. Linear: sz * ctVal * px. Inverse: sz * ctVal."""
    if contracts <= 0:
        return 0.0
    if spec.get("ctType") == "inverse":
        return contracts * spec["ctVal"]
    if price <= 0:
        return 0.0
    return contracts * spec["ctVal"] * price

def pnl_to_usd(amount, spec, price):
    """Settlement-currency PnL as dollars, plus the label to print."""
    ccy = spec.get("settleCcy") or "USDT"
    if ccy in ("USDT", "USD", "USDC"):
        return amount, "USDT" if ccy == "USDT" else ccy
    if price > 0:
        return amount * price, "USDT"
    return amount, ccy

async def describe_closed_trade(row, prev):
    """
    One closed-trade block.
    A new posId is the whole close. An updated posId reports only the new slice,
    because OKX keeps close size and PnL cumulative on the same position id.
    Returns None when this update did not close additional size.
    """
    new_close = fnum(row.get("closeTotalPos"))
    old_close = fnum(prev["closeTotalPos"]) if prev else 0.0
    delta_sz = new_close - old_close
    if delta_sz <= 1e-12:
        return None

    new_realized = fnum(row.get("realizedPnl"))
    old_realized = fnum(prev["realizedPnl"]) if prev else 0.0
    delta_realized = new_realized - old_realized

    spec = await get_inst_spec(row.get("instId"))
    price = fnum(row.get("closeAvgPx")) or fnum(row.get("openAvgPx"))
    notional = contract_notional_usd(spec, delta_sz, price)
    pnl_usd, pnl_ccy = pnl_to_usd(delta_realized, spec, price)

    lever = fnum(row.get("lever"))
    if lever > 0 and notional > 0:
        margin = notional / lever
    else:
        margin = notional

    if prev is None:
        ratio = fnum(row.get("pnlRatio"))
        if lever <= 0 and ratio:
            margin = pnl_usd / ratio
    else:
        # Slice return on margin. pnlRatio on the row is cumulative, so it
        # cannot be reused for the incremental close.
        new_pnl = fnum(row.get("pnl"))
        old_pnl = fnum(prev["pnl"]) if prev else 0.0
        delta_pnl = new_pnl - old_pnl
        slice_pnl_usd, _ = pnl_to_usd(delta_pnl, spec, price)
        ratio = (slice_pnl_usd / margin) if margin else 0.0

    side = (row.get("direction") or row.get("posSide") or "net").upper()
    close_type = str(row.get("type") or "")
    label = "" if close_type == "2" else CLOSE_TYPE_LABELS.get(close_type, "")

    return {
        "ts": int(fnum(row.get("uTime"))),
        "instId": row.get("instId") or "?",
        "side": side,
        "label": label,
        "notional": notional,
        "pnl": pnl_usd,
        "ccy": pnl_ccy,
        "ratio": ratio,
        "margin": margin if margin > 0 else 0.0,
        "posId": str(row.get("posId") or ""),
    }

def format_trade_block(trade):
    suffix = f" ({trade['label']})" if trade.get("label") else ""
    ccy = trade.get("ccy") or "USDT"
    return (
        f"{trade.get('instId', '?')} {trade.get('side', '')}{suffix}\n"
        f"Amount: {fnum(trade.get('notional')):.2f} USDT\n"
        f"PnL: {fmt_pnl(fnum(trade.get('pnl')))} {ccy} ({fmt_pct(fnum(trade.get('ratio')))})"
    )

def load_trade_state():
    """Restore closed trades and position cursors so reports survive a restart."""
    state.trade_events = []
    state.next_report_at = None
    state.last_daily_date = None
    try:
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as e:
        log(f"Trade log unreadable, starting fresh: {e}")
        return

    if not isinstance(payload, dict):
        return

    events = payload.get("events") or []
    if isinstance(events, list):
        state.trade_events = [
            event for event in events
            if isinstance(event, dict) and event.get("ts")
        ]

    snaps = payload.get("snapshots") or {}
    if isinstance(snaps, dict):
        state.position_snapshots = snaps

    raw_next = payload.get("next_report_at")
    if raw_next:
        try:
            parsed = datetime.fromisoformat(raw_next)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=REPORT_TZ)
            state.next_report_at = parsed.astimezone(REPORT_TZ)
        except Exception:
            state.next_report_at = None

    last_daily = payload.get("last_daily_date")
    if isinstance(last_daily, str):
        state.last_daily_date = last_daily

    log(
        f"Loaded {len(state.trade_events)} closed trades "
        f"and {len(state.position_snapshots)} position cursors"
    )

def save_trade_state():
    cutoff = int((time.time() - 3 * 86400) * 1000)
    state.trade_events = [
        event for event in state.trade_events
        if int(fnum(event.get("ts"))) >= cutoff
    ]
    snap_cutoff = int((time.time() - 14 * 86400) * 1000)
    state.position_snapshots = {
        pos_id: snap
        for pos_id, snap in state.position_snapshots.items()
        if int(fnum(snap.get("uTime"))) >= snap_cutoff
    }

    folder = os.path.dirname(TRADE_LOG_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    payload = {
        "events": state.trade_events,
        "snapshots": state.position_snapshots,
        "next_report_at": (
            state.next_report_at.isoformat() if state.next_report_at else None
        ),
        "last_daily_date": state.last_daily_date,
    }
    tmp = TRADE_LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, TRADE_LOG_PATH)

def remember_trades(trades):
    if not trades:
        return
    state.trade_events.extend(trades)
    try:
        save_trade_state()
    except Exception as e:
        log(f"Failed to save trade log: {e}")

def events_between(start_dt, end_dt):
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    found = [
        event for event in state.trade_events
        if start_ms <= int(fnum(event.get("ts"))) < end_ms
    ]
    found.sort(key=lambda event: int(fnum(event.get("ts"))))
    return found

def trade_totals(events):
    notional = sum(fnum(event.get("notional")) for event in events)
    pnl = sum(fnum(event.get("pnl")) for event in events)
    weight = 0.0
    weighted = 0.0
    for event in events:
        margin = fnum(event.get("margin"))
        if margin > 0:
            weight += margin
            weighted += fnum(event.get("ratio")) * margin
    ratio = (weighted / weight) if weight else None
    return len(events), notional, pnl, ratio

def render_report_pages(title, start_dt, end_dt, events, extra_lines, balance):
    """One or more Telegram messages. Balance is only on the last page."""
    period = f"{fmt_utc(start_dt.timestamp())} — {fmt_utc(end_dt.timestamp())}"
    count, notional, pnl, ratio = trade_totals(events)
    pnl_line = f"PnL: {fmt_pnl(pnl)} USDT"
    if ratio is not None:
        pnl_line += f" ({fmt_pct(ratio)})"
    totals = (
        f"Trades: {count}\n"
        f"Amount: {notional:.2f} USDT\n"
        f"{pnl_line}"
    )
    header = f"{title}\n\nPeriod: {period}\n\n"
    tail_parts = [totals]
    if extra_lines:
        tail_parts.append("\n".join(extra_lines))
    tail_parts.append(f"💰 Balance: {balance:.2f} USDT")
    tail = "\n\n" + "\n\n".join(tail_parts)

    blocks = [format_trade_block(event) for event in events]
    if not blocks:
        return [header + "\n\n".join(tail_parts)]

    pages = []
    current = header
    limit = 3500
    for block in blocks:
        piece = block + "\n\n"
        if len(current) + len(piece) + len(tail) > limit and current != header:
            pages.append(current.rstrip())
            current = f"{title} (continued)\n\n"
        current += piece
    pages.append(current.rstrip() + tail)

    if len(pages) > 1:
        total = len(pages)
        pages = [f"{page}\n\n({index}/{total})" for index, page in enumerate(pages, 1)]
    return pages

async def check_closed_trades():
    """
    Poll closed futures positions on the same cadence as order tracking.
    History from before this process started is remembered and not sent.
    """
    rows = []
    for inst_type in ("SWAP", "FUTURES"):
        batch = await get_positions_history(inst_type)
        if batch is None:
            continue
        rows.extend(batch)

    if not rows and not state.position_snapshots:
        return

    # One row per position; oldest close first so the Telegram text reads in order.
    deduped = {}
    for row in rows:
        pos_id = row.get("posId")
        inst_type = row.get("instType")
        if not pos_id or inst_type not in ("SWAP", "FUTURES"):
            continue
        current = deduped.get(pos_id)
        if current is None or fnum(row.get("uTime")) >= fnum(current.get("uTime")):
            deduped[pos_id] = row
    ordered = sorted(deduped.values(), key=lambda row: fnum(row.get("uTime")))

    started_ms = int(state.started_at * 1000)
    pending = []
    baselined = 0

    for row in ordered:
        pos_id = row.get("posId")
        snap = position_snapshot(row)
        prev = state.position_snapshots.get(pos_id)
        u_time = int(fnum(snap["uTime"]))

        if prev is None and u_time <= started_ms:
            state.position_snapshots[pos_id] = snap
            baselined += 1
            continue

        if prev is not None and not snapshot_changed(prev, snap):
            continue

        pending.append((pos_id, row, prev, snap))

    if baselined:
        log(
            f"Ignored {baselined} closed position(s) from before start; "
            "alerts only for new closes"
        )

    if not pending:
        return

    blocks = []
    recorded = []
    applied = []
    terminal_alive = False
    for pos_id, row, prev, snap in pending:
        try:
            trade = await describe_closed_trade(row, prev)
        except Exception as e:
            log(f"Failed to format closed trade {pos_id}: {e}")
            continue
        applied.append((pos_id, snap))
        if trade:
            blocks.append(format_trade_block(trade))
            recorded.append(trade)
            # Partial/full closes mean the terminal is trading.
            # Liquidation and ADL must not reset the freeze timer.
            if str(row.get("type") or "") in ("1", "2"):
                terminal_alive = True

    for pos_id, snap in applied:
        state.position_snapshots[pos_id] = snap

    remember_trades(recorded)

    if not blocks:
        return

    if terminal_alive:
        if state.is_sleeping:
            mark_order_activity()
        else:
            state.last_order_activity = time.time()

    bal = await get_balance()
    title = "📈 TRADE CLOSED" if len(blocks) == 1 else f"📈 TRADES CLOSED ({len(blocks)})"
    body = "\n\n".join(blocks)
    await tg(
        f"{title}\n\n"
        f"{body}\n\n"
        f"💰 Balance: {bal:.2f} USDT",
        force=True,
    )
    log(f"Reported {len(blocks)} closed trade(s), balance {bal:.2f} USDT")

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
        f"Trade alerts: on close, hourly, and daily\n"
        f"Panic threshold: {PANIC_THRESHOLD} (sleep mode after)"
    )

def upcoming_hour(moment):
    boundary = moment.replace(minute=0, second=0, microsecond=0)
    if moment >= boundary:
        boundary += timedelta(hours=1)
    return boundary

async def watchdog_lines():
    idle = int(time.time() - state.last_order_activity)
    if state.is_sleeping:
        status = "💤 SLEEPING"
    elif idle < WATCHDOG_TIMEOUT:
        status = "✅ OK"
    else:
        status = "⚠️ WARNING"

    try:
        orders = len(await get_pending_orders())
        positions = len(await get_positions())
    except Exception as e:
        log(f"Status lines failed: {e}")
        orders = 0
        positions = 0

    return [
        f"Status: {status}",
        f"Last order activity: {idle}s ago",
        f"Open orders: {orders}",
        f"Open positions: {positions}",
        f"Panic events: {state.panic_events}/{PANIC_THRESHOLD}",
    ]

async def publish_report(title, start_dt, end_dt, extra_lines):
    events = events_between(start_dt, end_dt)
    balance = await get_balance()
    state.last_report_time = time.time()
    state.last_report_balance = balance
    pages = render_report_pages(
        title, start_dt, end_dt, events, extra_lines, balance
    )
    for page in pages:
        await tg(page, force=True)
    log(
        f"{title}: {len(events)} trade(s), "
        f"{len(pages)} message(s), balance {balance:.2f} USDT"
    )

async def flush_due_reports():
    """Send every clock hour that is already due, and the daily report at midnight."""
    now = datetime.now(REPORT_TZ)
    while state.next_report_at + timedelta(seconds=CHECK_INTERVAL) <= now:
        period_end = state.next_report_at
        period_start = period_end - timedelta(hours=1)
        late = (now - period_end).total_seconds() > CHECK_INTERVAL + 30
        hour_events = events_between(period_start, period_end)
        try:
            # Skip empty hours missed while the process was down.
            # The hour that just finished is always sent.
            if hour_events or not late:
                await publish_report(
                    "📊 OKXSTOPI HOURLY REPORT",
                    period_start,
                    period_end,
                    await watchdog_lines(),
                )

            if period_end.hour == 0:
                day_key = period_end.date().isoformat()
                if state.last_daily_date != day_key:
                    await publish_report(
                        "📆 OKXSTOPI DAILY REPORT",
                        period_end - timedelta(days=1),
                        period_end,
                        [],
                    )
                    state.last_daily_date = day_key
        except Exception as e:
            log(f"Report failed, retry in {CHECK_INTERVAL}s: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
            return

        state.next_report_at = period_end + timedelta(hours=1)
        state.last_hour_reported = get_current_hour()
        try:
            save_trade_state()
        except Exception as e:
            log(f"Failed to save report schedule: {e}")
        now = datetime.now(REPORT_TZ)

async def hourly_report():
    """Clock-hour trade summary, plus the full previous day at midnight."""
    if state.next_report_at is None:
        state.next_report_at = upcoming_hour(datetime.now(REPORT_TZ))

    while True:
        wake_at = state.next_report_at + timedelta(seconds=CHECK_INTERVAL)
        delay = (wake_at - datetime.now(REPORT_TZ)).total_seconds()
        if delay > 0:
            log(f"Next hourly report in {int(delay)}s ({REPORT_TZ})")
            await asyncio.sleep(delay)
        await flush_due_reports()

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

        try:
            await check_closed_trades()
        except Exception as e:
            log(f"Closed trades check error: {e}")

        # Skip panic checks during startup grace period
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

    load_trade_state()
    if state.next_report_at is None:
        state.next_report_at = upcoming_hour(datetime.now(REPORT_TZ))

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
        f"panic threshold={PANIC_THRESHOLD}, trade alerts every {CHECK_INTERVAL}s"
    )

    # Run all tasks
    await asyncio.gather(
        watchdog(),
        hourly_report(),
        ping_service()
    )

if __name__ == "__main__":
    asyncio.run(main())
