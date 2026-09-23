"""Short-bot strategy overlay V2.6.

V2.6 keeps V2.5 and adds two confirmed improvements:
1) Replace the fixed hand-maintained symbol pool with a two-stage dynamic TWSE/TPEx
   market universe. Official all-market daily snapshots prefilter active/strong
   common stocks, then the existing V2.5 historical rules do the expensive deep scan.
2) Previous-day locked-limit stocks get a second B-grade path: if the session never
   reaches +6%, trial/opening volume is weak, and price breaks the opening price,
   the stock may be alerted even when the old open<2% / rebound<1% A-grade pattern
   was not present. The old strict pattern remains the higher-confidence A path.

Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime

import requests

import strategy_v25 as v25

v24 = v25.v24
v23 = v25.v23
v22 = v25.v22
v21 = v25.v21
app = v25.app
legacy = v25.legacy
logger = v25.logger
TW_TZ = v25.TW_TZ
UA = v25.UA

_trial_history = v25._trial_history
_book_history = v25._book_history
_open_store = v25._open_store
_first_bar_cache = v25._first_bar_cache

SAFE_OPEN_NORMAL_PCT = v25.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = v25.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = v25.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = v25.SAFE_OPEN_VOL_PCT

MAX_STRUCTURAL_RISK_PCT = v25.MAX_STRUCTURAL_RISK_PCT
REBOUND_MIN_DROP_PCT = v25.REBOUND_MIN_DROP_PCT
REBOUND_MIN_BOUNCE_PCT = v25.REBOUND_MIN_BOUNCE_PCT
MAX_ALERTS_PER_SYMBOL = v25.MAX_ALERTS_PER_SYMBOL
MONITOR_END_MINUTE = v25.MONITOR_END_MINUTE
PRIMARY_END_MINUTE = v25.PRIMARY_END_MINUTE

DYNAMIC_MIN_VOLUME = max(500, int(os.environ.get("V26_DYNAMIC_MIN_VOLUME", "3000")))
DYNAMIC_MAX_SYMBOLS = max(80, int(os.environ.get("V26_DYNAMIC_MAX_SYMBOLS", "220")))
DYNAMIC_CACHE_SECONDS = max(300, int(os.environ.get("V26_DYNAMIC_CACHE_SECONDS", "1800")))
DYNAMIC_HIGH_PCT = float(os.environ.get("V26_DYNAMIC_HIGH_PCT", "3.0"))
DYNAMIC_CLOSE_PCT = float(os.environ.get("V26_DYNAMIC_CLOSE_PCT", "1.0"))
DYNAMIC_RETRACE_PCT = float(os.environ.get("V26_DYNAMIC_RETRACE_PCT", "1.5"))
DYNAMIC_BIG_VOLUME = max(5000, int(os.environ.get("V26_DYNAMIC_BIG_VOLUME", "30000")))

LOCKED_B_MAX_PCT = float(os.environ.get("V26_LOCKED_B_MAX_PCT", "6.0"))
LOCKED_B_REQUIRE_TRIAL_SHRINK = (
    os.environ.get("V26_LOCKED_B_REQUIRE_TRIAL_SHRINK", "true").lower() == "true"
)

TWSE_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_ALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

# Keep a last-known fallback so an upstream OpenAPI outage never leaves the bot empty.
_FALLBACK_SYMBOLS = list(legacy.SYMBOLS)
_FALLBACK_NAMES = dict(legacy.STOCK_NAMES)

# Recent notebook names are forced into stage-1 when present in the official snapshot.
# This is only a fallback/sanity set; future symbols do NOT need to be manually added.
_NOTEBOOK_NAMES = {
    "2338": "光罩",
    "3624": "光頡",
    "6226": "光鼎",
    "1528": "恩德",
    "6706": "惠特",
    "2305": "全友",
    "5314": "世紀*",
    "6179": "亞通",
    "2481": "強茂",
    "2426": "鼎元",
    "3094": "聯傑",
    "6538": "倉和",
    "6505": "台塑化",
    "1409": "新纖",
}
for _code, _name in _NOTEBOOK_NAMES.items():
    legacy.STOCK_NAMES[_code] = _name

_v25_screen = v25.screen_v25
_v25_daily_summary = v25.format_daily_summary_v25
_v25_format_test = v25.format_test_v25
_v25_preopen_scan = v25.preopen_scan_once_v25
_v25_preopen_summary = v25.format_preopen_summary_v25
_v25_report = v25.format_report_v25
_v25_line_morning = v25.format_line_morning_v25

_market_lock = threading.Lock()
_market_cache = {"ts": 0.0, "rows": [], "symbols": [], "source_date": None}
_market_stats = {
    "all_rows": 0,
    "eligible_rows": 0,
    "prefilter_symbols": len(_FALLBACK_SYMBOLS),
    "source_date": None,
    "fallback": True,
}


def _f(value, default=None):
    if value is None:
        return default
    try:
        s = str(value).replace(",", "").replace("+", "").strip()
        if not s or s in {"--", "---", "N/A", "nan", "None"}:
            return default
        return float(s)
    except Exception:
        return default


def _i(value, default=0):
    x = _f(value, None)
    return int(x) if x is not None else default


def _four_digit_common(code) -> bool:
    code = str(code or "").strip()
    # Common stocks in the strategy universe are four numeric digits and do not
    # start with 0 (filters ETFs/ETNs such as 0050).
    return len(code) == 4 and code.isdigit() and not code.startswith("0")


def _parse_snapshot_row(row, market):
    if market == "上市":
        code = str(row.get("Code", "")).strip()
        name = str(row.get("Name", "")).strip()
        close = _f(row.get("ClosingPrice"))
        high = _f(row.get("HighestPrice"))
        open_ = _f(row.get("OpeningPrice"))
        change = _f(row.get("Change"), 0.0)
        volume_lots = _i(row.get("TradeVolume"), 0) // 1000
        date_text = str(row.get("Date", "")).strip()
        suffix = ".TW"
    else:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        name = str(
            row.get("CompanyName")
            or row.get("SecuritiesCompanyName")
            or row.get("SecuritiesCompanyAbbreviation")
            or ""
        ).strip()
        close = _f(row.get("Close"))
        high = _f(row.get("High"))
        open_ = _f(row.get("Open"))
        change = _f(row.get("Change"), 0.0)
        volume_lots = _i(row.get("TradingShares"), 0) // 1000
        date_text = str(row.get("Date", "")).strip()
        suffix = ".TWO"

    if not _four_digit_common(code) or close is None or close <= 0:
        return None
    if high is None:
        high = close
    if open_ is None:
        open_ = close

    prev_close = close - (change or 0.0)
    if prev_close <= 0:
        prev_close = close
    close_pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    high_pct = (high - prev_close) / prev_close * 100 if prev_close else close_pct
    retrace_pct = max(0.0, high_pct - close_pct)

    return {
        "code": code,
        "name": name or legacy.STOCK_NAMES.get(code, code),
        "symbol": f"{code}{suffix}",
        "market": market,
        "close": close,
        "open": open_,
        "high": high,
        "volume": volume_lots,
        "close_pct": close_pct,
        "high_pct": high_pct,
        "retrace_pct": retrace_pct,
        "date": date_text,
    }


def _fetch_json(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_all_market_snapshot_v26(force=False):
    now_ts = time.time()
    with _market_lock:
        if (
            not force
            and _market_cache["rows"]
            and now_ts - _market_cache["ts"] < DYNAMIC_CACHE_SECONDS
        ):
            return list(_market_cache["rows"])

    rows = []
    errors = []
    for url, market in ((TWSE_ALL_URL, "上市"), (TPEX_ALL_URL, "上櫃")):
        try:
            payload = _fetch_json(url)
            parsed = []
            for raw in payload:
                item = _parse_snapshot_row(raw, market)
                if item:
                    parsed.append(item)
            rows.extend(parsed)
            logger.info("V2.6 market snapshot %s: %s common stocks", market, len(parsed))
        except Exception as exc:
            errors.append(f"{market}:{exc}")
            logger.warning("V2.6 market snapshot %s failed: %s", market, exc)

    if not rows:
        logger.warning("V2.6 all-market snapshot unavailable; fixed universe fallback: %s", errors)
        return []

    source_date = next((r["date"] for r in rows if r.get("date")), None)
    with _market_lock:
        _market_cache.update({
            "ts": now_ts,
            "rows": list(rows),
            "source_date": source_date,
        })
    return rows


def _stage1_score(row):
    # Ranking only reduces expensive historical Yahoo calls. It is deliberately
    # broad: volume + current strength + intraday strength + high-to-close retrace.
    vol_component = math.log10(max(row["volume"], 1) + 1) * 1.7
    return (
        vol_component
        + max(row["close_pct"], 0.0) * 1.25
        + max(row["high_pct"], 0.0) * 0.85
        + max(row["retrace_pct"], 0.0) * 0.65
    )


def build_dynamic_symbols_v26(force=False):
    rows = fetch_all_market_snapshot_v26(force=force)
    if not rows:
        _market_stats.update({
            "all_rows": 0,
            "eligible_rows": 0,
            "prefilter_symbols": len(_FALLBACK_SYMBOLS),
            "source_date": None,
            "fallback": True,
        })
        return list(_FALLBACK_SYMBOLS)

    eligible = []
    forced = []
    for row in rows:
        legacy.STOCK_NAMES[row["code"]] = row["name"]

        if row["close"] > legacy.SCREEN_MAX_PRICE or row["volume"] < DYNAMIC_MIN_VOLUME:
            # A handwritten priority name still needs enough liquidity to be useful.
            continue

        active = (
            row["high_pct"] >= DYNAMIC_HIGH_PCT
            or row["close_pct"] >= DYNAMIC_CLOSE_PCT
            or row["retrace_pct"] >= DYNAMIC_RETRACE_PCT
            or row["volume"] >= DYNAMIC_BIG_VOLUME
        )
        if active:
            eligible.append(row)
        if row["code"] in _NOTEBOOK_NAMES:
            forced.append(row)

    eligible.sort(key=_stage1_score, reverse=True)
    selected = eligible[:DYNAMIC_MAX_SYMBOLS]

    # Force only known notebook examples that exist in the official market data;
    # this does not block unknown future names because stage-1 is all-market.
    by_symbol = {r["symbol"]: r for r in selected}
    for row in forced:
        by_symbol.setdefault(row["symbol"], row)

    selected = sorted(by_symbol.values(), key=_stage1_score, reverse=True)
    symbols = [r["symbol"] for r in selected]

    _market_stats.update({
        "all_rows": len(rows),
        "eligible_rows": len(eligible),
        "prefilter_symbols": len(symbols),
        "source_date": _market_cache.get("source_date"),
        "fallback": False,
    })
    with _market_lock:
        _market_cache["symbols"] = list(symbols)

    logger.info(
        "V2.6 dynamic universe: all=%s eligible=%s deep_scan=%s date=%s",
        len(rows), len(eligible), len(symbols), _market_cache.get("source_date"),
    )
    return symbols or list(_FALLBACK_SYMBOLS)


def screen_v26(force: bool = False):
    symbols = build_dynamic_symbols_v26(force=force)
    # Keep the narrowed dynamic list in legacy so symbol_for_code and all inherited
    # screen functions resolve the correct market suffix.
    legacy.SYMBOLS[:] = symbols
    return _v25_screen(force=force)


# Patch all screen entry points that inherited preopen/report code may resolve.
v25.screen_v25 = screen_v26
v24.screen_v24 = screen_v26
v23.screen_v23 = screen_v26
v22.screen_v22 = screen_v26
v21.screen_v2 = screen_v26
legacy.screen = screen_v26


def _local_rebound_setup_v26(stock, quote, bars, ps):
    """V2.5 rebound structure, with the relaxed locked-limit B path."""
    if len(bars) < 4:
        return None
    window = bars[-v25.REBOUND_LOOKBACK_BARS:]
    session_open = bars[0]["open"]
    if session_open <= 0:
        return None

    low_idx = min(range(len(window)), key=lambda i: window[i]["low"])
    if low_idx >= len(window) - 1:
        return None

    low_price = window[low_idx]["low"]
    after_low = window[low_idx + 1:]
    rebound_high = max(b["high"] for b in after_low)
    current = float(quote["current"])
    drop_pct = (session_open - low_price) / session_open * 100
    bounce_pct = (rebound_high - low_price) / low_price * 100 if low_price > 0 else 0
    turn_level = v21.move_ticks(rebound_high, -v25.REBOUND_TURN_TICKS)

    if drop_pct < REBOUND_MIN_DROP_PCT or bounce_pct < REBOUND_MIN_BOUNCE_PCT:
        return None
    if current > turn_level:
        return None
    if ps.get("cancel") and not stock.get("limit_up_locked"):
        return None

    if stock.get("limit_up_locked"):
        open_price = quote.get("open")
        pct_now = quote.get("pct")
        day_high = quote.get("day_high") or current
        high_pct = (
            (day_high - stock["close"]) / stock["close"] * 100
            if stock.get("close") else 999
        )
        # Hard rule retained from the mentor notes: once the day reaches +6%,
        # don't force the short. Otherwise a break of the open can still qualify.
        if (
            not open_price
            or pct_now is None
            or current >= open_price
            or pct_now >= LOCKED_B_MAX_PCT
            or high_pct >= LOCKED_B_MAX_PCT
        ):
            return None

    stop = v21.move_ticks(rebound_high, +1)
    entry = v21.nearest_tick(current)
    risk_pct = (stop - entry) / entry * 100 if entry > 0 else 999
    if stop <= entry or risk_pct > MAX_STRUCTURAL_RISK_PCT:
        return None

    return {
        "setup_name": "弱勢下殺後反彈空",
        "entry": entry,
        "structural_high": rebound_high,
        "stop": stop,
        "risk_pct": risk_pct,
        "drop_pct": drop_pct,
        "bounce_pct": bounce_pct,
        "low": low_price,
    }


def _send_alert_v26(
    stock, quote, ps, vol_state, trial, book, setup_name,
    score, grade, reasons, entry, stop, risk_pct, second_entry=False
):
    code = stock["code"]
    now = datetime.now(TW_TZ)
    scalp2 = v21.move_ticks(entry, -2)
    scalp3 = v21.move_ticks(entry, -3)
    scalp5 = v21.move_ticks(entry, -5)
    risk = stop - entry
    target_2r = v21.floor_to_tick(entry - risk * 2)

    v25._record_alert_state(code, entry, stop, scalp3, setup_name)
    legacy._alerted_today.add(code)
    legacy._today_trades.append({
        "code": code,
        "name": stock["name"],
        "market": stock.get("market"),
        "entry": entry,
        "stop": stop,
        "setup": setup_name,
        "target": target_2r,
        "target_2r": target_2r,
        "target_scalp": scalp3,
        "scalp_2": scalp2,
        "scalp_3": scalp3,
        "scalp_5": scalp5,
        "watch_line": ps.get("ceiling"),
        "time": now.strftime("%H:%M"),
        "grade": grade,
        "score": score,
        "strategy_type": stock.get("strategy_type"),
        "open_volume_today": vol_state.get("today"),
        "open_volume_safe": vol_state.get("safe_blue"),
        "open_volume_prev": vol_state.get("prev_red"),
        "open_volume_tier": vol_state.get("tier"),
        "reentry": bool(second_entry),
    })

    open_price = quote.get("open")
    open_pct = quote.get("open_pct")
    open_text = f"{open_price}（{open_pct:+.2f}%）" if open_price and open_pct is not None else "無資料"
    trial_text = "、".join(trial.get("notes", [])) if trial.get("available") else "未取得試撮歷史"
    vol_text = "、".join(vol_state.get("notes", []))
    red_text = f"{vol_state['prev_red']:,}張" if vol_state.get("prev_red") else "無資料"
    prev_status = "壓住" if ps.get("held_prev") else "已過"
    plus_status = "壓住" if ps.get("held_plus") else "已過"
    reason_text = "、".join(reasons)
    entry_tag = "♻️ 二次進場" if second_entry else "🎯 首次進場"

    # SMART_V1 is a shadow expectation only. It waits one tick above the
    # original reference entry, allows a two-tick zone, and never crosses the
    # existing structural stop. runtime_v2 records the same frozen rule.
    smart_zone_high = min(
        v21.move_ticks(entry, +2),
        v21.move_ticks(stop, -1),
    )
    if smart_zone_high < entry:
        smart_zone_high = entry
    smart_ideal = min(v21.move_ticks(entry, +1), smart_zone_high)
    if smart_ideal < entry:
        smart_ideal = entry

    alert = (
        f"🚨 <b>{grade}級短空候選｜V2.6｜{now.strftime('%H:%M')}</b>\n\n"
        f"<b>{code} {stock['name']}</b> [{stock.get('market','')}]｜分數 <b>{score}</b>\n"
        f"  🧩 {setup_name}｜{entry_tag}\n"
        f"  🧠 Smart V1：<b>{entry}~{smart_zone_high}</b>｜預期限價 <b>{smart_ideal}</b>｜20分未到取消（Shadow）\n"
        f"  ✅ {reason_text}\n"
        f"  📍 現價 <b>{quote['current']}</b>（{quote['pct']:+.2f}%）｜開盤 {open_text}\n"
        f"  🟢 昨高 {ps.get('prev_high')}【{prev_status}】｜+2.5% {ps.get('plus25')}【{plus_status}】\n"
        f"  🧪 試撮：{trial_text}\n"
        f"  🔵 2%≤{vol_state['safe_blue']:,}｜保1.6%≤{vol_state['protect_blue']:,}｜更保1.4%≤{vol_state['strict_blue']:,}\n"
        f"  🔴 前日開盤首筆：{red_text}\n"
        f"  📦 今日開盤首筆：{vol_state['today']:,}張｜{vol_text}\n"
        f"  📚 五檔：買 {book['bid_size']:,} / 賣 {book['ask_size']:,}｜{book['reason']}\n\n"
        f"  ━━━━━━ 紙上進場參考 ━━━━━━\n"
        f"  🎯 掛空參考：<b>{entry}</b>\n"
        f"  🛑 結構停損：<b>{stop}</b>（約 +{risk_pct:.2f}%）\n"
        f"  ⚡ Scalp：2檔 {scalp2}｜<b>3檔 {scalp3}</b>｜5檔 {scalp5}\n"
        f"  💰 2R：<b>{target_2r}</b>\n\n"
        f"⚠️ V2.6：全市場動態母池；鎖漲停A級維持舊嚴格條件，B級需<+{LOCKED_B_MAX_PCT:g}%且破開盤。"
    )
    legacy.tg_only(legacy.CHAT_ID, alert)


def intraday_monitor_v26():
    now = datetime.now(TW_TZ)
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5 or hm < 9 * 60 or hm >= MONITOR_END_MINUTE:
        return

    v25._reset_signal_state()
    v21._reset_v2_state_if_needed()
    legacy.reset_daily_state()

    if not legacy._watchlist_today:
        candidates = screen_v26()
        if candidates:
            v21._populate_watchlist(candidates)
    if not legacy._watchlist_today:
        return

    for stock in list(legacy._watchlist_today):
        code = stock["code"]
        quote = v21.fugle_quote_v2(code)
        if not quote:
            continue

        if (
            not stock.get("limit_up_locked")
            and v24._is_extreme_ma5(stock)
            and quote.get("pct") is not None
            and quote["pct"] >= v24.EXTREME_GAIN_WAIT_PCT
        ):
            continue

        current = quote["current"]
        pct_now = quote["pct"]
        open_price = quote.get("open")
        open_pct = quote.get("open_pct")
        day_high = quote.get("day_high") or current
        prev_high = stock.get("prev_high") or legacy.get_prev_day_high(code, stock.get("market"))
        ps = v23.price_state(stock, day_high, prev_high)

        # Normal stocks still require both price references to remain meaningful.
        # Locked-limit stocks use the separate <6% + break-open logic below.
        if ps.get("cancel") and not stock.get("limit_up_locked"):
            continue

        locked_high_pct = None
        if stock.get("limit_up_locked"):
            locked_high_pct = (
                (day_high - stock["close"]) / stock["close"] * 100
                if stock.get("close") else 999
            )
            if locked_high_pct >= LOCKED_B_MAX_PCT or pct_now >= LOCKED_B_MAX_PCT:
                continue

        today_open = v24._today_open_auction_v24(stock)
        vol_state = v23.opening_volume_state_v23(stock, today_open)
        if not vol_state.get("available") or vol_state.get("danger"):
            continue

        bars = v25.fetch_intraday_1m_v25(code)
        v25._refresh_reentry_state(code, bars)
        can_alert, alert_kind = v25._can_alert(code)
        if not can_alert:
            continue

        ceiling = ps.get("ceiling") or stock.get("watch_line")
        trial = v21._trial_summary(stock)
        book = v23.analyze_order_book_v23(code, quote, ceiling)
        if trial.get("danger") or book.get("danger"):
            continue

        setup_name = None
        ideal_pullback = False
        special_limit_setup = False
        locked_b_grade = False
        structural = None

        rebound = _local_rebound_setup_v26(stock, quote, bars, ps)
        if rebound:
            if stock.get("limit_up_locked"):
                # Locked-limit rebound still needs the B-grade weakness evidence.
                trial_ok = (not LOCKED_B_REQUIRE_TRIAL_SHRINK) or (
                    trial.get("available") and trial.get("volume_shrink")
                )
                volume_ok = vol_state.get("safe") or vol_state.get("weaker_than_prev")
                if not (trial_ok and volume_ok and open_price and current < open_price):
                    rebound = None
                else:
                    locked_b_grade = True
                    special_limit_setup = True
            if rebound:
                setup_name = rebound["setup_name"]
                if locked_b_grade:
                    setup_name = "鎖漲停<6%破開盤｜弱勢反彈B級"
                structural = rebound
                ideal_pullback = True

        if setup_name is None and hm < PRIMARY_END_MINUTE:
            pullback = (
                v21.VALID_PULLBACK_MIN <= pct_now < v21.VALID_PULLBACK_MAX
                and current < ceiling
            )
            ideal_pullback = v21.IDEAL_PULLBACK_MIN <= pct_now <= v21.IDEAL_PULLBACK_MAX
            break_open = bool(
                open_price
                and open_pct is not None
                and current < open_price
                and current < ceiling
                and (
                    open_pct < legacy.WEAK_OPEN_MAX_PCT
                    or (stock.get("two_strong_opens") and open_pct < legacy.HOT_MONEY_OPEN_MAX_PCT)
                )
            )

            if stock.get("limit_up_locked"):
                if not open_price or open_pct is None:
                    continue
                rally_from_open = (day_high - open_price) / open_price * 100 if open_price else 999

                # A-grade: preserve the original strict mentor setup.
                strict_a = (
                    open_pct < v24.LOCKED_OPEN_MAX_PCT
                    and rally_from_open < v24.LOCKED_RALLY_MAX_PCT
                    and current < open_price
                    and current < ceiling
                )

                # B-grade: newer note says locked-limit stock can be watched when
                # current gain is below +6% and it breaks the opening price.
                # Require weak trial/opening volume to avoid turning this into a
                # generic "short any failed limit-up" rule.
                trial_ok = (not LOCKED_B_REQUIRE_TRIAL_SHRINK) or (
                    trial.get("available") and trial.get("volume_shrink")
                )
                volume_ok = vol_state.get("safe") or vol_state.get("weaker_than_prev")
                broad_b = (
                    current < open_price
                    and pct_now < LOCKED_B_MAX_PCT
                    and (locked_high_pct is None or locked_high_pct < LOCKED_B_MAX_PCT)
                    and trial_ok
                    and volume_ok
                )

                if strict_a:
                    setup_name = "鎖漲停隔日沖轉弱｜A級"
                    special_limit_setup = True
                    ideal_pullback = True
                elif broad_b:
                    setup_name = "鎖漲停<6%破開盤｜B級"
                    special_limit_setup = True
                    locked_b_grade = True
                    ideal_pullback = False
            elif pullback or break_open:
                setup_name = "破開盤價弱勢" if break_open else "小拉升不過壓力"

            if setup_name:
                structural = v25._early_structural_stop(current, bars)

        if not setup_name or not structural:
            continue

        if stock.get("limit_up_failed"):
            limit_ref = stock.get("limit_up_reference")
            failed_special = bool(
                trial.get("available")
                and trial.get("volume_shrink")
                and current < (prev_high or ceiling)
                and (not limit_ref or current < limit_ref)
            )
            if failed_special:
                special_limit_setup = True
                setup_name += "｜漲停失敗停損壓力"

        score, grade, reasons = v23._confidence_v23(
            stock, setup_name, ideal_pullback, vol_state, trial, book, ps,
            special_limit_setup=special_limit_setup,
        )

        if setup_name.startswith("弱勢下殺後反彈空") or "弱勢反彈B級" in setup_name:
            score += 2
            reasons.append("弱勢反彈後再轉下")

        if locked_b_grade:
            score -= 1
            reasons.append("鎖漲停B級：<6%破開盤")
            # Recompute grade after the explicit B-grade penalty.
            grade = "A" if score >= 8 else "B" if score >= v21.ALERT_SCORE_MIN else "C"

        if alert_kind == "reentry":
            score += 1
            reasons.append("前次小停損後重新跌破")
            setup_name += "｜二次進場"
            grade = "A" if score >= 8 else "B" if score >= v21.ALERT_SCORE_MIN else "C"

        if score < v21.ALERT_SCORE_MIN:
            continue

        _send_alert_v26(
            stock, quote, ps, vol_state, trial, book, setup_name,
            score, grade, reasons,
            structural["entry"], structural["stop"], structural["risk_pct"],
            second_entry=(alert_kind == "reentry"),
        )
        time.sleep(0.2)


v25.intraday_monitor_v25 = intraday_monitor_v26
v24.intraday_monitor_v24 = intraday_monitor_v26
v23.intraday_monitor_v23 = intraday_monitor_v26
v22.intraday_monitor_v22 = intraday_monitor_v26
v21.intraday_monitor_v2 = intraday_monitor_v26
legacy.intraday_monitor = intraday_monitor_v26


def preopen_scan_once_v26():
    # Dynamic universe must be built before inherited V2.5 preopen code populates
    # the watchlist.
    if not legacy._watchlist_today:
        screen_v26()
    return _v25_preopen_scan()


def format_preopen_summary_v26():
    return (
        _v25_preopen_summary().replace("V2.5", "V2.6")
        + f"\n🌐 V2.6動態母池：全上市/上櫃先掃，再挑約{DYNAMIC_MAX_SYMBOLS}支做歷史深篩。"
        + f"\n🔒 鎖漲停：A級保留舊嚴格條件；B級需試撮/開盤量弱、漲幅<+{LOCKED_B_MAX_PCT:g}%並跌破開盤。"
    )


def format_report_v26(candidates):
    return (
        _v25_report(candidates).replace("V2.5", "V2.6")
        + f"\n🌐 動態市場：全市場{_market_stats['all_rows']}支／"
          f"第一階段{_market_stats['eligible_rows']}支／深篩{_market_stats['prefilter_symbols']}支。"
        + f"\n🔒 鎖漲停新增B級：<+{LOCKED_B_MAX_PCT:g}%＋破開盤＋量縮；A級舊條件仍優先。"
    )


def format_line_morning_v26(candidates):
    return (
        _v25_line_morning(candidates).replace("V2.5", "V2.6")
        + f"\nV2.6：母池改全市場動態；鎖漲停B級需<+{LOCKED_B_MAX_PCT:g}%破開盤且量縮。"
    )


def format_daily_summary_v26():
    return _v25_daily_summary().replace("V2.5", "V2.6")


def format_test_v26():
    return (
        _v25_format_test().replace("V2.5", "V2.6")
        + f"\n✅ V2.6動態母池：TWSE+TPEx官方全市場→最多{DYNAMIC_MAX_SYMBOLS}支歷史深篩"
        + f"\n✅ V2.6流動性預篩：至少{DYNAMIC_MIN_VOLUME:,}張；官方API失敗自動退回舊母池"
        + f"\n✅ V2.6鎖漲停B級：最高/目前<+{LOCKED_B_MAX_PCT:g}%＋破開盤＋量縮"
        + "\n✅ V2.6鎖漲停A級：開<2%＋反彈<1%＋破開盤的舊嚴格條件保留"
    )


legacy.format_report = format_report_v26
legacy.format_line_morning = format_line_morning_v26
legacy.format_daily_summary = format_daily_summary_v26
legacy.format_test = format_test_v26

v25.preopen_scan_once_v25 = preopen_scan_once_v26
v25.format_preopen_summary_v25 = format_preopen_summary_v26


def _v26_status():
    return {
        "status": "ok",
        "version": "2.6",
        "mode": "alerts_only",
        "dynamic_market": dict(_market_stats),
        "dynamic_max_symbols": DYNAMIC_MAX_SYMBOLS,
        "dynamic_min_volume": DYNAMIC_MIN_VOLUME,
        "locked_b_max_pct": LOCKED_B_MAX_PCT,
        "max_structural_risk_pct": MAX_STRUCTURAL_RISK_PCT,
        "primary_end": "10:00",
        "secondary_end": "11:30",
        "time": datetime.now(TW_TZ).isoformat(),
    }


if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v26_status

logger.info("Short-bot strategy V2.6 overlay loaded")
