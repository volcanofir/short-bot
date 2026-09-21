"""Short-bot strategy overlay V2.8.

V2.8 keeps V2.7 and adds two mentor-confirmed protections:
1) Hard-exclude TWSE/TPEx disposal securities from the dynamic universe, preopen
   watchlist, and intraday monitor. Disposal data is fetched from official exchange
   OpenAPI endpoints and cached; if an endpoint fails, the bot keeps the last known
   result and never fabricates a disposal flag.
2) Tighten the "5-day overextended" wait line from +6.0% to +5.5%. This is a
   strength/risk ceiling, not an entry trigger: overextended names at or above
   +5.5% are deferred and can be reconsidered after falling back below +5.5%.

All V2.7 broker-inventory, price/volume, order-book, rebound and risk controls
remain in force. Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import date, datetime, timedelta

import requests

import strategy_v27 as v27

app = v27.app
legacy = v27.legacy
logger = v27.logger
TW_TZ = v27.TW_TZ
UA = v27.v26.core.UA

v26 = v27.v26
v25 = v27.v25
v24 = v27.v24
v23 = v27.v23
v22 = v27.v22
v21 = v27.v21

_trial_history = v27._trial_history
_book_history = v27._book_history
_open_store = v27._open_store
_first_bar_cache = v27._first_bar_cache
_market_stats = v27._market_stats
_broker_cache = v27._broker_cache

SAFE_OPEN_NORMAL_PCT = v27.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = v27.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = v27.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = v27.SAFE_OPEN_VOL_PCT
MAX_STRUCTURAL_RISK_PCT = v27.MAX_STRUCTURAL_RISK_PCT
REBOUND_MIN_DROP_PCT = v27.REBOUND_MIN_DROP_PCT
REBOUND_MIN_BOUNCE_PCT = v27.REBOUND_MIN_BOUNCE_PCT
MAX_ALERTS_PER_SYMBOL = v27.MAX_ALERTS_PER_SYMBOL
MONITOR_END_MINUTE = v27.MONITOR_END_MINUTE
PRIMARY_END_MINUTE = v27.PRIMARY_END_MINUTE
DYNAMIC_MIN_VOLUME = v27.DYNAMIC_MIN_VOLUME
DYNAMIC_MAX_SYMBOLS = v27.DYNAMIC_MAX_SYMBOLS
LOCKED_B_MAX_PCT = v27.LOCKED_B_MAX_PCT

BROKER_LOOKBACK_CAL_DAYS = v27.BROKER_LOOKBACK_CAL_DAYS
BROKER_TOP_N = v27.BROKER_TOP_N

EXTREME_GAIN_WAIT_PCT = float(os.environ.get("V28_EXTREME_GAIN_WAIT_PCT", "5.5"))
DISPOSAL_CACHE_SECONDS = max(300, int(os.environ.get("V28_DISPOSAL_CACHE_SECONDS", "1800")))

TWSE_DISPOSAL_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"

# V2.6 live monitor resolves this V2.4 global dynamically.
v24.EXTREME_GAIN_WAIT_PCT = EXTREME_GAIN_WAIT_PCT
v26.core.v24.EXTREME_GAIN_WAIT_PCT = EXTREME_GAIN_WAIT_PCT

_base_build_dynamic_symbols = v26.core.build_dynamic_symbols_v26
_base_screen_v27 = v27.screen_v27
_base_preopen_v27 = v27.preopen_scan_once_v27
_base_intraday_monitor = legacy.intraday_monitor
_base_preopen_summary = v27.format_preopen_summary_v27
_base_report = v27.format_report_v27
_base_line_morning = v27.format_line_morning_v27
_base_daily_summary = v27.format_daily_summary_v27
_base_format_test = v27.format_test_v27

_disposal_lock = threading.Lock()
_disposal_cache = {
    "ts": 0.0,
    "items": {},
    "source_ok": {"TWSE": False, "TPEx": False},
    "errors": [],
    "evaluation_date": None,
}
_disposal_stats = {
    "active": 0,
    "twse": 0,
    "tpex": 0,
    "excluded_dynamic": 0,
    "excluded_watchlist": 0,
    "evaluation_date": None,
    "source_ok": {"TWSE": False, "TPEx": False},
}


def _next_weekday(d: date) -> date:
    x = d + timedelta(days=1)
    while x.weekday() >= 5:
        x += timedelta(days=1)
    return x


_ROC_RE = re.compile(r"(?<!\d)(\d{3})[./-]?(\d{2})[./-]?(\d{2})(?!\d)")


def _roc_to_date(y: str, m: str, d: str):
    try:
        return date(int(y) + 1911, int(m), int(d))
    except Exception:
        return None


def _dates_in_text(value):
    out = []
    for y, m, d in _ROC_RE.findall(str(value or "")):
        parsed = _roc_to_date(y, m, d)
        if parsed:
            out.append(parsed)
    return out


def _record_code(row):
    for key in (
        "Code", "code", "SecuritiesCompanyCode", "SecuritiesCode",
        "SecurityCode", "股票代號", "證券代號", "代號",
    ):
        value = str(row.get(key, "") or "").strip()
        if len(value) == 4 and value.isdigit() and not value.startswith("0"):
            return value

    # Do not accidentally classify warrants/ETFs. Only accept an isolated 4-digit
    # common-stock-looking token if structured code fields were absent.
    for value in row.values():
        m = re.search(r"(?<!\d)([1-9]\d{3})(?!\d)", str(value or ""))
        if m:
            return m.group(1)
    return None


def _record_name(row):
    for key in (
        "Name", "name", "CompanyName", "SecuritiesCompanyName",
        "SecuritiesCompanyAbbreviation", "股票名稱", "證券名稱", "名稱",
    ):
        value = str(row.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _period_text(row):
    preferred = []
    for key, value in row.items():
        lk = str(key).lower()
        if (
            "period" in lk
            or "start" in lk
            or "end" in lk
            or "處置期間" in str(key)
            or "處置起訖" in str(key)
            or "處置日期" in str(key)
        ):
            preferred.append(str(value or ""))

    if preferred:
        return " ".join(preferred)

    # Official feeds sometimes expose only a detail/description field.
    for key in ("Detail", "detail", "Description", "處置內容", "Content"):
        if row.get(key):
            return str(row.get(key))
    return ""


def _period_bounds(row):
    dates = _dates_in_text(_period_text(row))
    if len(dates) >= 2:
        return min(dates), max(dates)
    return (None, None)


def _is_active_for_session(row, today: date):
    start, end = _period_bounds(row)
    if not start or not end:
        return False, start, end

    # At night the bot prepares the next session; during the trading day it also
    # protects the current session. Keeping both dates covers Friday->Monday.
    next_session = _next_weekday(today)
    active = (start <= today <= end) or (start <= next_session <= end)
    return active, start, end


def _fetch_json(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _parse_disposal_rows(rows, market, today):
    items = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = _record_code(row)
        if not code:
            continue
        active, start, end = _is_active_for_session(row, today)
        if not active:
            continue

        name = _record_name(row) or legacy.STOCK_NAMES.get(code, code)
        measure = str(
            row.get("DispositionMeasures")
            or row.get("dispositionMeasures")
            or row.get("處置措施")
            or row.get("處置內容")
            or ""
        ).strip()
        items[code] = {
            "code": code,
            "name": name,
            "market": market,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "measure": measure,
        }
    return items


def fetch_active_disposals_v28(force: bool = False):
    now = datetime.now(TW_TZ)
    today = now.date()
    now_ts = time.time()

    with _disposal_lock:
        if (
            not force
            and _disposal_cache["items"]
            and now_ts - _disposal_cache["ts"] < DISPOSAL_CACHE_SECONDS
        ):
            return dict(_disposal_cache["items"])

    items = {}
    source_ok = {"TWSE": False, "TPEx": False}
    errors = []

    for market, url in (("TWSE", TWSE_DISPOSAL_URL), ("TPEx", TPEX_DISPOSAL_URL)):
        try:
            rows = _fetch_json(url)
            parsed = _parse_disposal_rows(rows, market, today)
            items.update(parsed)
            source_ok[market] = True
            logger.info("V2.8 disposal %s: active=%s rows=%s", market, len(parsed), len(rows))
        except Exception as exc:
            errors.append(f"{market}:{exc}")
            logger.warning("V2.8 disposal %s fetch failed: %s", market, exc)

    with _disposal_lock:
        # If every source failed, retain the last known non-empty list rather than
        # silently pretending there are no disposal stocks.
        if not any(source_ok.values()) and _disposal_cache["items"]:
            items = dict(_disposal_cache["items"])
            source_ok = dict(_disposal_cache["source_ok"])
        else:
            # If one source failed, preserve its still-relevant cached records.
            cached = dict(_disposal_cache["items"])
            for code, item in cached.items():
                market = item.get("market")
                if market in source_ok and not source_ok[market]:
                    try:
                        end = date.fromisoformat(item.get("end")) if item.get("end") else None
                    except Exception:
                        end = None
                    if end and end >= today:
                        items.setdefault(code, item)

        _disposal_cache.update({
            "ts": now_ts,
            "items": dict(items),
            "source_ok": dict(source_ok),
            "errors": list(errors),
            "evaluation_date": today.isoformat(),
        })

    twse_n = sum(1 for x in items.values() if x.get("market") == "TWSE")
    tpex_n = sum(1 for x in items.values() if x.get("market") == "TPEx")
    _disposal_stats.update({
        "active": len(items),
        "twse": twse_n,
        "tpex": tpex_n,
        "evaluation_date": today.isoformat(),
        "source_ok": dict(source_ok),
    })
    return dict(items)


def build_dynamic_symbols_v28(force: bool = False):
    symbols = _base_build_dynamic_symbols(force=force)
    disposal = fetch_active_disposals_v28(force=force)
    if not disposal:
        _disposal_stats["excluded_dynamic"] = 0
        return symbols

    filtered = [s for s in symbols if s.split(".")[0] not in disposal]
    excluded = len(symbols) - len(filtered)
    _disposal_stats["excluded_dynamic"] = excluded
    if excluded:
        logger.info("V2.8 disposal hard-exclude from dynamic universe: %s", excluded)
    return filtered


# The saved V2.6 screen used by V2.7 calls this global dynamically.
v26.core.build_dynamic_symbols_v26 = build_dynamic_symbols_v28


def _purge_disposal_watchlist(force=False):
    disposal = fetch_active_disposals_v28(force=force)
    if not disposal or not legacy._watchlist_today:
        _disposal_stats["excluded_watchlist"] = 0
        return []

    removed = [
        x for x in legacy._watchlist_today
        if str(x.get("code")) in disposal
    ]
    if removed:
        legacy._watchlist_today[:] = [
            x for x in legacy._watchlist_today
            if str(x.get("code")) not in disposal
        ]
        logger.info(
            "V2.8 disposal removed from watchlist: %s",
            ",".join(str(x.get("code")) for x in removed),
        )
    _disposal_stats["excluded_watchlist"] = len(removed)
    return removed


def screen_v28(force: bool = False):
    # V2.7 broker enrichment remains intact; the V2.6 dynamic builder beneath it
    # has already been patched to remove disposal securities before deep scanning.
    candidates = _base_screen_v27(force=force)
    disposal = fetch_active_disposals_v28(force=False)
    if disposal:
        candidates = [c for c in candidates if str(c.get("code")) not in disposal]
    return candidates


# Patch all dynamic screen entry points that may be called by inherited code.
v27.screen_v27 = screen_v28
v26.screen_v26 = screen_v28
v26.core.screen_v26 = screen_v28
v25.screen_v25 = screen_v28
v24.screen_v24 = screen_v28
v23.screen_v23 = screen_v28
v22.screen_v22 = screen_v28
v21.screen_v2 = screen_v28
legacy.screen = screen_v28


def preopen_scan_once_v28():
    fetch_active_disposals_v28(force=False)
    _purge_disposal_watchlist(force=False)
    result = _base_preopen_v27()
    _purge_disposal_watchlist(force=False)
    return result


def intraday_monitor_v28():
    # Disposal status can change after the prior close; refresh through the cache
    # before every live scan and hard-remove any matching name.
    _purge_disposal_watchlist(force=False)
    return _base_intraday_monitor()


legacy.intraday_monitor = intraday_monitor_v28


def _disposal_lines(limit=10):
    items = fetch_active_disposals_v28(force=False)
    if not items:
        ok = _disposal_cache.get("source_ok", {})
        if not all(ok.values()):
            return ["處置資料目前部分來源未取得，未臆測排除名單。"]
        return ["目前未取得有效處置股。"]

    rows = sorted(items.values(), key=lambda x: (x.get("market", ""), x.get("code", "")))
    lines = []
    for item in rows[:limit]:
        period = ""
        if item.get("start") and item.get("end"):
            period = f"｜{item['start'][5:]}~{item['end'][5:]}"
        lines.append(
            f"⛔ {item['code']} {item.get('name','')} [{item.get('market','')}]{period}"
        )
    if len(rows) > limit:
        lines.append(f"…另有 {len(rows)-limit} 支")
    return lines


def format_disposal_status_v28():
    items = fetch_active_disposals_v28(force=True)
    lines = [
        "⛔ <b>V2.8 處置股排除</b>",
        f"目前有效/下一交易日涵蓋：{len(items)} 支",
        f"來源：TWSE {'✅' if _disposal_cache['source_ok'].get('TWSE') else '⚠️'}／"
        f"TPEx {'✅' if _disposal_cache['source_ok'].get('TPEx') else '⚠️'}",
        "",
    ]
    lines.extend(_disposal_lines(limit=15))
    lines.append("")
    lines.append("處置股不進試撮、不進盤中候選。")
    return "\n".join(lines)


def format_preopen_summary_v28():
    text = _base_preopen_summary().replace("V2.7", "V2.8")
    disposal = fetch_active_disposals_v28(force=False)
    return (
        text
        + f"\n⏳ 5日乖離過大：目前漲幅≥+{EXTREME_GAIN_WAIT_PCT:g}%先不逆勢空，"
          f"跌回+{EXTREME_GAIN_WAIT_PCT:g}%以下才重新評估。"
        + f"\n⛔ 處置股硬排除：目前/下一交易日共 {len(disposal)} 支。"
    )


def format_report_v28(candidates):
    return (
        _base_report(candidates).replace("V2.7", "V2.8")
        + f"\n⏳ 5日乖離過大風險線：+{EXTREME_GAIN_WAIT_PCT:g}%；以上等待，回落才重評。"
        + f"\n⛔ 處置股：全市場母池、試撮、盤中皆排除。"
    )


def format_line_morning_v28(candidates):
    return (
        _base_line_morning(candidates).replace("V2.7", "V2.8")
        + f"\nV2.8：乖離過大≥+{EXTREME_GAIN_WAIT_PCT:g}%先等；處置股直接排除。"
    )


def format_daily_summary_v28():
    return _base_daily_summary().replace("V2.7", "V2.8")


def format_test_v28():
    return (
        _base_format_test().replace("V2.7", "V2.8")
        + f"\n✅ V2.8乖離保護：連漲乖離目前漲幅≥+{EXTREME_GAIN_WAIT_PCT:g}%先不空"
        + "\n✅ V2.8處置排除：TWSE/TPEx官方處置資料→母池/試撮/盤中硬排除"
        + "\n✅ V2.8來源失敗保護：不把抓不到資料誤判成『沒有處置股』"
    )


legacy.format_report = format_report_v28
legacy.format_line_morning = format_line_morning_v28
legacy.format_daily_summary = format_daily_summary_v28
legacy.format_test = format_test_v28


def _v28_status():
    fetch_active_disposals_v28(force=False)
    return {
        "status": "ok",
        "version": "2.8",
        "mode": "alerts_only",
        "dynamic_market": dict(_market_stats),
        "disposal": {
            **dict(_disposal_stats),
            "cache_seconds": DISPOSAL_CACHE_SECONDS,
            "errors": list(_disposal_cache.get("errors", [])),
        },
        "extreme_gain_wait_pct": EXTREME_GAIN_WAIT_PCT,
        "broker_inventory": {
            "enabled": bool(legacy.FINMIND_TOKEN),
            "lookback_calendar_days": BROKER_LOOKBACK_CAL_DAYS,
            "top_n": BROKER_TOP_N,
        },
        "max_structural_risk_pct": MAX_STRUCTURAL_RISK_PCT,
        "primary_end": "10:00",
        "secondary_end": "11:30",
        "time": datetime.now(TW_TZ).isoformat(),
    }


if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v28_status

logger.info(
    "Short-bot strategy V2.8 loaded: disposal hard-exclude + MA5 wait %.1f%%",
    EXTREME_GAIN_WAIT_PCT,
)
