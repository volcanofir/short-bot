"""Short-bot strategy overlay V2.4.

V2.4 keeps V2.3 and applies the 09/11 mentor-note confirmations:
- "red" volume is the opening-auction FIRST official trade size, not the whole
  09:00 one-minute candle. Fugle intraday/trades is used for the exact current
  day value; the value is persisted locally for next-day comparison. One-minute
  volume remains only a fallback after restarts/deploys.
- 5-day overextended names are not chased short while the current gain is >=6%;
  unlike locked-limit names, they can be reconsidered if the gain later falls
  back below +6%.
- add notebook symbols 5314 世紀* and 2305 全友.
- keep V2.3's independent prior-high / +2.5% price references, 2.0/1.6/1.4%
  opening-volume tiers, and dynamic thick-bid weakness logic.

Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import requests

import strategy_v23 as v23

v22 = v23.v22
v21 = v23.v21
app = v23.app
legacy = v23.legacy
logger = v23.logger
TW_TZ = v23.TW_TZ
UA = v23.UA
_trial_history = v23._trial_history
_book_history = v23._book_history

SAFE_OPEN_NORMAL_PCT = v23.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = v23.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = v23.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = SAFE_OPEN_NORMAL_PCT
LOOSE_OPEN_VOL_PCT = v23.LOOSE_OPEN_VOL_PCT

EXTREME_GAIN_WAIT_PCT = float(os.environ.get("V24_EXTREME_GAIN_WAIT_PCT", "6.0"))
OPEN_AUCTION_STORE = Path(os.environ.get("V24_OPEN_AUCTION_STORE", "data/open_auction.json"))
OPEN_TRADE_LIMIT = max(10, int(os.environ.get("V24_OPEN_TRADE_LIMIT", "50")))

# Save V2.3 callables before patching module globals.
_v23_screen = v23.screen_v23
_v23_intraday_monitor = v23.intraday_monitor_v23
_v23_preopen_scan_once = v23.preopen_scan_once_v23
_v23_preopen_summary = v23.format_preopen_summary_v23
_v23_report = v23.format_report_v23
_v23_line_morning = v23.format_line_morning_v23
_v23_daily_summary = v23.format_daily_summary_v23
_v23_format_test = v23.format_test_v23
_v23_first_bar_fetch = v23.fetch_first_bar_volume_v23

# Runtime compatibility: status shows this cache count.
_first_bar_cache = {}
_store_lock = threading.Lock()
_open_source_cache = {}

# Notebook universe additions.
for _code, _name, _symbol in (
    ("5314", "世紀*", "5314.TWO"),
    ("2305", "全友", "2305.TW"),
):
    legacy.STOCK_NAMES[_code] = _name
    if _symbol not in legacy.SYMBOLS:
        legacy.SYMBOLS.append(_symbol)

# Exact red numbers visibly written in the 09/10 mentor note.
# They bridge the first V2.4 deployment; future values are captured automatically.
_MENTOR_RED_SEEDS = {
    ("2026-09-10", "6179"): 1401,
    ("2026-09-10", "5314"): 494,
    ("2026-09-10", "2303"): 4195,
    ("2026-09-10", "2305"): 1009,
}


def _store_key(target_date, code: str) -> str:
    return f"{target_date.isoformat()}:{code}"


def _load_open_store():
    try:
        if OPEN_AUCTION_STORE.exists():
            data = json.loads(OPEN_AUCTION_STORE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.info("V2.4 open-auction store load failed: %s", exc)
    return {}


_open_store = _load_open_store()


def _save_open_store():
    try:
        OPEN_AUCTION_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OPEN_AUCTION_STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_open_store, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OPEN_AUCTION_STORE)
    except Exception as exc:
        logger.info("V2.4 open-auction store save failed: %s", exc)


def _read_stored_open(target_date, code: str):
    key = _store_key(target_date, code)
    with _store_lock:
        item = _open_store.get(key)
    if isinstance(item, dict):
        value = item.get("volume")
        if value is not None:
            _open_source_cache[key] = item.get("source", "store")
            return int(value)
    elif item is not None:
        _open_source_cache[key] = "store"
        return int(item)

    seed = _MENTOR_RED_SEEDS.get((target_date.isoformat(), code))
    if seed is not None:
        _open_source_cache[key] = "mentor_note"
        return int(seed)
    return None


def _write_stored_open(target_date, code: str, value: int, source: str):
    key = _store_key(target_date, code)
    payload = {
        "volume": int(value),
        "source": source,
        "updated": datetime.now(TW_TZ).isoformat(),
    }
    with _store_lock:
        _open_store[key] = payload
        _open_source_cache[key] = source
        _first_bar_cache[key] = payload
        _save_open_store()


def _fugle_open_auction_current_day(code: str, target_date):
    """Exact opening-auction size from the first official trade of the day."""
    now = datetime.now(TW_TZ)
    if target_date != now.date() or not legacy.FUGLE_TOKEN or now.hour < 9:
        return None

    try:
        url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/trades/{code}"
        params = {"sort": "asc", "limit": OPEN_TRADE_LIMIT, "isTrial": "false"}
        r = requests.get(
            url,
            params=params,
            headers={"X-API-KEY": legacy.FUGLE_TOKEN},
            timeout=10,
        )
        if r.status_code != 200:
            logger.info("V2.4 Fugle open trade %s status=%s", code, r.status_code)
            return None
        rows = r.json().get("data", []) or []
        for row in rows:
            size = row.get("size")
            if size is None:
                continue
            size = int(size)
            if size > 0:
                return size
    except Exception as exc:
        logger.debug("V2.4 Fugle open trade %s: %s", code, exc)
    return None


def fetch_open_auction_volume_v24(symbol: str, target_date, force: bool = False):
    """Return the opening-auction first-match volume in lots.

    Priority:
      1) captured/persisted exact value
      2) explicit mentor-note seed
      3) current-day Fugle intraday/trades first official trade
      4) V2.3 09:00 one-minute candle as approximate fallback
    """
    code = symbol.split(".")[0]
    key = _store_key(target_date, code)

    if not force:
        stored = _read_stored_open(target_date, code)
        if stored is not None:
            return stored

    exact = _fugle_open_auction_current_day(code, target_date)
    if exact is not None:
        _write_stored_open(target_date, code, exact, "fugle_open_trade")
        return exact

    approx = _v23_first_bar_fetch(symbol, target_date, force=force)
    if approx is not None:
        _open_source_cache[key] = "one_minute_fallback"
        _first_bar_cache[key] = {
            "volume": int(approx),
            "source": "one_minute_fallback",
            "updated": datetime.now(TW_TZ).isoformat(),
        }
        return int(approx)

    _open_source_cache[key] = "none"
    return None


def _open_source(target_date, code: str):
    return _open_source_cache.get(_store_key(target_date, code), "unknown")


def _enrich_open_volume_v24(c):
    c = dict(c)
    symbol = c.get("symbol") or legacy.symbol_for_code(c["code"], c.get("market"))
    target_date = legacy.get_last_trading_day()
    total = max(int(c.get("vol") or 0), 1)
    c["safe_open_volume"] = max(1, int(total * SAFE_OPEN_NORMAL_PCT))
    c["safe_open_protect"] = max(1, int(total * SAFE_OPEN_PROTECT_PCT))
    c["safe_open_strict"] = max(1, int(total * SAFE_OPEN_STRICT_PCT))
    c["loose_open_volume"] = max(1, int(total * LOOSE_OPEN_VOL_PCT))
    c["prev_open_volume"] = fetch_open_auction_volume_v24(symbol, target_date)
    c["prev_open_volume_source"] = _open_source(target_date, c["code"])
    return c


# V2.3 scanner resolves its enrichment helper dynamically.
v23._enrich_open_volume_v23 = _enrich_open_volume_v24


def screen_v24(force: bool = False):
    return _v23_screen(force=force)


v23.screen_v23 = screen_v24
v22.screen_v22 = screen_v24
v21.screen_v2 = screen_v24
legacy.screen = screen_v24


def _today_open_auction_v24(stock):
    now = datetime.now(TW_TZ)
    if now.hour < 9:
        return None
    symbol = stock.get("symbol") or legacy.symbol_for_code(stock["code"], stock.get("market"))
    return fetch_open_auction_volume_v24(symbol, now.date())


# Existing V2.3 live logic now compares exact opening-auction sizes.
v23._today_first_bar_v23 = _today_open_auction_v24
v22._today_first_bar = _today_open_auction_v24


def _is_extreme_ma5(stock) -> bool:
    # The notebook only says "5日乖離過大" but does not give a new numeric
    # threshold. Reuse the strategy's explicit 連漲乖離 classification instead
    # of inventing a broader cutoff.
    types = stock.get("strategy_types", []) or []
    return "連漲乖離" in types


def intraday_monitor_v24():
    """Defer overextended names while their CURRENT gain remains >= +6%."""
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5 or not (9 <= now.hour < legacy.INTRADAY_ALERT_END_HOUR):
        return

    if not legacy._watchlist_today:
        candidates = screen_v24()
        if candidates:
            v21._populate_watchlist(candidates)
    if not legacy._watchlist_today:
        return

    original = list(legacy._watchlist_today)
    allowed = []

    for stock in original:
        if stock.get("limit_up_locked") or not _is_extreme_ma5(stock):
            allowed.append(stock)
            continue

        quote = v21.fugle_quote_v2(stock["code"])
        if quote and quote.get("pct") is not None and quote["pct"] >= EXTREME_GAIN_WAIT_PCT:
            logger.info(
                "%s V2.4 MA5-overextended current %+0.2f%% >= %+0.1f%%; wait",
                stock["code"], quote["pct"], EXTREME_GAIN_WAIT_PCT,
            )
            continue
        allowed.append(stock)

    if not allowed:
        return

    legacy._watchlist_today[:] = allowed
    try:
        _v23_intraday_monitor()
    finally:
        # Restore deferred names so they can become eligible on a later scan if
        # their current gain falls below +6%.
        legacy._watchlist_today[:] = original


v23.intraday_monitor_v23 = intraday_monitor_v24
v22.intraday_monitor_v22 = intraday_monitor_v24
v21.intraday_monitor_v2 = intraday_monitor_v24
legacy.intraday_monitor = intraday_monitor_v24


def preopen_scan_once_v24():
    return _v23_preopen_scan_once()


def format_preopen_summary_v24():
    text = _v23_preopen_summary().replace("V2.3", "V2.4")
    return (
        text
        + f"\n⏳ 5日乖離過大：試撮/盤中若仍≥+{EXTREME_GAIN_WAIT_PCT:g}%先不追空，"
          f"回到+{EXTREME_GAIN_WAIT_PCT:g}%以下才重新評估。"
        + "\n🔴紅字＝前日第一筆正式開盤撮合量；1分K只在抓不到精確值時備援。"
    )


# The already-running V2 preopen loop resolves these globals dynamically.
v23.preopen_scan_once_v23 = preopen_scan_once_v24
v23.format_preopen_summary_v23 = format_preopen_summary_v24
v22.preopen_scan_once_v22 = preopen_scan_once_v24
v22.format_preopen_summary_v22 = format_preopen_summary_v24
v21.preopen_scan_once = preopen_scan_once_v24
v21.format_preopen_summary = format_preopen_summary_v24


def format_report_v24(candidates):
    text = _v23_report(candidates).replace("V2.3", "V2.4")
    return (
        text
        + f"\n\n⏳ 5日乖離過大標的：盤中仍≥+{EXTREME_GAIN_WAIT_PCT:g}%先等，"
          f"跌回+{EXTREME_GAIN_WAIT_PCT:g}%以下才重新評估。"
        + "\n🔴紅字以開盤第一筆正式撮合量為準；1分K僅備援。"
    )


def format_line_morning_v24(candidates):
    text = _v23_line_morning(candidates).replace("V2.3", "V2.4")
    return text + f"\n乖離過大者：≥+{EXTREME_GAIN_WAIT_PCT:g}%先不追空，回落後再看。"


def format_daily_summary_v24():
    return _v23_daily_summary().replace("V2.3", "V2.4")


def format_test_v24():
    return (
        _v23_format_test().replace("V2.3", "V2.4")
        + "\n✅ V2.4紅字：開盤第一筆正式撮合量（Fugle trades）"
        + "\n✅ V2.4紅字保存：當日抓取後留給隔日比較；1分K僅備援"
        + f"\n✅ V2.4乖離保護：目前漲幅≥+{EXTREME_GAIN_WAIT_PCT:g}%先等"
        + "\n✅ V2.4母池新增：5314 世紀*、2305 全友"
    )


legacy.format_report = format_report_v24
legacy.format_line_morning = format_line_morning_v24
legacy.format_daily_summary = format_daily_summary_v24
legacy.format_test = format_test_v24

v23.format_report_v23 = format_report_v24
v23.format_line_morning_v23 = format_line_morning_v24
v23.format_daily_summary_v23 = format_daily_summary_v24
v23.format_test_v23 = format_test_v24


def _index_v24():
    return "📈 短空機器人 V2.4 運行中（雙價格壓力 + 開盤首筆紅字 + 三級量縮 + 乖離6%等待 + 動態五檔｜僅提醒不下單）"


legacy.app.view_functions["index"] = _index_v24


def _v24_status():
    return {
        "status": "ok",
        "version": "2.4",
        "mode": "alerts_only",
        "watchlist": len(legacy._watchlist_today),
        "alerted_today": len(legacy._alerted_today),
        "trial_symbols": len(_trial_history),
        "book_symbols": len(_book_history),
        "open_auction_store": len(_open_store),
        "symbols": len(legacy.SYMBOLS),
        "extreme_gain_wait_pct": EXTREME_GAIN_WAIT_PCT,
        "red_volume": "opening_first_official_trade",
        "time": datetime.now(TW_TZ).isoformat(),
    }


if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v24_status

logger.info("Short-bot strategy V2.4 overlay loaded")
