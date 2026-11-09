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
import secrets
import asyncio
import aiohttp
import aiohttp.web
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WATCHDOG_TIMEOUT = int(os.getenv("WATCHDOG_TIMEOUT", "600"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))  # How often to check orders
PROFIT_ALERT_USD = float(os.getenv("PROFIT_ALERT_USD", "5"))
LOSS_ALERT_USD = float(os.getenv("LOSS_ALERT_USD", "15"))
PAIR_LOSS_USD = float(os.getenv("PAIR_LOSS_USD", "20"))
PAIR_BAN_HOURS = float(os.getenv("PAIR_BAN_HOURS", "12"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
WEB_COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
SESSION_COOKIE = "okxstop_session"
SESSION_TTL = 7 * 24 * 3600
LOGIN_LIMIT = 8
LOGIN_LOCK_SECONDS = 15 * 60
_session_key = secrets.token_bytes(32)
_login_failures = {}
# Sent alone so Telegram shows them large.
ROCKET_ALERT = "🚀"
CLOWN_ALERT = "🤡"
GREEN_CIRCLE = "🟢"
RED_CIRCLE = "🔴"
SETTINGS_PATH = os.getenv("SETTINGS_PATH", os.path.join("data", "settings.json"))
DEFAULT_SETTINGS = {
    "watchdog_timeout": WATCHDOG_TIMEOUT,
    "check_interval": CHECK_INTERVAL,
    "profit_alert_usd": PROFIT_ALERT_USD,
    "loss_alert_usd": LOSS_ALERT_USD,
    "pair_loss_usd": PAIR_LOSS_USD,
    "pair_ban_hours": PAIR_BAN_HOURS,
}
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
    day_open_balance = None
    last_hour_reported = None
    orders_cache = []  # Cache for order comparison
    last_orders_count = 0
    is_sleeping = False  # Режим сна после превышения паник
    sleep_start_time = None  # Когда начался сон
    has_activity_since_sleep = False  # Была ли активность во время сна
    position_snapshots = {}  # posId -> last seen closed-position snapshot
    trade_events = []  # closed trades already taken from exchange history
    seen_order_ids = []
    seen_lookup = set()
    order_history_ready = False
    cancel_attempted = set()
    pair_bans = {}
    settings = dict(DEFAULT_SETTINGS)
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
        log("ACTIVITY DETECTED - OKXSTOPI RESUMED (woke from sleep)")
        asyncio.create_task(tg("OKXSTOPI RESUMED\n\nActivity detected - Watchdog is active again!"))
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
        log(f"Telegram notification suppressed (sleeping mode): {msg[:50]}...")
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

async def get_orders_history(inst_type="SWAP"):
    """Recent filled and cancelled orders, newest first."""
    data = await okx_request(
        "GET",
        "/api/v5/trade/orders-history",
        params={"instType": inst_type, "limit": 100},
    )
    if not isinstance(data, dict) or str(data.get("code") or "0") != "0":
        log(f"orders-history {inst_type} error: {data}")
        return None
    return data.get("data") or []

def mark_order_seen(ord_id):
    ord_id = str(ord_id or "")
    if not ord_id or ord_id in state.seen_lookup:
        return False
    state.seen_lookup.add(ord_id)
    state.seen_order_ids.append(ord_id)
    if len(state.seen_order_ids) > 2000:
        dropped = state.seen_order_ids.pop(0)
        state.seen_lookup.discard(dropped)
    return True

def is_closed_order(order):
    """Filled close. Opening orders on OKX carry pnl 0 and reduceOnly false."""
    if str(order.get("state") or "") != "filled":
        return False
    if str(order.get("reduceOnly") or "").lower() == "true":
        return True
    return fnum(order.get("pnl")) != 0.0

def fmt_tokens(value):
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"

def fmt_result(value):
    """Net result as +10$ or -2$."""
    number = round(float(value), 2)
    if abs(number - round(number)) < 0.001:
        body = f"{number:.0f}"
    else:
        body = f"{number:.2f}".rstrip("0").rstrip(".")
    if number > 0:
        return f"+{body}$"
    return f"{body}$"

def result_circle(value):
    if value > 0:
        return GREEN_CIRCLE
    if value < 0:
        return RED_CIRCLE
    return ""

def fmt_result_marked(value):
    circle = result_circle(value)
    text = fmt_result(value)
    if circle:
        return f"{circle} {text}"
    return text

def money_in_usdt(amount, ccy, price):
    code = (ccy or "USDT").upper()
    if code in ("USDT", "USD", "USDC", ""):
        return amount
    if price:
        return amount * price
    return 0.0

def order_net_result(order):
    """Close result after the order fee. OKX pnl excludes the fee."""
    price = fnum(order.get("avgPx")) or fnum(order.get("px"))
    fee = money_in_usdt(fnum(order.get("fee")), order.get("feeCcy"), price)
    return fnum(order.get("pnl")) + fee

def cfg(name):
    values = state.settings or DEFAULT_SETTINGS
    if name in values:
        return values[name]
    return DEFAULT_SETTINGS[name]

def extra_trade_alert(result):
    if result > float(cfg("profit_alert_usd")):
        return ROCKET_ALERT
    if result < -float(cfg("loss_alert_usd")):
        return CLOWN_ALERT
    return None

def fmt_ban_until(until_ts):
    moment = datetime.fromtimestamp(float(until_ts), REPORT_TZ)
    return moment.strftime("%Y-%m-%d %H:%M ") + str(REPORT_TZ)

def ban_active(inst_id, now=None):
    now = now or time.time()
    ban = state.pair_bans.get(inst_id)
    return bool(ban) and fnum(ban.get("until")) > now

def pair_window_result(inst_id, now=None):
    """Net result after the previous ban, inside the rolling window."""
    now = now or time.time()
    window = float(cfg("pair_ban_hours")) * 3600
    start = now - window
    ban = state.pair_bans.get(inst_id)
    ignore_before = fnum(ban.get("until")) if ban else 0.0
    total = 0.0
    for event in state.trade_events:
        if event.get("instId") != inst_id:
            continue
        ts = fnum(event.get("ts")) / 1000.0
        if ts < start or ts <= ignore_before:
            continue
        total += event_result(event)
    return total

def consider_pair_ban(inst_id, now=None):
    """Ban a pair for the configured hours once its window loss exceeds the limit."""
    now = now or time.time()
    if not inst_id or ban_active(inst_id, now):
        return None
    total = pair_window_result(inst_id, now)
    limit = float(cfg("pair_loss_usd"))
    if total >= -limit:
        return None
    until = now + float(cfg("pair_ban_hours")) * 3600
    state.pair_bans[inst_id] = {
        "until": until,
        "triggered_at": now,
        "loss": total,
    }
    return {"instId": inst_id, "until": until, "loss": total}

async def order_size(order):
    """Contracts from order history, expressed as tokens and USDT notional."""
    filled = fnum(order.get("accFillSz")) or fnum(order.get("sz"))
    price = fnum(order.get("avgPx")) or fnum(order.get("px"))
    spec = await get_inst_spec(order.get("instId"))
    if spec.get("ctType") == "inverse":
        usdt = filled * spec["ctVal"]
        tokens = usdt / price if price else 0.0
    else:
        tokens = filled * spec["ctVal"]
        usdt = tokens * price
    return usdt, tokens

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
        "fee": str(row.get("fee") or "0"),
        "fundingFee": str(row.get("fundingFee") or "0"),
        "liqPenalty": str(row.get("liqPenalty") or "0"),
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

def position_fees(source):
    """Exchange fee columns are cumulative and already signed (a charge is negative)."""
    if not source:
        return 0.0
    return (
        fnum(source.get("fee"))
        + fnum(source.get("fundingFee"))
        + fnum(source.get("liqPenalty"))
    )

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
    delta_gross = fnum(row.get("pnl")) - (fnum(prev.get("pnl")) if prev else 0.0)
    delta_fee = position_fees(row) - (position_fees(prev) if prev else 0.0)

    spec = await get_inst_spec(row.get("instId"))
    price = fnum(row.get("closeAvgPx")) or fnum(row.get("openAvgPx"))
    notional = contract_notional_usd(spec, delta_sz, price)
    pnl_usd, pnl_ccy = pnl_to_usd(delta_realized, spec, price)
    gross_usd, _ = pnl_to_usd(delta_gross, spec, price)
    fee_usd, _ = pnl_to_usd(delta_fee, spec, price)

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
        "gross": gross_usd,
        "fee": fee_usd,
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

    if payload.get("day_open_balance") is not None:
        state.day_open_balance = fnum(payload.get("day_open_balance"))

    ids = payload.get("seen_order_ids") or []
    if isinstance(ids, list):
        state.seen_order_ids = [str(item) for item in ids][-2000:]
        state.seen_lookup = set(state.seen_order_ids)
        state.order_history_ready = bool(state.seen_order_ids)

    bans = payload.get("pair_bans") or {}
    if isinstance(bans, dict):
        state.pair_bans = {
            str(inst): ban
            for inst, ban in bans.items()
            if isinstance(ban, dict) and fnum(ban.get("until")) > 0
        }

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
    ban_cutoff = time.time() - 14 * 86400
    state.pair_bans = {
        inst: ban
        for inst, ban in state.pair_bans.items()
        if fnum(ban.get("until")) >= ban_cutoff
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
        "day_open_balance": state.day_open_balance,
        "seen_order_ids": state.seen_order_ids[-2000:],
        "pair_bans": state.pair_bans,
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

def event_volume(event):
    if "usdt" in event:
        return fnum(event.get("usdt"))
    return fnum(event.get("notional"))

def event_result(event):
    if "result" in event:
        return fnum(event.get("result"))
    return fnum(event.get("pnl"))

def report_status():
    idle = int(time.time() - state.last_order_activity)
    if state.is_sleeping:
        return "SLEEPING", idle
    if idle < float(cfg("watchdog_timeout")):
        return "OK", idle
    return "WARNING", idle

def render_summary_report(
    title, start_dt, end_dt, events, balance, balance_change,
    unrealized, orders, positions,
):
    period = f"{fmt_utc(start_dt.timestamp())} — {fmt_utc(end_dt.timestamp())}"
    volume = sum(event_volume(event) for event in events)
    result = sum(event_result(event) for event in events)
    status, idle = report_status()
    return (
        f"{title}\n\n"
        f"Period: {period}\n\n"
        f"Trades: {len(events)}\n"
        f"Volume: {volume:.2f} USDT\n"
        f"Result: {fmt_result_marked(result)}\n"
        f"Balance change: {fmt_pnl(balance_change)} USDT\n\n"
        f"Balance: {balance:.2f} USDT\n"
        f"Unrealized PnL: {fmt_pnl(unrealized)} USDT\n"
        f"Open orders: {orders}\n"
        f"Open positions: {positions}\n\n"
        f"Status: {status}\n"
        f"Last order activity: {idle}s ago\n"
        f"Panic events: {state.panic_events}/{PANIC_THRESHOLD}"
    )

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
            recorded.append(trade)
            # Partial/full closes mean the terminal is trading.
            # Liquidation and ADL must not reset the freeze timer.
            if str(row.get("type") or "") in ("1", "2"):
                terminal_alive = True

    for pos_id, snap in applied:
        state.position_snapshots[pos_id] = snap

    remember_trades(recorded)

    if not recorded:
        return

    if terminal_alive:
        if state.is_sleeping:
            mark_order_activity()
        else:
            state.last_order_activity = time.time()

    bal = await get_balance()
    if len(recorded) == 1:
        blocks = [format_trade_block(recorded[0])]
        title = "TRADE CLOSED"
    else:
        blocks = [format_trade_block(trade) for trade in recorded]
        title = f"TRADES CLOSED ({len(recorded)})"
    body = "\n\n".join(blocks)
    await tg(
        f"{title}\n\n"
        f"{body}\n\n"
        f"Balance: {bal:.2f} USDT",
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
        f"OKXSTOPI ENTERING SLEEP MODE\n\n"
        f"Panic events: {state.panic_events} (threshold: {PANIC_THRESHOLD})\n"
        f"Watchdog is now sleeping until new trading activity is detected.\n"
        f"No more panic notifications will be sent.\n\n"
        f"Current balance: {state.last_report_balance:.2f} USDT"
    )
    
    await tg(sleep_msg)
    log("OKXSTOPI entering sleep mode - no more panic notifications")

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
        f"OKXSTOPI PANIC #{state.panic_events}\n\n"
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
    if state.day_open_balance is None:
        state.day_open_balance = bal
    state.last_hour_reported = get_current_hour()
    state.last_orders_count = orders

    await tg(
        f"OKXSTOPI STARTED\n\n"
        f"Balance: {bal:.2f} USDT\n"
        f"Orders: {orders}\n"
        f"Positions: {pos}\n"
        f"All orders idle timeout: {int(cfg('watchdog_timeout'))}s\n"
        f"Check interval: {int(cfg('check_interval'))}s\n"
        f"Trade alerts: order history every {int(cfg('check_interval'))}s"
    )

def upcoming_hour(moment):
    boundary = moment.replace(minute=0, second=0, microsecond=0)
    if moment >= boundary:
        boundary += timedelta(hours=1)
    return boundary

async def publish_report(title, start_dt, end_dt, balance_basis):
    events = events_between(start_dt, end_dt)
    balance = await get_balance()
    try:
        positions = await get_positions()
        orders = len(await get_pending_orders())
    except Exception as e:
        log(f"Report account snapshot failed: {e}")
        positions = []
        orders = 0
    unrealized = sum(fnum(pos.get("upl")) for pos in positions)
    if balance_basis == "day":
        basis = state.day_open_balance
    else:
        basis = state.last_report_balance
    if basis is None:
        basis = balance
    balance_change = balance - basis
    state.last_report_time = time.time()
    state.last_report_balance = balance
    if balance_basis == "day":
        state.day_open_balance = balance
    text = render_summary_report(
        title, start_dt, end_dt, events, balance, balance_change,
        unrealized, orders, len(positions),
    )
    await tg(text, force=True)
    log(f"{title}: {len(events)} trade(s), balance {balance:.2f} USDT")

async def flush_due_reports():
    """Send every clock hour that is already due, and the daily report at midnight."""
    now = datetime.now(REPORT_TZ)
    interval = int(cfg("check_interval"))
    while state.next_report_at + timedelta(seconds=interval) <= now:
        period_end = state.next_report_at
        period_start = period_end - timedelta(hours=1)
        late = (now - period_end).total_seconds() > interval + 30
        hour_events = events_between(period_start, period_end)
        try:
            # Skip empty hours missed while the process was down.
            # The hour that just finished is always sent.
            if hour_events or not late:
                await publish_report(
                    "OKXSTOPI HOURLY REPORT",
                    period_start,
                    period_end,
                    "hour",
                )

            if period_end.hour == 0:
                day_key = period_end.date().isoformat()
                if state.last_daily_date != day_key:
                    await publish_report(
                        "OKXSTOPI DAILY REPORT",
                        period_end - timedelta(days=1),
                        period_end,
                        "day",
                    )
                    state.last_daily_date = day_key
        except Exception as e:
            log(f"Report failed, retry in {interval}s: {e}")
            await asyncio.sleep(interval)
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
        wake_at = state.next_report_at + timedelta(seconds=int(cfg("check_interval")))
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

async def cancel_listed_orders(orders):
    cancelled = []
    for order in orders:
        ord_id = str(order.get("ordId") or "")
        result = await okx_request(
            "POST",
            "/api/v5/trade/cancel-order",
            {"instId": order.get("instId"), "ordId": ord_id},
        )
        if isinstance(result, dict) and result.get("code") == "0":
            state.cancel_attempted.add(ord_id)
            cancelled.append(order)
        else:
            log(f"Failed to cancel order {ord_id}: {result}")
    return cancelled

async def cancel_stuck_orders():
    """Cancel a banned pair at once. Cancel every order only when none of them moved."""
    orders = await get_pending_orders()
    if not isinstance(orders, list):
        return

    now = time.time()
    timeout = float(cfg("watchdog_timeout"))
    newest = None
    tracked = []
    blocked = []
    for order in orders:
        ord_id = str(order.get("ordId") or "")
        updated = fnum(order.get("uTime")) / 1000.0
        if not ord_id or updated <= 0:
            continue
        if newest is None or updated > newest:
            newest = updated
        inst_id = order.get("instId") or ""
        if ban_active(inst_id, now):
            if ord_id not in state.cancel_attempted:
                blocked.append(order)
            continue
        tracked.append(order)

    if newest is not None:
        state.last_order_activity = newest

    if blocked:
        removed = await cancel_listed_orders(blocked)
        if removed:
            lines = ["BLACKLIST ORDER REMOVED", ""]
            for order in removed:
                ban = state.pair_bans.get(order.get("instId")) or {}
                lines.append(
                    f"{order.get('instId')} {order.get('ordId')}\n"
                    f"Pair blocked until {fmt_ban_until(ban.get('until') or now)}"
                )
            await tg("\n\n".join(lines), force=True)
            log(f"Removed {len(removed)} order(s) on blacklisted pairs")

    # One fresh order means the terminal is alive, so old orders stay.
    if newest is None or now - newest < timeout:
        for order in tracked:
            state.cancel_attempted.discard(str(order.get("ordId") or ""))
        return

    frozen = [
        order for order in tracked
        if str(order.get("ordId") or "") not in state.cancel_attempted
    ]
    if not frozen:
        return

    cancelled = await cancel_listed_orders(frozen)
    if not cancelled:
        return

    idle = int(now - newest)
    lines = [
        "ORDERS CANCELLED",
        "",
        f"No placed order moved for {idle}s",
        "",
    ]
    for order in cancelled:
        log(f"Cancelled order {order.get('ordId')} {order.get('instId')} after {idle}s without any movement")
        lines.append(f"{order.get('instId')} {order.get('ordId')}")
    await tg("\n".join(lines), force=True)

async def report_filled_orders():
    """Read closed orders and report the net result after the order fee."""
    rows = []
    for inst_type in ("SWAP", "FUTURES"):
        batch = await get_orders_history(inst_type)
        if batch is None:
            return
        rows.extend(batch)

    if not state.order_history_ready:
        for order in rows:
            mark_order_seen(order.get("ordId"))
        state.order_history_ready = True
        log(f"Order history baseline: {len(state.seen_order_ids)} orders")
        try:
            save_trade_state()
        except Exception as e:
            log(f"Failed to save order history baseline: {e}")
        return

    fresh = []
    changed = False
    for order in rows:
        if not mark_order_seen(order.get("ordId")):
            continue
        changed = True
        if is_closed_order(order):
            fresh.append(order)

    if not fresh:
        if changed:
            try:
                save_trade_state()
            except Exception as e:
                log(f"Failed to save seen orders: {e}")
        return

    fresh.sort(key=lambda order: fnum(order.get("uTime")))
    balance = await get_balance()
    recorded = []
    for order in fresh:
        try:
            usdt, tokens = await order_size(order)
        except Exception as e:
            log(f"Failed to read order size {order.get('ordId')}: {e}")
            continue
        result = order_net_result(order)
        await tg(
            f"TRADE CLOSED\n\n"
            f"{order.get('instId')}\n"
            f"Amount: {usdt:.2f} USDT\n"
            f"Tokens: {fmt_tokens(tokens)}\n"
            f"Result: {fmt_result_marked(result)}\n\n"
            f"Balance: {balance:.2f} USDT",
            force=True,
        )
        alert = extra_trade_alert(result)
        if alert:
            await tg(alert, force=True)
        recorded.append({
            "ts": int(fnum(order.get("uTime")) or time.time() * 1000),
            "instId": order.get("instId") or "",
            "usdt": usdt,
            "result": result,
            "ordId": str(order.get("ordId") or ""),
        })
        log(
            f"Closed order {order.get('ordId')} {order.get('instId')} "
            f"{fmt_result_marked(result)}"
        )

    if not recorded:
        return

    state.trade_events.extend(recorded)
    new_bans = []
    seen_pairs = set()
    for item in recorded:
        inst_id = item.get("instId") or ""
        if not inst_id or inst_id in seen_pairs:
            continue
        seen_pairs.add(inst_id)
        ban = consider_pair_ban(inst_id)
        if ban:
            new_bans.append(ban)
    try:
        save_trade_state()
    except Exception as e:
        log(f"Failed to save trade log: {e}")

    if not new_bans:
        return

    lines = ["PAIR BLACKLIST", ""]
    for ban in new_bans:
        lines.append(
            f"{ban['instId']}\n"
            f"12h result: {fmt_result_marked(ban['loss'])}\n"
            f"New orders on this pair will be removed until {fmt_ban_until(ban['until'])}"
        )
    await tg("\n\n".join(lines), force=True)
    try:
        await cancel_stuck_orders()
    except Exception as e:
        log(f"Blacklist cancel error: {e}")

async def watchdog():
    """Check stuck orders and order history on the same interval."""
    while True:
        try:
            await cancel_stuck_orders()
        except Exception as e:
            log(f"Stuck order check error: {e}")

        try:
            await report_filled_orders()
        except Exception as e:
            log(f"Order history check error: {e}")

        await asyncio.sleep(max(1, int(cfg("check_interval"))))

async def ping_service():
    """Periodic ping to keep connection alive and log status"""
    while True:
        await asyncio.sleep(60)  # Every minute
        if state.panic_events == 0 and not state.is_sleeping:
            idle = int(time.time() - state.last_order_activity)
            if idle > float(cfg("watchdog_timeout")) - 30:
                log(f"Warning: {idle}s since last order activity")
        
        # Log current status every 5 minutes
        if int(time.time() / 300) != int((time.time() - 60) / 300):
            orders = len(await get_pending_orders())
            positions = len(await get_positions())
            status = "SLEEPING" if state.is_sleeping else "ACTIVE"
            log(f"Status: {status} | {orders} orders, {positions} positions, idle: {int(time.time() - state.last_order_activity)}s, panics: {state.panic_events}/{PANIC_THRESHOLD}")

SETTING_LIMITS = {
    "watchdog_timeout": (int, 30, 86400),
    "check_interval": (int, 3, 3600),
    "profit_alert_usd": (float, 0.01, 100000),
    "loss_alert_usd": (float, 0.01, 100000),
    "pair_loss_usd": (float, 0.01, 100000),
    "pair_ban_hours": (float, 1, 168),
}

def sanitize_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    cleaned = {}
    for key, (cast, low, high) in SETTING_LIMITS.items():
        if key not in raw:
            continue
        try:
            value = cast(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key}: нужно число")
        if value < low or value > high:
            raise ValueError(f"{key}: от {low} до {high}")
        cleaned[key] = value
    if not cleaned:
        raise ValueError("нет полей для сохранения")
    return cleaned

def load_settings():
    state.settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as e:
        log(f"Settings unreadable, using defaults: {e}")
        return
    try:
        state.settings.update(sanitize_settings(saved))
    except Exception as e:
        log(f"Settings ignored: {e}")

def save_settings():
    folder = os.path.dirname(SETTINGS_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state.settings, fh, indent=2)
    os.replace(tmp, SETTINGS_PATH)

def pair_stats():
    now = time.time()
    window = float(cfg("pair_ban_hours")) * 3600
    groups = {}
    for event in state.trade_events:
        inst_id = event.get("instId") or "?"
        row = groups.setdefault(inst_id, {
            "instId": inst_id,
            "trades": 0,
            "volume": 0.0,
            "result": 0.0,
            "result_window": 0.0,
        })
        row["trades"] += 1
        row["volume"] += event_volume(event)
        result = event_result(event)
        row["result"] += result
        if fnum(event.get("ts")) / 1000.0 >= now - window:
            row["result_window"] += result
    rows = list(groups.values())
    rows.sort(key=lambda row: row["result_window"])
    return rows

def blacklist_rows():
    now = time.time()
    rows = []
    for inst_id, ban in state.pair_bans.items():
        until = fnum(ban.get("until"))
        if until <= now:
            continue
        rows.append({
            "instId": inst_id,
            "loss": fnum(ban.get("loss")),
            "triggered_at": fnum(ban.get("triggered_at")),
            "until": until,
            "remaining_sec": int(until - now),
        })
    rows.sort(key=lambda row: row["until"])
    return rows

def issue_session():
    expires = int(time.time()) + SESSION_TTL
    payload = f"{expires}.{secrets.token_hex(16)}".encode()
    signature = hmac.new(_session_key, payload, hashlib.sha256).hexdigest()
    return payload.decode() + "." + signature

def session_valid(token):
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expires, nonce, signature = parts
    if not expires.isdigit() or not nonce:
        return False
    payload = f"{expires}.{nonce}".encode()
    expected = hmac.new(_session_key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    return int(expires) > time.time()

def password_matches(given):
    if not WEB_PASSWORD or not isinstance(given, str) or not given or len(given) > 200:
        return False
    return hmac.compare_digest(given.encode("utf-8"), WEB_PASSWORD.encode("utf-8"))

def login_locked(ip, now=None):
    now = now or time.time()
    recent = [stamp for stamp in _login_failures.get(ip, []) if now - stamp < LOGIN_LOCK_SECONDS]
    if recent:
        _login_failures[ip] = recent
    else:
        _login_failures.pop(ip, None)
    return len(recent) >= LOGIN_LIMIT

def record_login_failure(ip):
    now = time.time()
    recent = [stamp for stamp in _login_failures.get(ip, []) if now - stamp < LOGIN_LOCK_SECONDS]
    recent.append(now)
    _login_failures[ip] = recent

def clear_login_failures(ip):
    _login_failures.pop(ip, None)

def set_session_cookie(response):
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="Lax",
        secure=WEB_COOKIE_SECURE,
        path="/",
    )
    return response

@aiohttp.web.middleware
async def web_auth(request, handler):
    if request.path in ("/", "/api/login"):
        return await handler(request)
    if not WEB_PASSWORD:
        return aiohttp.web.json_response(
            {"error": "Задайте WEB_PASSWORD в .env и перезапустите сервис"},
            status=503,
        )
    if request.path == "/api/logout" or session_valid(request.cookies.get(SESSION_COOKIE, "")):
        return await handler(request)
    return aiohttp.web.json_response({"error": "Нужен вход"}, status=401)

async def handle_login(request):
    ip = request.remote or "?"
    if login_locked(ip):
        return aiohttp.web.json_response(
            {"error": "Слишком много попыток. Подождите 15 минут."},
            status=429,
        )
    try:
        raw = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "bad json"}, status=400)
    if not isinstance(raw, dict):
        return aiohttp.web.json_response({"error": "bad json"}, status=400)
    if not WEB_PASSWORD:
        return aiohttp.web.json_response(
            {"error": "Задайте WEB_PASSWORD в .env и перезапустите сервис"},
            status=503,
        )
    if not password_matches(raw.get("password")):
        record_login_failure(ip)
        log(f"Web login failed from {ip}")
        return aiohttp.web.json_response({"error": "Неверный пароль"}, status=401)
    clear_login_failures(ip)
    log(f"Web login from {ip}")
    return set_session_cookie(aiohttp.web.json_response({"ok": True}))

async def handle_logout(_request):
    response = aiohttp.web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response

async def handle_index(_request):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
    if not os.path.isfile(path):
        return aiohttp.web.Response(text="web/index.html missing", status=500)
    return aiohttp.web.FileResponse(path)

async def handle_state(_request):
    return aiohttp.web.json_response({
        "settings": state.settings,
        "pairs": pair_stats(),
        "blacklist": blacklist_rows(),
        "timezone": str(REPORT_TZ),
    })

async def handle_settings(request):
    try:
        raw = await request.json()
        cleaned = sanitize_settings(raw)
    except Exception as e:
        return aiohttp.web.json_response({"error": str(e)}, status=400)
    state.settings.update(cleaned)
    try:
        save_settings()
    except Exception as e:
        return aiohttp.web.json_response({"error": str(e)}, status=500)
    log("Settings updated from web UI")
    return aiohttp.web.json_response({"settings": state.settings})

async def handle_unban(request):
    try:
        raw = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "bad json"}, status=400)
    inst_id = str(raw.get("instId") or "")
    ban = state.pair_bans.get(inst_id)
    if ban and ban_active(inst_id):
        ban["until"] = time.time()
        try:
            save_trade_state()
        except Exception as e:
            log(f"Failed to save unban: {e}")
        log(f"Pair unbanned from web UI: {inst_id}")
    return aiohttp.web.json_response({"blacklist": blacklist_rows()})

async def start_web():
    app = aiohttp.web.Application(middlewares=[web_auth])
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/settings", handle_settings)
    app.router.add_post("/api/unban", handle_unban)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    if WEB_PASSWORD:
        log(f"Web UI on port {WEB_PORT}, password required")
    else:
        log(f"Web UI on port {WEB_PORT} is locked until WEB_PASSWORD is set")

async def main():
    if not all([
        OKX_API_KEY,
        OKX_SECRET_KEY,
        OKX_PASSPHRASE
    ]):
        raise RuntimeError("Missing OKX credentials")

    load_settings()
    load_trade_state()
    if state.next_report_at is None:
        state.next_report_at = upcoming_hour(datetime.now(REPORT_TZ))

    # Test credentials before starting
    log("Testing API credentials...")
    try:
        bal = await get_balance()
        log(f"Balance check successful: {bal:.2f} USDT")
    except Exception as e:
        log(f"Warning: Could not fetch balance: {e}")

    await startup_report()
    try:
        await start_web()
    except Exception as e:
        log(f"Web UI failed: {e}")

    log(
        f"OKXSTOPI started: order timeout={int(cfg('watchdog_timeout'))}s, "
        f"check interval={int(cfg('check_interval'))}s, startup grace={STARTUP_GRACE}s, "
        f"panic threshold={PANIC_THRESHOLD}, trade alerts every {int(cfg('check_interval'))}s, "
        f"web port={WEB_PORT}"
    )

    # Run all tasks
    await asyncio.gather(
        watchdog(),
        hourly_report(),
        ping_service()
    )

if __name__ == "__main__":
    asyncio.run(main())
