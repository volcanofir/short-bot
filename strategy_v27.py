"""Short-bot strategy overlay V2.7.

V2.7 keeps V2.6 and adds broker-branch inventory context for the already-selected
watchlist. FinMind branch transactions are aggregated by branch and trading day,
then summarized into 1/3/5-day net flow, 5-day buy VWAP, estimated recent
inventory, turnover behavior, and likely direction.

Important: branch "inventory" is an estimate from recent net buys, not an actual
custody balance. It is used only as a soft context/score and never triggers a
short by itself.

Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

import strategy_v26_hotfix as v26

app = v26.app
legacy = v26.legacy
logger = v26.logger
TW_TZ = v26.TW_TZ

v25 = v26.v25
v24 = v26.v24
v23 = v26.v23
v22 = v26.v22
v21 = v26.v21

_trial_history = v26._trial_history
_book_history = v26._book_history
_open_store = v26._open_store
_first_bar_cache = v26._first_bar_cache
_market_stats = v26._market_stats

SAFE_OPEN_NORMAL_PCT = v26.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = v26.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = v26.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = v26.SAFE_OPEN_VOL_PCT
MAX_STRUCTURAL_RISK_PCT = v26.MAX_STRUCTURAL_RISK_PCT
REBOUND_MIN_DROP_PCT = v26.REBOUND_MIN_DROP_PCT
REBOUND_MIN_BOUNCE_PCT = v26.REBOUND_MIN_BOUNCE_PCT
MAX_ALERTS_PER_SYMBOL = v26.MAX_ALERTS_PER_SYMBOL
MONITOR_END_MINUTE = v26.MONITOR_END_MINUTE
PRIMARY_END_MINUTE = v26.PRIMARY_END_MINUTE
DYNAMIC_MIN_VOLUME = v26.DYNAMIC_MIN_VOLUME
DYNAMIC_MAX_SYMBOLS = v26.DYNAMIC_MAX_SYMBOLS
LOCKED_B_MAX_PCT = v26.LOCKED_B_MAX_PCT

BROKER_LOOKBACK_CAL_DAYS = max(8, int(os.environ.get("V27_BROKER_LOOKBACK_CAL_DAYS", "14")))
BROKER_CACHE_SECONDS = max(900, int(os.environ.get("V27_BROKER_CACHE_SECONDS", "21600")))
BROKER_WORKERS = max(1, min(6, int(os.environ.get("V27_BROKER_WORKERS", "4"))))
BROKER_TOP_N = max(3, min(10, int(os.environ.get("V27_BROKER_TOP_N", "5"))))
BROKER_MIN_LOTS = max(20, int(os.environ.get("V27_BROKER_MIN_LOTS", "100")))
BROKER_SOFT_SCORE_MIN_LOTS = max(100, int(os.environ.get("V27_BROKER_SOFT_SCORE_MIN_LOTS", "300")))
BROKER_SOFT_SCORE_MIN_VOL_PCT = float(os.environ.get("V27_BROKER_SOFT_SCORE_MIN_VOL_PCT", "2.0"))

FINMIND_REPORT_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"

_v26_screen = v26.screen_v26
_v26_preopen_scan = v26.preopen_scan_once_v26
_v26_preopen_summary = v26.format_preopen_summary_v26
_v26_report = v26.format_report_v26
_v26_line_morning = v26.format_line_morning_v26
_v26_daily_summary = v26.format_daily_summary_v26
_v26_format_test = v26.format_test_v26
_base_confidence = v26.core.v23._confidence_v23

_broker_lock = threading.Lock()
_broker_cache = {}


def _num(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _branch_name(row):
    return str(
        row.get("securities_trader_branch_name")
        or row.get("securities_trader")
        or row.get("broker")
        or row.get("dealer_name")
        or ""
    ).strip()


def _branch_id(row):
    return str(
        row.get("securities_trader_id")
        or row.get("broker_id")
        or row.get("dealer_id")
        or ""
    ).strip()


def _fetch_finmind_branch_rows(code: str, target_date):
    """Fetch recent branch-by-price transactions for one stock.

    FinMind documents buy/sell in shares, so all displayed net flow is converted
    to lots (張) by dividing by 1000.
    """
    if not legacy.FINMIND_TOKEN:
        return []

    start_date = target_date - timedelta(days=BROKER_LOOKBACK_CAL_DAYS)
    headers = {"Authorization": f"Bearer {legacy.FINMIND_TOKEN}"}
    params = {
        "data_id": str(code),
        "start_date": start_date.isoformat(),
        "end_date": target_date.isoformat(),
    }

    try:
        r = requests.get(FINMIND_REPORT_URL, headers=headers, params=params, timeout=15)
        if r.status_code == 200:
            rows = r.json().get("data", []) or []
            if rows:
                return rows
        logger.info("V2.7 FinMind branch dedicated endpoint %s status=%s", code, r.status_code)
    except Exception as exc:
        logger.debug("V2.7 FinMind dedicated %s: %s", code, exc)

    # Backward-compatible fallback for accounts still using the generic v4 route.
    try:
        params2 = {
            "dataset": "TaiwanStockTradingDailyReport",
            "stock_id": str(code),
            "start_date": start_date.isoformat(),
            "end_date": target_date.isoformat(),
            "token": legacy.FINMIND_TOKEN,
        }
        r = requests.get(FINMIND_DATA_URL, params=params2, timeout=15)
        if r.status_code != 200:
            logger.info("V2.7 FinMind branch fallback %s status=%s", code, r.status_code)
            return []
        return r.json().get("data", []) or []
    except Exception as exc:
        logger.debug("V2.7 FinMind fallback %s: %s", code, exc)
        return []


def _window_stats(day_map, dates):
    buy = sell = 0
    buy_value = 0.0
    sell_value = 0.0
    for d in dates:
        item = day_map.get(d, {})
        b = int(item.get("buy", 0))
        s = int(item.get("sell", 0))
        buy += b
        sell += s
        buy_value += float(item.get("buy_value", 0.0))
        sell_value += float(item.get("sell_value", 0.0))
    net = buy - sell
    return {
        "buy_lots": round(buy / 1000.0, 1),
        "sell_lots": round(sell / 1000.0, 1),
        "net_lots": round(net / 1000.0, 1),
        "turnover_lots": round((buy + sell) / 1000.0, 1),
        "buy_avg": round(buy_value / buy, 2) if buy > 0 else None,
        "sell_avg": round(sell_value / sell, 2) if sell > 0 else None,
    }


def _direction_for_branch(s1, s3, s5):
    net1 = s1["net_lots"]
    net3 = s3["net_lots"]
    net5 = s5["net_lots"]
    turnover5 = s5["turnover_lots"]
    min_lots = BROKER_MIN_LOTS

    if turnover5 >= min_lots * 3 and abs(net5) <= turnover5 * 0.12:
        return "高周轉/當沖", "turnover"

    if net5 >= min_lots:
        sell_trigger = max(min_lots * 0.5, abs(net5) * 0.15)
        if net1 <= -sell_trigger:
            return "開始倒貨", "turning_sell"
        if net3 > 0 and net1 > 0:
            return "持續累積", "accumulating"
        return "庫存偏多", "inventory"

    if net3 <= -min_lots and net1 < 0:
        return "持續偏賣", "selling"
    if net5 <= -min_lots:
        return "近期偏賣", "selling"
    return "中性", "neutral"


def _aggregate_broker_rows(code: str, rows, target_date):
    by_branch = {}
    valid_dates = set()

    for row in rows:
        d = str(row.get("date") or "")[:10]
        if not d:
            continue
        try:
            d_obj = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if d_obj > target_date:
            continue

        name = _branch_name(row)
        bid = _branch_id(row)
        if not name and not bid:
            continue

        buy = int(_num(row.get("buy") or row.get("buy_volume"), 0))
        sell = int(_num(row.get("sell") or row.get("sell_volume"), 0))
        if buy == 0 and sell == 0:
            continue
        price = _num(row.get("price"), 0.0)

        key = (bid, name)
        branch = by_branch.setdefault(key, {"id": bid, "name": name or bid, "days": {}})
        day = branch["days"].setdefault(d_obj, {
            "buy": 0, "sell": 0, "buy_value": 0.0, "sell_value": 0.0
        })
        day["buy"] += buy
        day["sell"] += sell
        if price > 0:
            day["buy_value"] += price * buy
            day["sell_value"] += price * sell
        valid_dates.add(d_obj)

    dates = sorted(valid_dates)
    if not dates:
        return {
            "available": False,
            "code": str(code),
            "reason": "FinMind無分點資料",
            "branches": [],
        }

    d1 = dates[-1:]
    d3 = dates[-3:]
    d5 = dates[-5:]
    latest_date = dates[-1]

    branches = []
    for branch in by_branch.values():
        s1 = _window_stats(branch["days"], d1)
        s3 = _window_stats(branch["days"], d3)
        s5 = _window_stats(branch["days"], d5)
        direction, direction_key = _direction_for_branch(s1, s3, s5)
        if s5["turnover_lots"] < 1:
            continue
        branches.append({
            "broker": branch["name"],
            "broker_id": branch["id"],
            "net_1d": s1["net_lots"],
            "net_3d": s3["net_lots"],
            "net_5d": s5["net_lots"],
            "buy_5d": s5["buy_lots"],
            "sell_5d": s5["sell_lots"],
            "turnover_5d": s5["turnover_lots"],
            "buy_avg_5d": s5["buy_avg"],
            "sell_avg_5d": s5["sell_avg"],
            "est_inventory_lots": round(max(s5["net_lots"], 0.0), 1),
            "direction": direction,
            "direction_key": direction_key,
        })

    branches.sort(
        key=lambda x: (
            x["direction_key"] == "turning_sell",
            x["est_inventory_lots"],
            abs(x["net_1d"]),
            x["turnover_5d"],
        ),
        reverse=True,
    )

    pressure = sorted(
        [b for b in branches if b["est_inventory_lots"] >= BROKER_MIN_LOTS],
        key=lambda x: (x["direction_key"] == "turning_sell", x["est_inventory_lots"]),
        reverse=True,
    )[:BROKER_TOP_N]
    sellers = sorted(
        [b for b in branches if b["net_1d"] < -BROKER_MIN_LOTS],
        key=lambda x: x["net_1d"],
    )[:BROKER_TOP_N]
    turnover = sorted(
        [b for b in branches if b["direction_key"] == "turnover"],
        key=lambda x: x["turnover_5d"],
        reverse=True,
    )[:BROKER_TOP_N]

    return {
        "available": True,
        "code": str(code),
        "as_of": latest_date.isoformat(),
        "trading_dates": [d.isoformat() for d in d5],
        "branches": branches[: max(BROKER_TOP_N * 3, 12)],
        "pressure": pressure,
        "sellers": sellers,
        "turnover": turnover,
        "branch_count": len(branches),
    }


def fetch_broker_inventory_v27(code: str, force: bool = False):
    target_date = legacy.get_last_trading_day()
    key = (str(code), target_date.isoformat())
    now_ts = time.time()

    with _broker_lock:
        cached = _broker_cache.get(key)
        if cached and not force and now_ts - cached["ts"] < BROKER_CACHE_SECONDS:
            return dict(cached["value"])

    rows = _fetch_finmind_branch_rows(str(code), target_date)
    value = _aggregate_broker_rows(str(code), rows, target_date)
    with _broker_lock:
        _broker_cache[key] = {"ts": now_ts, "value": dict(value)}
    return value


def _compat_broker_context(code):
    """Fast compatibility hook for inherited V2 screen.

    V2.7 does the network calls concurrently after the final shortlist exists,
    avoiding 12 serial FinMind requests inside the inherited screen loop.
    """
    target_date = legacy.get_last_trading_day().isoformat()
    with _broker_lock:
        cached = _broker_cache.get((str(code), target_date))
        if not cached:
            return {}
        value = cached["value"]

    notes = []
    for b in value.get("pressure", [])[:2]:
        notes.append(
            f"{b['broker']} 5日{b['net_5d']:+g}張/{b['direction']}"
        )
    return {
        "broker_inventory": value,
        "broker_notes": notes,
        "broker_date": value.get("as_of"),
    }


# Replace the old single-day serial broker lookup used by strategy_v2.
legacy.fetch_broker_context = _compat_broker_context


def _enrich_candidate_broker(stock, force=False):
    c = dict(stock)
    ctx = fetch_broker_inventory_v27(c["code"], force=force)
    c["broker_inventory"] = ctx
    c["broker_date"] = ctx.get("as_of")

    vol = max(float(c.get("vol") or 0), 1.0)
    pressure = ctx.get("pressure", []) if ctx.get("available") else []
    for b in pressure:
        b["inventory_pct_prev_volume"] = round(
            b.get("est_inventory_lots", 0.0) / vol * 100.0, 2
        )

    primary = None
    turning = [b for b in pressure if b.get("direction_key") == "turning_sell"]
    if turning:
        primary = turning[0]
    elif pressure:
        primary = pressure[0]

    c["broker_primary"] = primary
    c["broker_soft_bearish"] = False
    notes = []
    if primary:
        inv = primary.get("est_inventory_lots", 0.0)
        inv_pct = primary.get("inventory_pct_prev_volume", 0.0)
        if primary.get("direction_key") == "turning_sell":
            c["broker_soft_bearish"] = True
        elif inv >= BROKER_SOFT_SCORE_MIN_LOTS and inv_pct >= BROKER_SOFT_SCORE_MIN_VOL_PCT:
            # Large estimated inventory is a pressure context, not a sell signal.
            c["broker_soft_bearish"] = True
        notes.append(
            f"{primary['broker']} 5日{primary['net_5d']:+g}張"
            f"、昨日{primary['net_1d']:+g}張｜{primary['direction']}"
        )

    if not notes and ctx.get("available"):
        notes.append(f"分點{ctx.get('branch_count', 0)}家，未見明顯集中庫存")
    elif not ctx.get("available"):
        notes.append("分點資料未取得")

    c["broker_notes"] = notes
    return c


def screen_v27(force: bool = False):
    candidates = _v26_screen(force=force)
    if not candidates:
        return []

    if not legacy.FINMIND_TOKEN:
        return [dict(c, broker_notes=["未設定FINMIND_API_TOKEN"]) for c in candidates]

    enriched = []
    with ThreadPoolExecutor(max_workers=BROKER_WORKERS) as ex:
        futs = {
            ex.submit(_enrich_candidate_broker, c, force): c
            for c in candidates
        }
        for fut in as_completed(futs):
            base = futs[fut]
            try:
                enriched.append(fut.result())
            except Exception as exc:
                logger.info("V2.7 broker enrich %s: %s", base.get("code"), exc)
                fallback = dict(base)
                fallback["broker_notes"] = ["分點資料抓取失敗"]
                enriched.append(fallback)

    # Restore original candidate order/ranking.
    order = {c["code"]: i for i, c in enumerate(candidates)}
    enriched.sort(key=lambda c: order.get(c.get("code"), 9999))
    logger.info(
        "V2.7 broker inventory enriched: %s/%s candidates",
        sum(1 for c in enriched if c.get("broker_inventory", {}).get("available")),
        len(enriched),
    )
    return enriched


def _fmt_lots(value):
    if value is None:
        return "-"
    value = float(value)
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value)):+,}"
    return f"{value:+,.1f}"


def _broker_one_line(stock):
    ctx = stock.get("broker_inventory") or {}
    if not ctx.get("available"):
        return f"{stock.get('code')} {stock.get('name')}｜分點資料無"
    p = stock.get("broker_primary")
    if not p:
        return f"{stock.get('code')} {stock.get('name')}｜分點未見明顯集中"
    cost = f"｜近5日買均{p['buy_avg_5d']}" if p.get("buy_avg_5d") else ""
    return (
        f"{stock.get('code')} {stock.get('name')}｜{p['broker']} "
        f"5日{_fmt_lots(p['net_5d'])}張／昨{_fmt_lots(p['net_1d'])}張"
        f" → {p['direction']}{cost}"
    )


def format_broker_watchlist_v27():
    items = list(legacy._watchlist_today or [])
    if not items:
        return "🧾 <b>分點籌碼</b>\n目前沒有觀察名單。"

    lines = ["🧾 <b>觀察名單分點籌碼｜V2.7</b>"]
    for stock in items:
        if not stock.get("broker_inventory"):
            try:
                stock.update(_enrich_candidate_broker(stock))
            except Exception:
                pass
        lines.append("• " + _broker_one_line(stock))
    lines.append("\n⚠️ 5日庫存＝近期淨買推估，不是券商實際保管餘額。")
    return "\n".join(lines)


def format_broker_detail_v27(code: str):
    code = str(code).strip()
    stock = next((x for x in legacy._watchlist_today if str(x.get("code")) == code), None)
    name = (stock or {}).get("name") or legacy.STOCK_NAMES.get(code, code)
    ctx = fetch_broker_inventory_v27(code)
    if not ctx.get("available"):
        return f"🧾 <b>{code} {name} 分點籌碼</b>\n目前沒有可用的 FinMind 分點資料。"

    branches = ctx.get("branches", [])[:BROKER_TOP_N]
    lines = [
        f"🧾 <b>{code} {name} 分點籌碼｜截至 {ctx.get('as_of')}</b>",
        "（1/3/5日皆為淨買賣張數；+為淨買、-為淨賣）",
        "",
    ]
    for i, b in enumerate(branches, 1):
        cost = f"｜5日買均 {b['buy_avg_5d']}" if b.get("buy_avg_5d") else ""
        lines.append(
            f"{i}. <b>{b['broker']}</b>｜"
            f"1日 {_fmt_lots(b['net_1d'])}｜3日 {_fmt_lots(b['net_3d'])}｜"
            f"5日 {_fmt_lots(b['net_5d'])}張｜{b['direction']}{cost}"
        )

    sellers = ctx.get("sellers", [])[:3]
    if sellers:
        lines.append("")
        lines.append("🔻 <b>昨日主要淨賣</b>")
        for b in sellers:
            lines.append(f"• {b['broker']} {_fmt_lots(b['net_1d'])}張")

    lines.append("")
    lines.append("⚠️「推估庫存」只代表近期累積淨買，不等於實際持股餘額。")
    return "\n".join(lines)


def _confidence_v27(
    stock, setup_name, ideal_pullback, volume_state, trial, book, ps,
    special_limit_setup=False,
):
    score, grade, reasons = _base_confidence(
        stock, setup_name, ideal_pullback, volume_state, trial, book, ps,
        special_limit_setup=special_limit_setup,
    )
    primary = stock.get("broker_primary")
    if primary:
        if primary.get("direction_key") == "turning_sell":
            score += 1
            reasons.append(
                f"分點{primary['broker']} 5日{_fmt_lots(primary['net_5d'])}張"
                f"後昨轉賣{_fmt_lots(primary['net_1d'])}張"
            )
        elif stock.get("broker_soft_bearish"):
            # Inventory concentration is only context; keep score neutral unless
            # there is actual latest-day selling.
            reasons.append(
                f"分點{primary['broker']}估計5日庫存{_fmt_lots(primary['est_inventory_lots'])}張"
            )

    grade = "A" if score >= 8 else "B" if score >= v21.ALERT_SCORE_MIN else "C"
    return score, grade, reasons


# Make the existing V2.6 intraday monitor consume the richer context without
# duplicating its price/volume/order-book logic.
v26.core.v23._confidence_v23 = _confidence_v27


def format_preopen_summary_v27():
    base = _v26_preopen_summary().replace("V2.6", "V2.7")
    items = list(legacy._watchlist_today or [])
    if not items:
        return base
    lines = ["", "🧾 <b>分點籌碼摘要</b>"]
    for stock in items:
        lines.append("• " + _broker_one_line(stock))
    lines.append("⚠️ 分點庫存為近5日淨買推估，不等於實際持股。")
    return base + "\n" + "\n".join(lines)


def format_report_v27(candidates):
    text = _v26_report(candidates).replace("V2.6", "V2.7")
    if candidates:
        lines = ["", "🧾 分點籌碼（盤後資料）"]
        for c in candidates:
            lines.append("• " + _broker_one_line(c))
        text += "\n" + "\n".join(lines)
    return text


def format_line_morning_v27(candidates):
    text = _v26_line_morning(candidates).replace("V2.6", "V2.7")
    if candidates:
        lines = ["", "🧾 分點籌碼"]
        for c in candidates:
            lines.append("• " + _broker_one_line(c))
        text += "\n" + "\n".join(lines)
    return text


def format_daily_summary_v27():
    return _v26_daily_summary().replace("V2.6", "V2.7")


def format_test_v27():
    return (
        _v26_format_test().replace("V2.6", "V2.7")
        + "\n✅ V2.7分點籌碼：觀察名單自動抓1/3/5日各券商分點淨買賣"
        + "\n✅ V2.7推估庫存：近5日累積淨買＋買進均價＋高周轉/開始倒貨分類"
        + "\n✅ V2.7只在『近期庫存後最新日轉賣』時軟性+1分，不單獨觸發進場"
    )


# Patch screen entry points. The saved _v26_screen remains the non-recursive base.
v26.screen_v26 = screen_v27
v26.core.screen_v26 = screen_v27
v25.screen_v25 = screen_v27
v24.screen_v24 = screen_v27
v23.screen_v23 = screen_v27
v22.screen_v22 = screen_v27
v21.screen_v2 = screen_v27
legacy.screen = screen_v27

legacy.format_report = format_report_v27
legacy.format_line_morning = format_line_morning_v27
legacy.format_daily_summary = format_daily_summary_v27
legacy.format_test = format_test_v27


def preopen_scan_once_v27():
    # The hardened V2.6 preopen path calls core.screen_v26 dynamically, which has
    # been patched to screen_v27 above.
    return _v26_preopen_scan()


def _v27_status():
    with _broker_lock:
        cached_symbols = len({
            key[0] for key, value in _broker_cache.items()
            if value.get("value", {}).get("available")
        })
    return {
        "status": "ok",
        "version": "2.7",
        "mode": "alerts_only",
        "dynamic_market": dict(_market_stats),
        "broker_inventory": {
            "enabled": bool(legacy.FINMIND_TOKEN),
            "cached_symbols": cached_symbols,
            "lookback_calendar_days": BROKER_LOOKBACK_CAL_DAYS,
            "top_n": BROKER_TOP_N,
            "min_lots": BROKER_MIN_LOTS,
            "note": "recent net-buy estimate, not custody balance",
        },
        "max_structural_risk_pct": MAX_STRUCTURAL_RISK_PCT,
        "primary_end": "10:00",
        "secondary_end": "11:30",
        "time": datetime.now(TW_TZ).isoformat(),
    }


if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v27_status

logger.info("Short-bot strategy V2.7 broker-inventory overlay loaded")
