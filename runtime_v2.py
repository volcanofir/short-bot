"""Production runtime shim for Short-bot V2.5.

Runs the mentor-note/post-trade V2.5 strategy with:
- 09:00~09:15: 30-second scans
- 09:15~10:00: 60-second scans
- 10:00~11:30: 120-second secondary rebound/re-entry scans
"""

import time
import threading
from datetime import datetime

import strategy_v25 as v2

app = v2.app
legacy = v2.legacy
TW_TZ = v2.TW_TZ
logger = v2.logger

_base_intraday_monitor = legacy.intraday_monitor
_base_handle_update = legacy.handle_update
_scan_lock = threading.Lock()
_last_scan_ts = 0.0
_last_scan_text = None
MIN_SCAN_GAP_SECONDS = 20


def _monitor_window(now):
    hm = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 <= hm < v2.MONITOR_END_MINUTE


def guarded_intraday_monitor():
    global _last_scan_ts, _last_scan_text
    now = datetime.now(TW_TZ)
    if not _monitor_window(now):
        return
    ts = time.time()
    if ts - _last_scan_ts < MIN_SCAN_GAP_SECONDS:
        return
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        ts = time.time()
        if ts - _last_scan_ts < MIN_SCAN_GAP_SECONDS:
            return
        _last_scan_ts = ts
        _last_scan_text = now.isoformat()
        _base_intraday_monitor()
    except Exception as exc:
        logger.error("V2.5 guarded intraday scan error: %s", exc)
    finally:
        _scan_lock.release()


legacy.intraday_monitor = guarded_intraday_monitor


def precise_intraday_loop():
    logger.info("V2.5 precise intraday loop started")
    next_due = 0.0
    while True:
        try:
            now = datetime.now(TW_TZ)
            hm = now.hour * 60 + now.minute
            if _monitor_window(now):
                if hm < 9 * 60 + 15:
                    interval = 30
                elif hm < 10 * 60:
                    interval = 60
                else:
                    interval = 120
                ts = time.time()
                if ts >= next_due:
                    guarded_intraday_monitor()
                    next_due = time.time() + interval
                time.sleep(1)
            elif now.weekday() < 5 and 8 * 60 + 55 <= hm < 9 * 60:
                next_due = 0.0
                time.sleep(1)
            else:
                next_due = 0.0
                time.sleep(30)
        except Exception as exc:
            logger.error("V2.5 precise loop error: %s", exc)
            time.sleep(5)


def runtime_status_text():
    now = datetime.now(TW_TZ)
    return (
        f"🧭 <b>Short-bot V2.5 Runtime</b>\n"
        f"時間：{now.strftime('%m/%d %H:%M:%S')}\n"
        f"最後精準掃描：{_last_scan_text or '尚未執行'}\n"
        f"觀察名單：{len(legacy._watchlist_today)} 支\n"
        f"今日已提醒：{len(legacy._alerted_today)} 支\n"
        f"試撮紀錄：{len(v2._trial_history)} 支\n"
        f"五檔紀錄：{len(v2._book_history)} 支\n"
        f"開盤首筆紀錄：{len(v2._open_store)} 筆\n"
        f"股票母池：{len(legacy.SYMBOLS)} 支\n"
        f"結構停損上限：{v2.MAX_STRUCTURAL_RISK_PCT:g}%\n"
        f"每股最多提醒：{v2.MAX_ALERTS_PER_SYMBOL} 次\n"
        "時段：09:00~10:00主策略；10:00~11:30弱勢反彈/二次進場\n"
        "模式：只提醒，不自動下單"
    )


def handle_update_runtime(update):
    update_id = update.get("update_id", 0)
    if update_id <= legacy.last_update_id:
        return
    msg = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if text in ["/trial", "試撮"] and chat_id:
        legacy.last_update_id = update_id
        try:
            v2.preopen_scan_once_v25()
        except Exception as exc:
            logger.info("manual V2.5 trial scan: %s", exc)
        legacy.tg_only(chat_id, v2.format_preopen_summary_v25())
        return
    if text in ["/status", "狀態"] and chat_id:
        legacy.last_update_id = update_id
        legacy.tg_only(chat_id, runtime_status_text())
        return
    _base_handle_update(update)


legacy.handle_update = handle_update_runtime


@app.route("/runtime-status")
def runtime_status():
    return {
        "status": "ok",
        "version": "2.5-runtime",
        "mode": "alerts_only",
        "last_precise_scan": _last_scan_text,
        "watchlist": len(legacy._watchlist_today),
        "alerted_today": len(legacy._alerted_today),
        "trial_symbols": len(v2._trial_history),
        "book_symbols": len(v2._book_history),
        "open_auction_store": len(v2._open_store),
        "symbols": len(legacy.SYMBOLS),
        "max_structural_risk_pct": v2.MAX_STRUCTURAL_RISK_PCT,
        "max_alerts_per_symbol": v2.MAX_ALERTS_PER_SYMBOL,
        "primary_end": "10:00",
        "secondary_end": "11:30",
        "rebound_min_drop_pct": v2.REBOUND_MIN_DROP_PCT,
        "rebound_min_bounce_pct": v2.REBOUND_MIN_BOUNCE_PCT,
        "time": datetime.now(TW_TZ).isoformat(),
    }


threading.Thread(
    target=precise_intraday_loop,
    daemon=True,
    name="v25-precise-intraday",
).start()
logger.info("Short-bot V2.5 production runtime loaded")
