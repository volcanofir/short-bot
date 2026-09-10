"""Short-bot strategy overlay V2.5.

V2.5 keeps V2.4 and applies post-trade review lessons:
- use a local rebound/swing high + one tick as the structural stop, not the whole
  session high; if structural risk is >0.8%, wait instead of chasing.
- after an initial selloff, allow a "weak rebound short" when price rebounds
  from a local low, stalls below the price references, then turns down.
- a stopped-out setup may be re-evaluated once for a second entry after a fresh
  breakdown; a stock is no longer permanently blocked by the first alert.
- extend only the secondary rebound scan to 11:30, at a slower cadence in the
  production runtime.
- keep V2.4 opening-auction/red-volume, price, MA5 +6% wait, and order-book rules.

Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime

import requests

import strategy_v24 as v24

v23 = v24.v23
v22 = v24.v22
v21 = v24.v21
app = v24.app
legacy = v24.legacy
logger = v24.logger
TW_TZ = v24.TW_TZ
UA = v24.UA

_trial_history = v24._trial_history
_book_history = v24._book_history
_open_store = v24._open_store
_first_bar_cache = v24._first_bar_cache

SAFE_OPEN_NORMAL_PCT = v24.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = v24.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = v24.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = v24.SAFE_OPEN_VOL_PCT

MAX_STRUCTURAL_RISK_PCT = float(os.environ.get("V25_MAX_STRUCTURAL_RISK_PCT", "0.8"))
REBOUND_MIN_DROP_PCT = float(os.environ.get("V25_REBOUND_MIN_DROP_PCT", "0.8"))
REBOUND_MIN_BOUNCE_PCT = float(os.environ.get("V25_REBOUND_MIN_BOUNCE_PCT", "0.4"))
REBOUND_TURN_TICKS = max(1, int(os.environ.get("V25_REBOUND_TURN_TICKS", "1")))
REBOUND_LOOKBACK_BARS = max(6, int(os.environ.get("V25_REBOUND_LOOKBACK_BARS", "20")))
MAX_ALERTS_PER_SYMBOL = max(1, int(os.environ.get("V25_MAX_ALERTS_PER_SYMBOL", "2")))
REENTRY_COOLDOWN_SECONDS = max(30, int(os.environ.get("V25_REENTRY_COOLDOWN_SECONDS", "120")))

PRIMARY_END_MINUTE = 10 * 60
MONITOR_END_MINUTE = 11 * 60 + 30
legacy.INTRADAY_ALERT_END_HOUR = 12

_v24_screen = v24.screen_v24
_v24_daily_summary = v24.format_daily_summary_v24
_v24_format_test = v24.format_test_v24
_v24_preopen_scan = v24.preopen_scan_once_v24
_v24_preopen_summary = v24.format_preopen_summary_v24
_v24_report = v24.format_report_v24
_v24_line_morning = v24.format_line_morning_v24

_signal_state = defaultdict(dict)
_signal_state_date = None


def _reset_signal_state():
    global _signal_state_date
    today = datetime.now(TW_TZ).date()
    if _signal_state_date != today:
        _signal_state.clear()
        _signal_state_date = today


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TW_TZ)
    except Exception:
        return None


def fetch_intraday_1m_v25(code: str):
    """Fetch today's regular-lot 1-minute candles from Fugle."""
    if not legacy.FUGLE_TOKEN:
        return []
    try:
        url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/{code}"
        r = requests.get(
            url,
            params={"timeframe": "1", "sort": "asc"},
            headers={"X-API-KEY": legacy.FUGLE_TOKEN},
            timeout=10,
        )
        if r.status_code != 200:
            logger.info("V2.5 intraday candles %s status=%s", code, r.status_code)
            return []
        out = []
        today = datetime.now(TW_TZ).date()
        for row in r.json().get("data", []) or []:
            dt = _parse_dt(row.get("date"))
            if not dt or dt.date() != today or not (9 <= dt.hour <= 13):
                continue
            try:
                out.append({
                    "dt": dt,
                    "open": float(row.get("open")),
                    "high": float(row.get("high")),
                    "low": float(row.get("low")),
                    "close": float(row.get("close")),
                    "volume": int(row.get("volume") or 0),
                })
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: x["dt"])
        return out
    except Exception as exc:
        logger.debug("V2.5 intraday candles %s: %s", code, exc)
        return []


def _bars_after(bars, when):
    if not when:
        return bars
    return [b for b in bars if b["dt"] >= when]


def _trade_outcome_since_alert(state, bars):
    """Conservative stop-first resolution for paper re-entry eligibility."""
    when = state.get("alert_at")
    stop = state.get("stop")
    target = state.get("target_scalp")
    if not when or stop is None or target is None:
        return None
    for bar in _bars_after(bars, when):
        if bar["high"] >= stop:
            return "stop"
        if bar["low"] <= target:
            return "win"
    return None


def _refresh_reentry_state(code, bars):
    state = _signal_state[code]
    if not state or state.get("resolved"):
        return
    outcome = _trade_outcome_since_alert(state, bars)
    if outcome == "win":
        state["resolved"] = "win"
    elif outcome == "stop":
        state["resolved"] = "stop"
        state["stopped_at"] = datetime.now(TW_TZ)


def _can_alert(code):
    state = _signal_state[code]
    alerts = int(state.get("alerts", 0))
    if alerts >= MAX_ALERTS_PER_SYMBOL:
        return False, None
    if alerts == 0:
        return True, "first"
    if state.get("resolved") != "stop":
        return False, None
    stopped_at = state.get("stopped_at")
    if stopped_at and (datetime.now(TW_TZ) - stopped_at).total_seconds() < REENTRY_COOLDOWN_SECONDS:
        return False, None
    return True, "reentry"


def _record_alert_state(code, entry, stop, scalp3, setup_name):
    prev_alerts = int(_signal_state[code].get("alerts", 0))
    _signal_state[code] = {
        "alerts": prev_alerts + 1,
        "alert_at": datetime.now(TW_TZ),
        "entry": entry,
        "stop": stop,
        "target_scalp": scalp3,
        "setup": setup_name,
        "resolved": None,
    }


def _local_rebound_setup(stock, quote, bars, ps):
    """Return a weak-rebound setup with a tight structural stop, or None."""
    if len(bars) < 4:
        return None
    window = bars[-REBOUND_LOOKBACK_BARS:]
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
    turn_level = v21.move_ticks(rebound_high, -REBOUND_TURN_TICKS)

    if drop_pct < REBOUND_MIN_DROP_PCT:
        return None
    if bounce_pct < REBOUND_MIN_BOUNCE_PCT:
        return None
    if current > turn_level:
        return None
    if ps.get("cancel"):
        return None

    if stock.get("limit_up_locked"):
        open_pct = quote.get("open_pct")
        if open_pct is None or open_pct >= v24.LOCKED_OPEN_MAX_PCT:
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


def _early_structural_stop(current, bars):
    """Use recent local highs for early setups; reject if risk exceeds 0.8%."""
    if not bars:
        return None
    recent = bars[-min(len(bars), 6):]
    local_high = max(b["high"] for b in recent)
    stop = v21.move_ticks(local_high, +1)
    entry = v21.nearest_tick(current)
    risk_pct = (stop - entry) / entry * 100 if entry > 0 else 999
    if stop <= entry or risk_pct > MAX_STRUCTURAL_RISK_PCT:
        return None
    return {"entry": entry, "stop": stop, "risk_pct": risk_pct, "structural_high": local_high}


def _send_alert(stock, quote, ps, vol_state, trial, book, setup_name,
                score, grade, reasons, entry, stop, risk_pct, second_entry=False):
    code = stock["code"]
    now = datetime.now(TW_TZ)
    scalp2 = v21.move_ticks(entry, -2)
    scalp3 = v21.move_ticks(entry, -3)
    scalp5 = v21.move_ticks(entry, -5)
    risk = stop - entry
    target_2r = v21.floor_to_tick(entry - risk * 2)

    _record_alert_state(code, entry, stop, scalp3, setup_name)
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

    alert = (
        f"🚨 <b>{grade}級短空候選｜V2.5｜{now.strftime('%H:%M')}</b>\n\n"
        f"<b>{code} {stock['name']}</b> [{stock.get('market','')}]｜分數 <b>{score}</b>\n"
        f"  🧩 {setup_name}｜{entry_tag}\n"
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
        f"⚠️ V2.5不再用整天最高價當停損；結構風險>{MAX_STRUCTURAL_RISK_PCT:g}%就等下一次反彈。"
    )
    legacy.tg_only(legacy.CHAT_ID, alert)


def intraday_monitor_v25():
    now = datetime.now(TW_TZ)
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5 or hm < 9 * 60 or hm >= MONITOR_END_MINUTE:
        return

    _reset_signal_state()
    v21._reset_v2_state_if_needed()
    legacy.reset_daily_state()

    if not legacy._watchlist_today:
        candidates = _v24_screen()
        if candidates:
            v21._populate_watchlist(candidates)
    if not legacy._watchlist_today:
        return

    for stock in list(legacy._watchlist_today):
        code = stock["code"]
        quote = v21.fugle_quote_v2(code)
        if not quote:
            continue

        if (not stock.get("limit_up_locked")
                and v24._is_extreme_ma5(stock)
                and quote.get("pct") is not None
                and quote["pct"] >= v24.EXTREME_GAIN_WAIT_PCT):
            continue

        current = quote["current"]
        pct_now = quote["pct"]
        open_price = quote.get("open")
        open_pct = quote.get("open_pct")
        day_high = quote.get("day_high") or current
        prev_high = stock.get("prev_high") or legacy.get_prev_day_high(code, stock.get("market"))
        ps = v23.price_state(stock, day_high, prev_high)
        if ps.get("cancel"):
            continue

        if stock.get("limit_up_locked"):
            high_pct = (day_high - stock["close"]) / stock["close"] * 100 if stock.get("close") else 0
            if high_pct >= v24.LOCKED_HARD_AVOID_PCT or pct_now >= v24.LOCKED_HARD_AVOID_PCT:
                continue

        today_open = v24._today_open_auction_v24(stock)
        vol_state = v23.opening_volume_state_v23(stock, today_open)
        if not vol_state.get("available") or vol_state.get("danger"):
            continue

        bars = fetch_intraday_1m_v25(code)
        _refresh_reentry_state(code, bars)
        can_alert, alert_kind = _can_alert(code)
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
        structural = None

        rebound = _local_rebound_setup(stock, quote, bars, ps)
        if rebound:
            setup_name = rebound["setup_name"]
            structural = rebound
            ideal_pullback = True

        if setup_name is None and hm < PRIMARY_END_MINUTE:
            pullback = (
                v21.VALID_PULLBACK_MIN <= pct_now < v21.VALID_PULLBACK_MAX
                and current < ceiling
            )
            ideal_pullback = v21.IDEAL_PULLBACK_MIN <= pct_now <= v21.IDEAL_PULLBACK_MAX
            break_open = bool(
                open_price and open_pct is not None
                and current < open_price and current < ceiling
                and (
                    open_pct < legacy.WEAK_OPEN_MAX_PCT
                    or (stock.get("two_strong_opens") and open_pct < legacy.HOT_MONEY_OPEN_MAX_PCT)
                )
            )

            if stock.get("limit_up_locked"):
                if not open_price or open_pct is None:
                    continue
                rally_from_open = (day_high - open_price) / open_price * 100 if open_price else 999
                if (
                    open_pct < v24.LOCKED_OPEN_MAX_PCT
                    and rally_from_open < v24.LOCKED_RALLY_MAX_PCT
                    and current < open_price and current < ceiling
                ):
                    setup_name = "鎖漲停隔日沖轉弱"
                    special_limit_setup = True
                    ideal_pullback = True
            elif pullback or break_open:
                setup_name = "破開盤價弱勢" if break_open else "小拉升不過壓力"

            if setup_name:
                structural = _early_structural_stop(current, bars)

        if not setup_name or not structural:
            continue

        if stock.get("limit_up_failed"):
            limit_ref = stock.get("limit_up_reference")
            failed_special = bool(
                trial.get("available") and trial.get("volume_shrink")
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
        if setup_name.startswith("弱勢下殺後反彈空"):
            score += 2
            reasons.append("弱勢反彈後再轉下")
        if alert_kind == "reentry":
            score += 1
            reasons.append("前次小停損後重新跌破")
            setup_name += "｜二次進場"

        if score < v21.ALERT_SCORE_MIN:
            continue

        entry = structural["entry"]
        stop = structural["stop"]
        risk_pct = structural["risk_pct"]

        _send_alert(
            stock, quote, ps, vol_state, trial, book, setup_name,
            score, grade, reasons, entry, stop, risk_pct,
            second_entry=(alert_kind == "reentry"),
        )
        time.sleep(0.2)


v24.intraday_monitor_v24 = intraday_monitor_v25
v23.intraday_monitor_v23 = intraday_monitor_v25
v22.intraday_monitor_v22 = intraday_monitor_v25
v21.intraday_monitor_v2 = intraday_monitor_v25
legacy.intraday_monitor = intraday_monitor_v25


def screen_v25(force: bool = False):
    return _v24_screen(force=force)


legacy.screen = screen_v25


def preopen_scan_once_v25():
    return _v24_preopen_scan()


def format_preopen_summary_v25():
    return (
        _v24_preopen_summary().replace("V2.4", "V2.5")
        + f"\n🛑 V2.5結構停損上限：{MAX_STRUCTURAL_RISK_PCT:g}%；超過就等更好的反彈位置。"
        + "\n♻️ 若第一筆被結構停損，後續重新轉弱可再評估一次，不永久封鎖該股。"
    )


def format_report_v25(candidates):
    return (
        _v24_report(candidates).replace("V2.4", "V2.5")
        + f"\n\nV2.5：弱勢下殺後反彈可做第二類進場；結構停損需≤{MAX_STRUCTURAL_RISK_PCT:g}%."
        + "\n09:00~10:00主策略；10:00~11:30僅保留較慢的弱勢反彈/二次進場掃描。"
    )


def format_line_morning_v25(candidates):
    return (
        _v24_line_morning(candidates).replace("V2.4", "V2.5")
        + "\nV2.5：若早盤先殺，不追低；等反彈形成局部壓力再轉弱，結構停損≤0.8%才提醒。"
    )


def format_daily_summary_v25():
    text = _v24_daily_summary().replace("V2.4", "V2.5")
    reentries = sum(1 for t in legacy._today_trades if t.get("reentry"))
    return text + f"\n♻️ V2.5二次進場提醒：{reentries}筆"


def format_test_v25():
    return (
        _v24_format_test().replace("V2.4", "V2.5")
        + f"\n✅ V2.5結構停損：局部反彈高點上一檔，風險≤{MAX_STRUCTURAL_RISK_PCT:g}%"
        + f"\n✅ V2.5弱勢反彈：跌離開盤≥{REBOUND_MIN_DROP_PCT:g}%後，反彈≥{REBOUND_MIN_BOUNCE_PCT:g}%再轉弱"
        + f"\n✅ V2.5再進場：首筆停損後最多再提醒1次（每股上限{MAX_ALERTS_PER_SYMBOL}次）"
        + "\n✅ V2.5時段：主策略至10:00；反彈/二次進場延伸至11:30"
    )


legacy.format_report = format_report_v25
legacy.format_line_morning = format_line_morning_v25
legacy.format_daily_summary = format_daily_summary_v25
legacy.format_test = format_test_v25

v24.preopen_scan_once_v24 = preopen_scan_once_v25
v24.format_preopen_summary_v24 = format_preopen_summary_v25


def _v25_status():
    return {
        "status": "ok",
        "version": "2.5",
        "mode": "alerts_only",
        "max_structural_risk_pct": MAX_STRUCTURAL_RISK_PCT,
        "primary_end": "10:00",
        "secondary_end": "11:30",
        "max_alerts_per_symbol": MAX_ALERTS_PER_SYMBOL,
        "rebound_min_drop_pct": REBOUND_MIN_DROP_PCT,
        "rebound_min_bounce_pct": REBOUND_MIN_BOUNCE_PCT,
        "tracked_signal_states": len(_signal_state),
        "time": datetime.now(TW_TZ).isoformat(),
    }


if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v25_status

logger.info("Short-bot strategy V2.5 overlay loaded")
