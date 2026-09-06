"""Short-bot strategy overlay V2.2.

V2.2 keeps the V2.1 engine and adds the rules confirmed from the mentor's notes:

1. Price first: the weaker of previous-day high and previous close +2.5% is the
   key resistance. If price can break that level, do not short.
2. Blue number: safe first-bar volume = previous-day total volume * 2%.
3. Red number: previous-day ACTUAL first 1-minute opening-bar volume. Today's
   first 1-minute bar should preferably be below both the blue threshold and
   the red comparison volume.
4. Previous-day locked limit-up: next day only consider the special short when
   open < +2%, rebound from the open stays < +1%, price breaks the open, and
   the session has not rallied to +6% or more.
5. Failed limit-up: weak trial matching + inability to retake prior high /
   limit-up reference adds expected stop-loss selling pressure.

This overlay remains alerts/paper-analysis only. It never places orders.
"""

from __future__ import annotations

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

import strategy_v2 as base

# Re-export what runtime_v2 expects.
app = base.app
legacy = base.legacy
logger = base.logger
TW_TZ = base.TW_TZ
UA = base.UA
_trial_history = base._trial_history
_book_history = base._book_history

# ---------------------------------------------------------------------------
# V2.2 configuration
# ---------------------------------------------------------------------------
SAFE_OPEN_VOL_PCT = float(os.environ.get("V22_SAFE_OPEN_VOL_PCT", "0.02"))
LOOSE_OPEN_VOL_PCT = float(os.environ.get("V22_LOOSE_OPEN_VOL_PCT", "0.03"))
REQUIRE_RED_VOLUME_SHRINK = os.environ.get("V22_REQUIRE_RED_VOLUME_SHRINK", "true").lower() == "true"

LOCKED_OPEN_MAX_PCT = float(os.environ.get("V22_LOCKED_OPEN_MAX_PCT", "2.0"))
LOCKED_RALLY_MAX_PCT = float(os.environ.get("V22_LOCKED_RALLY_MAX_PCT", "1.0"))
LOCKED_HARD_AVOID_PCT = float(os.environ.get("V22_LOCKED_HARD_AVOID_PCT", "6.0"))
LIMIT_TOUCH_PCT = float(os.environ.get("V22_LIMIT_TOUCH_PCT", "9.5"))

FIRST_BAR_CACHE_SECONDS = int(os.environ.get("V22_FIRST_BAR_CACHE_SECONDS", "900"))
FIRST_BAR_WORKERS = max(1, int(os.environ.get("V22_FIRST_BAR_WORKERS", "4")))

_first_bar_cache = {}
_first_bar_lock = threading.Lock()

# Preserve V2.1 functions before replacing module globals.
_v21_metrics_from_rows = base._metrics_from_rows
_v21_profile_for_symbol = base._profile_for_symbol
_v21_screen = base.screen_v2
_v21_format_daily_summary = base.format_daily_summary_v2
_v21_format_backtest = base.format_backtest_v2
_v21_format_test = base.format_test_v2
_v21_preopen_scan_once = base.preopen_scan_once


# ---------------------------------------------------------------------------
# Limit-up classification
# ---------------------------------------------------------------------------
def _limit_profile(rows, idx: int):
    if idx <= 0 or idx >= len(rows):
        return {
            "limit_up_locked": False,
            "limit_up_failed": False,
            "limit_up_reference": None,
            "day_pct": 0.0,
        }

    day = rows[idx]
    prev_close = rows[idx - 1]["close"]
    if not prev_close:
        return {
            "limit_up_locked": False,
            "limit_up_failed": False,
            "limit_up_reference": None,
            "day_pct": 0.0,
        }

    day_pct = (day["close"] - prev_close) / prev_close * 100
    high_pct = (day["high"] - prev_close) / prev_close * 100
    tick = base.tick_size(day["high"])
    limit_ref = base.nearest_tick(prev_close * 1.10)

    touched = high_pct >= LIMIT_TOUCH_PCT
    # "Locked" means it reached the daily upper-limit area and closed at the
    # high (allow one tick tolerance for feed rounding).
    locked = touched and day_pct >= LIMIT_TOUCH_PCT and (day["high"] - day["close"]) <= tick + 1e-9
    failed = touched and not locked and (day["high"] - day["close"]) >= tick - 1e-9

    return {
        "limit_up_locked": bool(locked),
        "limit_up_failed": bool(failed),
        "limit_up_reference": limit_ref if touched else None,
        "day_pct": round(day_pct, 2),
    }


def _metrics_from_rows_v22(rows, idx: int):
    metrics = _v21_metrics_from_rows(rows, idx)
    if metrics is None:
        return None

    lp = _limit_profile(rows, idx)
    types = list(metrics.get("strategy_types", []))
    notes = list(metrics.get("daily_notes", []))
    score = int(metrics.get("strategy_score", 0))

    if lp["limit_up_locked"]:
        if "鎖漲停" not in types:
            types.append("鎖漲停")
        notes.append("前日鎖漲停")
        score += 3
    elif lp["limit_up_failed"]:
        if "漲停失敗" not in types:
            types.append("漲停失敗")
        notes.append("前日碰漲停未鎖住")
        score += 2

    metrics.update(lp)
    metrics["strategy_types"] = types
    metrics["strategy_score"] = score
    metrics["daily_notes"] = notes
    metrics["eligible"] = bool(types)
    return metrics


def _profile_for_symbol_v22(symbol: str, target_date):
    rows = base._fetch_daily_rows(symbol)
    if not rows:
        return None
    eligible_indices = [i for i, row in enumerate(rows) if row["date"] <= target_date]
    if not eligible_indices:
        return None

    idx = eligible_indices[-1]
    metrics = _metrics_from_rows_v22(rows, idx)
    if not metrics or not metrics["eligible"]:
        return None

    today = metrics["today"]
    if today["close"] <= 0 or today["close"] > legacy.SCREEN_MAX_PRICE:
        return None

    code = symbol.replace(".TW", "").replace(".TWO", "")
    market = "上市" if symbol.endswith(".TW") else "上櫃"
    name = legacy.STOCK_NAMES.get(code, code)
    trade = base.calc_trade_v2(today["close"])

    return {
        "market": market,
        "code": code,
        "name": name,
        "symbol": symbol,
        "close": round(today["close"], 2),
        "pct": metrics["pct"],
        "vol": today["vol"],
        "prev_high": round(today["high"], 2),
        "rel_vol": metrics["rel_vol"],
        "avg5_vol": metrics["avg5_vol"],
        "three_day_gain": metrics["three_day_gain"],
        "ten_day_gain": metrics["ten_day_gain"],
        "sma5": metrics["sma5"],
        "sma5_extension": metrics["sma5_extension"],
        "three_day_no_high": metrics["three_day_no_high"],
        "two_strong_opens": metrics["two_strong_opens"],
        "strategy_types": metrics["strategy_types"],
        "strategy_type": "+".join(metrics["strategy_types"]),
        "strategy_score": metrics["strategy_score"],
        "daily_notes": metrics["daily_notes"],
        "limit_up_locked": metrics.get("limit_up_locked", False),
        "limit_up_failed": metrics.get("limit_up_failed", False),
        "limit_up_reference": metrics.get("limit_up_reference"),
        "safe_open_volume": max(1, int(today["vol"] * SAFE_OPEN_VOL_PCT)),
        "loose_open_volume": max(1, int(today["vol"] * LOOSE_OPEN_VOL_PCT)),
        **trade,
    }


# Monkey-patch base globals used internally by its already-running preopen loop.
base._metrics_from_rows = _metrics_from_rows_v22
base._profile_for_symbol = _profile_for_symbol_v22


# ---------------------------------------------------------------------------
# First 1-minute opening-bar volume (red number)
# ---------------------------------------------------------------------------
def fetch_first_bar_volume(symbol: str, target_date, force: bool = False):
    """Return the 09:00 1-minute bar volume in lots for target_date.

    Yahoo's 1-minute history is only retained for a short recent window, which
    is sufficient for the live next-trading-day comparison (e.g. Friday->Monday).
    """
    key = (symbol, str(target_date))
    now_ts = time.time()
    with _first_bar_lock:
        cached = _first_bar_cache.get(key)
        if cached and not force and now_ts - cached["ts"] < FIRST_BAR_CACHE_SECONDS:
            return cached["value"]

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1m", "range": "7d", "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return None
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        timestamps = chart.get("timestamp", [])
        volumes = q.get("volume", [])

        candidates = []
        for ts, vol in zip(timestamps, volumes):
            if vol is None:
                continue
            dt = datetime.fromtimestamp(ts, TW_TZ)
            if dt.date() != target_date:
                continue
            if dt.hour == 9 and 0 <= dt.minute <= 2:
                candidates.append((dt, int(vol) // 1000))
        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        value = candidates[0][1]
        # Avoid caching a possibly incomplete current 09:00 bar before 09:01.
        now = datetime.now(TW_TZ)
        final_enough = target_date < now.date() or (target_date == now.date() and (now.hour > 9 or now.minute >= 1))
        if final_enough:
            with _first_bar_lock:
                _first_bar_cache[key] = {"ts": now_ts, "value": value}
        return value
    except Exception as exc:
        logger.debug("first-bar volume %s %s: %s", symbol, target_date, exc)
        return None


def _enrich_open_volume(c):
    c = dict(c)
    symbol = c.get("symbol") or legacy.symbol_for_code(c["code"], c.get("market"))
    target_date = legacy.get_last_trading_day()
    c["safe_open_volume"] = max(1, int(c["vol"] * SAFE_OPEN_VOL_PCT))
    c["loose_open_volume"] = max(1, int(c["vol"] * LOOSE_OPEN_VOL_PCT))
    c["prev_open_volume"] = fetch_first_bar_volume(symbol, target_date)
    return c


# ---------------------------------------------------------------------------
# Screening: V2.1 universe + limit-up types + red opening volume
# ---------------------------------------------------------------------------
def screen_v22(force: bool = False):
    # base.screen_v2 now uses our patched profile/metrics functions.
    candidates = _v21_screen(force=force)
    if not candidates:
        return []

    enriched = []
    with ThreadPoolExecutor(max_workers=FIRST_BAR_WORKERS) as ex:
        futs = {ex.submit(_enrich_open_volume, c): c for c in candidates}
        for fut in as_completed(futs):
            try:
                enriched.append(fut.result())
            except Exception:
                enriched.append(dict(futs[fut]))

    enriched.sort(
        key=lambda x: (x.get("strategy_score", 0), x.get("rel_vol", 0), x.get("vol", 0)),
        reverse=True,
    )
    for c in enriched:
        c["signal"] = base._signal_for_candidate(c)

    logger.info("V2.2 screen: %s candidates enriched with first-bar data", len(enriched))
    return enriched


# Ensure all V2.1 functions that look up screen_v2 by module-global name use V2.2.
base.screen_v2 = screen_v22
legacy.screen = screen_v22


# ---------------------------------------------------------------------------
# Volume interpretation
# ---------------------------------------------------------------------------
def _today_first_bar(stock):
    now = datetime.now(TW_TZ)
    if now.hour == 9 and now.minute < 1:
        return None
    symbol = stock.get("symbol") or legacy.symbol_for_code(stock["code"], stock.get("market"))
    return fetch_first_bar_volume(symbol, now.date(), force=False)


def opening_volume_state(stock, today_first_bar):
    safe_blue = max(1, int(stock.get("safe_open_volume") or stock["vol"] * SAFE_OPEN_VOL_PCT))
    loose = max(1, int(stock.get("loose_open_volume") or stock["vol"] * LOOSE_OPEN_VOL_PCT))
    prev_red = stock.get("prev_open_volume")

    if today_first_bar is None:
        return {
            "available": False,
            "safe": False,
            "weaker_than_prev": False,
            "danger": False,
            "today": None,
            "safe_blue": safe_blue,
            "prev_red": prev_red,
            "ratio_prev": None,
            "notes": ["第一盤量尚未完成"],
        }

    safe = today_first_bar <= safe_blue
    weaker = prev_red is not None and prev_red > 0 and today_first_bar < prev_red
    ratio_prev = today_first_bar / prev_red if prev_red else None

    danger = False
    notes = []
    if safe:
        notes.append(f"低於藍字安全量{safe_blue:,}")
    elif today_first_bar <= loose:
        notes.append(f"高於藍字但仍低於3%放寬量{loose:,}")
    else:
        notes.append(f"第一盤量{today_first_bar:,}高於放寬量{loose:,}")

    if prev_red:
        if weaker:
            notes.append(f"低於前日紅字{prev_red:,}（{ratio_prev*100:.0f}%）")
        else:
            notes.append(f"未低於前日紅字{prev_red:,}（{ratio_prev*100:.0f}%）")
            if REQUIRE_RED_VOLUME_SHRINK:
                danger = True
    elif today_first_bar > loose:
        danger = True

    return {
        "available": True,
        "safe": safe,
        "weaker_than_prev": weaker,
        "danger": danger,
        "today": today_first_bar,
        "safe_blue": safe_blue,
        "prev_red": prev_red,
        "ratio_prev": ratio_prev,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Preopen formatting (trial match + blue/red references)
# ---------------------------------------------------------------------------
def preopen_scan_once_v22():
    # The captured V2.1 function uses module globals that now point at
    # screen_v22, so we retain the original implementation without recursion.
    return _v21_preopen_scan_once()


def format_preopen_summary_v22():
    rows = []
    for stock in legacy._watchlist_today:
        s = base._trial_summary(stock)
        if not s["available"]:
            continue
        score = 2 if s.get("good") else 1 if s.get("volume_shrink") else 0
        if s.get("danger"):
            score -= 2
        rows.append((score, stock, s))
    rows.sort(key=lambda x: x[0], reverse=True)

    if not rows:
        return "🧪 <b>08:44 試撮摘要｜V2.2</b>\n\n目前觀察名單沒有可用試撮資料"

    lines = ["🧪 <b>08:44 試撮摘要｜窮大叔量價法 V2.2</b>"]
    for score, stock, s in rows[:8]:
        icon = "🟢" if s.get("good") else "🔴" if s.get("danger") else "🟡"
        notes = "、".join(s.get("notes", [])) or "資料不足"
        blue = max(1, int(stock.get("safe_open_volume") or stock["vol"] * SAFE_OPEN_VOL_PCT))
        red = stock.get("prev_open_volume")
        red_text = f"{red:,}張" if red else "無資料"
        flag = "｜前日鎖漲停" if stock.get("limit_up_locked") else "｜前日漲停失敗" if stock.get("limit_up_failed") else ""
        lines.append(
            f"{icon} <b>{stock['code']} {stock['name']}</b>{flag}\n"
            f"  試撮 {s.get('price_pct', 0):+.2f}%｜末筆量 {s.get('size',0):,}張｜{notes}\n"
            f"  🔵第一盤安全量 ≤{blue:,}張｜🔴前日實際第一盤 {red_text}"
        )
    lines.append("\n09:00後價格優先：過昨高/+2.5%即取消空方想法；第一盤量越縮越安全")
    return "\n".join(lines)


base.preopen_scan_once = preopen_scan_once_v22
base.format_preopen_summary = format_preopen_summary_v22


# ---------------------------------------------------------------------------
# Live V2.2 intraday logic
# ---------------------------------------------------------------------------
def _confidence_v22(stock, setup_name, ideal_pullback, volume_state, trial, book,
                    day_high, resistance_line, special_limit_setup=False):
    score = 0
    reasons = []

    if stock.get("strategy_score", 0) >= 4:
        score += 1
        reasons.append(stock.get("strategy_type", "量價候選"))
    if stock.get("rel_vol", 0) >= base.REL_VOL_STRONG:
        score += 1
        reasons.append(f"昨量比{stock['rel_vol']:.1f}x")

    if ideal_pullback:
        score += 2
        reasons.append("小拉約1%")
    else:
        score += 1

    if setup_name in ("破開盤價弱勢", "鎖漲停隔日沖轉弱"):
        score += 1
        reasons.append("跌破開盤價")

    if volume_state.get("safe"):
        score += 2
        reasons.append("第一盤低於藍字安全量")
    elif volume_state.get("weaker_than_prev"):
        score += 1
        reasons.append("第一盤低於前日紅字")

    if trial.get("good"):
        score += 1
        reasons.append("試撮量價吻合")
    elif trial.get("volume_shrink"):
        score += 1
        reasons.append("試撮量縮")

    if day_high < resistance_line:
        score += 1
        reasons.append("未過昨高/+2.5%")

    if book.get("weak_score", 0) > 0:
        score += min(book["weak_score"], 2)
        reasons.append("五檔轉弱")

    if special_limit_setup:
        score += 2
        reasons.append("隔日沖停損結構")

    if trial.get("danger") or book.get("danger") or volume_state.get("danger"):
        score -= 3

    grade = "A" if score >= 8 else "B" if score >= base.ALERT_SCORE_MIN else "C"
    return score, grade, reasons


def intraday_monitor_v22():
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5 or not (9 <= now.hour < legacy.INTRADAY_ALERT_END_HOUR):
        return

    base._reset_v2_state_if_needed()
    legacy.reset_daily_state()

    if not legacy._watchlist_today:
        logger.info("V2.2 intraday: watchlist empty; rebuilding from prior close")
        candidates = screen_v22()
        if not candidates:
            return
        base._populate_watchlist(candidates)

    for stock in list(legacy._watchlist_today):
        code = stock["code"]
        if code in legacy._alerted_today:
            continue

        quote = base.fugle_quote_v2(code)
        if not quote:
            continue

        current = quote["current"]
        pct_now = quote["pct"]
        open_price = quote.get("open")
        open_pct = quote.get("open_pct")
        day_high = quote.get("day_high") or current
        prev_high = stock.get("prev_high") or legacy.get_prev_day_high(code, stock.get("market"))
        resistance_line = min(stock["watch_line"], prev_high) if prev_high else stock["watch_line"]

        # PRICE FIRST: if the session has already taken out the weaker resistance,
        # the mentor's "weakness" premise is gone.
        if day_high >= resistance_line:
            logger.info("%s V2.2 price gate failed: high %s >= resistance %s", code, day_high, resistance_line)
            continue

        # We intentionally wait until the first 1-minute bar is finalized. This
        # prevents an incomplete 09:00 volume from looking artificially small.
        today_first = _today_first_bar(stock)
        vol_state = opening_volume_state(stock, today_first)
        if not vol_state["available"]:
            continue
        if vol_state["danger"]:
            logger.info("%s V2.2 opening volume not weak: %s", code, vol_state["notes"])
            continue

        pullback = base.VALID_PULLBACK_MIN <= pct_now < base.VALID_PULLBACK_MAX and current < resistance_line
        ideal_pullback = base.IDEAL_PULLBACK_MIN <= pct_now <= base.IDEAL_PULLBACK_MAX
        break_open = (
            open_price and open_pct is not None
            and current < open_price and current < resistance_line
            and (
                open_pct < legacy.WEAK_OPEN_MAX_PCT
                or (stock.get("two_strong_opens") and open_pct < legacy.HOT_MONEY_OPEN_MAX_PCT)
            )
        )

        setup_name = None
        special_limit_setup = False

        # Locked limit-up: use the stricter confirmed rule.
        if stock.get("limit_up_locked"):
            if not open_price or open_pct is None:
                continue
            high_pct = (day_high - stock["close"]) / stock["close"] * 100 if stock["close"] else 0
            rally_from_open = (day_high - open_price) / open_price * 100 if open_price else 999

            # "漲幅超過6%以上就不要冒險空" — hard avoid.
            if high_pct >= LOCKED_HARD_AVOID_PCT or pct_now >= LOCKED_HARD_AVOID_PCT:
                logger.info("%s locked-limit candidate rallied >= %.1f%%; hard avoid", code, LOCKED_HARD_AVOID_PCT)
                continue

            locked_setup = (
                open_pct < LOCKED_OPEN_MAX_PCT
                and rally_from_open < LOCKED_RALLY_MAX_PCT
                and current < open_price
                and current < resistance_line
            )
            if not locked_setup:
                continue
            setup_name = "鎖漲停隔日沖轉弱"
            special_limit_setup = True
            ideal_pullback = rally_from_open <= LOCKED_RALLY_MAX_PCT

        else:
            if not (pullback or break_open):
                continue
            setup_name = "破開盤價弱勢" if break_open else "小拉升不過基準"

        trial = base._trial_summary(stock)
        book = base.analyze_order_book_v2(code, quote, resistance_line)
        try:
            legacy.log_orderbook_snapshot(stock, quote, resistance_line, book, "watchlist_scan", setup_name)
        except Exception:
            pass

        if trial.get("danger") or book.get("danger"):
            continue

        # Failed-limit-up special pressure: requires weak trial matching and
        # inability to retake both prior high and the prior limit-up reference.
        if stock.get("limit_up_failed"):
            limit_ref = stock.get("limit_up_reference")
            below_limit_ref = not limit_ref or current < limit_ref
            failed_special = (
                trial.get("available")
                and trial.get("volume_shrink")
                and current < (prev_high or resistance_line)
                and below_limit_ref
            )
            if failed_special:
                special_limit_setup = True
                setup_name = setup_name + "｜漲停失敗停損壓力"
            else:
                # If this stock has no other V2.1 reason, do not use a failed
                # limit-up label alone without the weak trial-match evidence.
                other_types = [t for t in stock.get("strategy_types", []) if t != "漲停失敗"]
                if not other_types:
                    continue

        score, grade, reasons = _confidence_v22(
            stock, setup_name, ideal_pullback, vol_state, trial, book,
            day_high, resistance_line, special_limit_setup=special_limit_setup,
        )
        if score < base.ALERT_SCORE_MIN:
            continue

        stop_actual = base.move_ticks(day_high, +1)
        entry_cap = min(open_price, resistance_line) if open_price else resistance_line
        entry_low = base.nearest_tick(current)
        entry_high = min(base.move_ticks(current, +2), base.move_ticks(entry_cap, -1))
        if entry_high < entry_low:
            entry_high = entry_low
        entry_mid = base.nearest_tick((entry_low + entry_high) / 2)
        risk = stop_actual - entry_mid
        if risk <= 0:
            continue

        target_2r = base.floor_to_tick(entry_mid - risk * 2)
        scalp2 = base.move_ticks(entry_mid, -2)
        scalp3 = base.move_ticks(entry_mid, -3)
        scalp5 = base.move_ticks(entry_mid, -5)
        stop_pct = round(risk / entry_mid * 100, 2)

        legacy._alerted_today.add(code)
        legacy._today_trades.append({
            "code": code,
            "name": stock["name"],
            "market": stock.get("market"),
            "entry": entry_mid,
            "stop": stop_actual,
            "setup": setup_name,
            "target": target_2r,
            "target_2r": target_2r,
            "target_scalp": scalp3,
            "scalp_2": scalp2,
            "scalp_3": scalp3,
            "scalp_5": scalp5,
            "watch_line": resistance_line,
            "time": now.strftime("%H:%M"),
            "grade": grade,
            "score": score,
            "strategy_type": stock.get("strategy_type"),
            "open_volume_today": vol_state.get("today"),
            "open_volume_safe": vol_state.get("safe_blue"),
            "open_volume_prev": vol_state.get("prev_red"),
        })

        trial_text = "、".join(trial.get("notes", [])) if trial.get("available") else "未取得試撮歷史"
        daily_text = "、".join(stock.get("daily_notes", [])) or "量價候選"
        broker_text = "、".join(stock.get("broker_notes", [])) or (
            "未接分點資料" if not legacy.FINMIND_TOKEN else "分點無明顯異常"
        )
        reason_text = "、".join(reasons)
        open_text = f"{open_price}（{open_pct:+.2f}%）" if open_price and open_pct is not None else "無資料"
        vol_text = "、".join(vol_state["notes"])
        limit_text = (
            "前日鎖漲停"
            if stock.get("limit_up_locked")
            else "前日漲停失敗"
            if stock.get("limit_up_failed")
            else "一般強勢股"
        )

        red_line = (
            f"  🔴 前日實際第一盤：{vol_state['prev_red']:,}張\n"
            if vol_state.get("prev_red")
            else "  🔴 前日實際第一盤：無資料\n"
        )
        alert = (
            f"🚨 <b>{grade}級短空候選｜V2.2｜{now.strftime('%H:%M')}</b>\n\n"
            f"<b>{code} {stock['name']}</b> [{stock['market']}]｜分數 <b>{score}</b>\n"
            f"  🧩 類型：{stock.get('strategy_type','量價候選')}｜{limit_text}\n"
            f"  ✅ 原因：{reason_text}\n"
            f"  📊 昨日：收 {stock['close']}｜{stock['pct']:+.2f}%｜{stock['vol']:,}張｜量比 {stock.get('rel_vol',0):.1f}x\n"
            f"  🧭 日線：{daily_text}\n"
            f"  🧪 試撮：{trial_text}\n"
            f"  📍 現價：<b>{current}</b>（{pct_now:+.2f}%）｜開盤 {open_text}\n"
            f"  🟢 價格弱勢線：<b>{resistance_line}</b>（昨高 {prev_high or 'N/A'} / +2.5% {stock['watch_line']} 取低）\n"
            f"  🔵 安全第一盤量：≤{vol_state['safe_blue']:,}張\n"
            f"{red_line}"
        )
        alert += (
            f"  📦 今日第一盤：<b>{vol_state['today']:,}張</b>｜{vol_text}\n"
            f"  📚 五檔：買 {book['bid_size']:,} / 賣 {book['ask_size']:,}｜{book['reason']}\n"
            f"  🧾 分點：{broker_text}\n\n"
            f"  ━━━━━━ 紙上進場參考 ━━━━━━\n"
            f"  🎯 掛空：<b>{entry_low}~{entry_high}</b>（試算 {entry_mid}）\n"
            f"  🛑 停損：早盤高點上一檔 <b>{stop_actual}</b>（約 +{stop_pct:.2f}%）\n"
            f"  ⚡ Scalp：2檔 {scalp2}｜<b>3檔 {scalp3}</b>｜5檔 {scalp5}\n"
            f"  💰 2R：<b>{target_2r}</b>\n\n"
            f"⚠️ 價格優先；一旦突破弱勢線或鎖漲停股當日曾衝逾+6%，取消空方想法"
        )

        legacy.tg_only(legacy.CHAT_ID, alert)
        time.sleep(0.2)


base.intraday_monitor_v2 = intraday_monitor_v22
legacy.intraday_monitor = intraday_monitor_v22


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def format_report_v22(candidates):
    last_day = legacy.get_last_trading_day()
    next_day = legacy.get_next_trading_day(last_day)
    if not candidates:
        return (
            f"📊 <b>{last_day.strftime('%m/%d')} V2.2 收盤篩選</b>\n\n"
            "今日沒有符合爆量/乖離/高檔轉弱/漲停結構的標的"
        )

    base._populate_watchlist(candidates)
    lines = [
        f"📊 <b>{last_day.strftime('%m/%d')} 收盤｜{next_day.strftime('%m/%d')} V2.2觀察名單（{len(candidates)}支）</b>"
    ]
    for c in candidates:
        blue = max(1, int(c.get("safe_open_volume") or c["vol"] * SAFE_OPEN_VOL_PCT))
        red = c.get("prev_open_volume")
        red_text = f"{red:,}張" if red else "無資料"
        weak_line = min(c["watch_line"], c.get("prev_high") or c["watch_line"])
        notes = "、".join(c.get("daily_notes", [])) or "一般量價候選"
        limit_tag = "｜🔒前日鎖漲停" if c.get("limit_up_locked") else "｜⚠️前日漲停失敗" if c.get("limit_up_failed") else ""
        lines.append(
            f"\n{c['signal']} <b>{c['code']} {c['name']}</b>｜{c.get('strategy_type','')}{limit_tag}\n"
            f"  收 {c['close']}｜{c['pct']:+.2f}%｜{c['vol']:,}張｜5日量比 <b>{c.get('rel_vol',0):.1f}x</b>\n"
            f"  3日 {c.get('three_day_gain',0):+.1f}%｜MA5乖離 {c.get('sma5_extension',0):+.1f}%｜{notes}\n"
            f"  🟢價格弱勢線：<b>{weak_line}</b>（昨高 {c.get('prev_high')} / +2.5% {c['watch_line']} 取低）\n"
            f"  🔵第一盤安全量：≤<b>{blue:,}</b>張（昨總量×2%）\n"
            f"  🔴前日實際第一盤：<b>{red_text}</b>\n"
            f"  Scalp參考：{c['scalp_2']} / {c['scalp_3']} / {c['scalp_5']}｜2R {c['target_2r']}"
        )

    lines.append(
        "\n規則：價格優先 → 試撮量縮 → 今日第一盤低於🔴前日量，最好也低於🔵2%安全量 → 再等破開盤價/小拉不過壓力\n"
        f"鎖漲停股：隔日開盤<+{LOCKED_OPEN_MAX_PCT:g}%、拉升<+{LOCKED_RALLY_MAX_PCT:g}%後破開盤才看；曾衝+{LOCKED_HARD_AVOID_PCT:g}%以上不空\n"
        "⚠️ 只做候選提醒，不自動下單"
    )
    return "\n".join(lines)


def format_line_morning_v22(candidates):
    next_day = legacy.get_next_trading_day(legacy.get_last_trading_day())
    if not candidates:
        return f"📊 {next_day.strftime('%m/%d')} 今日無V2.2短空觀察標的"

    lines = [f"📊 {next_day.strftime('%m/%d')} 短空觀察 V2.2"]
    for c in candidates[:8]:
        blue = max(1, int(c.get("safe_open_volume") or c["vol"] * SAFE_OPEN_VOL_PCT))
        red = c.get("prev_open_volume")
        red_text = f"{red:,}" if red else "N/A"
        limit_tag = "鎖漲停" if c.get("limit_up_locked") else "漲停失敗" if c.get("limit_up_failed") else c.get("strategy_type", "")
        lines.append(
            f"\n{'🔴' if c.get('strategy_score',0)>=6 else '🟠'} {c['code']} {c['name']}｜{limit_tag}\n"
            f"昨收 {c['close']} {c['pct']:+.2f}%｜量比 {c.get('rel_vol',0):.1f}x\n"
            f"🟢弱勢線 {min(c['watch_line'], c.get('prev_high') or c['watch_line'])}\n"
            f"🔵安全量≤{blue:,}｜🔴前日第一盤 {red_text}\n"
            f"看：試撮弱 → 第一盤量縮 → 不過昨高/+2.5% → 破開盤/小拉不過"
        )
    return "\n".join(lines)


def format_daily_summary_v22():
    return _v21_format_daily_summary().replace("V2.1", "V2.2")


def format_backtest_v22(period_days, symbol_results, all_trades):
    text = _v21_format_backtest(period_days, symbol_results, all_trades).replace("V2.1", "V2.2")
    return (
        text
        + "\n\nℹ️ V2.2新增的『🔴前日實際第一盤量』與漲停隔日特殊條件目前以即時盤中為主；"
          "長週期歷史回測因1分K保留限制，未強行套用，避免假精準。"
    )


def format_test_v22():
    base_text = _v21_format_test().replace("V2.1", "V2.2")
    return (
        base_text
        + f"\n✅ 藍字安全第一盤：昨總量×{SAFE_OPEN_VOL_PCT*100:.1f}%"
        + "\n✅ 紅字比較：前一交易日09:00第一分鐘實際量"
        + f"\n✅ 鎖漲停隔日：開<+{LOCKED_OPEN_MAX_PCT:g}% / 拉<+{LOCKED_RALLY_MAX_PCT:g}% / +{LOCKED_HARD_AVOID_PCT:g}%以上避空"
    )


legacy.format_report = format_report_v22
legacy.format_line_morning = format_line_morning_v22
legacy.format_daily_summary = format_daily_summary_v22
legacy.format_backtest = format_backtest_v22
legacy.format_test = format_test_v22

# Also replace module globals used by V2.1 scheduled functions.
base.format_report_v2 = format_report_v22
base.format_line_morning_v2 = format_line_morning_v22
base.format_daily_summary_v2 = format_daily_summary_v22
base.format_backtest_v2 = format_backtest_v22
base.format_test_v2 = format_test_v22


def _index_v22():
    return "📈 短空機器人 V2.2 運行中（價格優先 + 藍字2%安全量 + 紅字前日第一盤 + 漲停隔日規則｜僅提醒不下單）"


legacy.app.view_functions["index"] = _index_v22


def _v22_status():
    return {
        "status": "ok",
        "version": "2.2",
        "mode": "alerts_only",
        "watchlist": len(legacy._watchlist_today),
        "alerted_today": len(legacy._alerted_today),
        "trial_symbols": len(_trial_history),
        "book_symbols": len(_book_history),
        "first_bar_cache": len(_first_bar_cache),
        "blue_safe_pct": SAFE_OPEN_VOL_PCT,
        "red_open_volume_compare": True,
        "time": datetime.now(TW_TZ).isoformat(),
    }


# The original decorator registered endpoint name "v2_status".
if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v22_status

logger.info("Short-bot strategy V2.2 overlay loaded")
