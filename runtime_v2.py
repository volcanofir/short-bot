"""Production runtime shim for Short-bot V2.9.

Runs the V2.9 strategy with:
- dynamic TWSE/TPEx all-market prefilter before the historical deep scan
- locked-limit A/B setup handling
- 09:00~09:15: 30-second scans
- 09:15~10:00: 60-second scans
- 10:00~11:30: 120-second secondary rebound/re-entry scans
"""

import time
import threading
import hashlib
import json
from datetime import datetime, timedelta

import requests
from flask import jsonify, make_response, request

import strategy_v29 as v2

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

# Public Supabase connection information used only to validate the dashboard
# owner's access token. The publishable key is intentionally safe for clients;
# no service-role or secret key is stored in this repository.
SUPABASE_URL = "https://ebamqlnchakpwyuxzmnk.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_38LhI3s8eZNKkfMz4ID98A_JgR2SpJR"
DASHBOARD_ALLOWED_ORIGINS = {
    "https://volcanofir.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}
_dashboard_push_lock = threading.Lock()
_dashboard_last_fingerprint = None
_smart_lock = threading.Lock()
_smart_entries = {}
_smart_restore_done = False
SMART_ENTRY_TTL_MINUTES = 20
SMART_ENTRY_TRACK_MINUTES = 60


def _dashboard_cors(response):
    origin = request.headers.get("Origin", "")
    if origin in DASHBOARD_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Cache-Control"] = "no-store"
    return response


def _dashboard_owner():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_PUBLISHABLE_KEY,
                "Authorization": auth,
            },
            timeout=6,
        )
        if r.status_code != 200:
            return None
        user = r.json() or {}
        if (user.get("app_metadata") or {}).get("short_bot_role") != "owner":
            return None
        return user
    except Exception as exc:
        logger.info("dashboard auth check failed: %s", exc)
        return None


def _next_weekday(day):
    result = day
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def dashboard_candidate_scan_date(now=None):
    """Date represented by the in-memory watchlist.

    Before the 13:40 close scan, the watchlist belongs to the current session.
    After the 13:40 close scan (and on weekends), it belongs to the next
    trading session. This avoids the old get_last_trading_day() mismatch that
    made a live 09:xx dashboard appear under the previous date.
    """
    now = now or datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return _next_weekday(now.date())
    hm = now.hour * 60 + now.minute
    if hm >= 13 * 60 + 40:
        return legacy.get_next_trading_day(now.date())
    return now.date()


def _dashboard_candidates(now):
    scan_date = dashboard_candidate_scan_date(now)
    rows = []
    alerted = {str(x) for x in legacy._alerted_today}
    for stock in list(legacy._watchlist_today or []):
        code = str(stock.get("code") or "").strip()
        price = stock.get("close") or stock.get("current") or stock.get("entry")
        if not code or price in (None, 0):
            continue
        chip2 = stock.get("broker_2d") or {}
        rows.append({
            "scan_date": scan_date.isoformat(),
            "scan_time": now.strftime("%H:%M"),
            "code": code,
            "name": str(stock.get("name") or legacy.STOCK_NAMES.get(code, code)),
            "strategy": "V2.9",
            "price": float(price),
            "change": float(stock.get("pct") or 0),
            "setup": str(stock.get("strategy_type") or stock.get("setup") or "量價候選"),
            "chip": str(chip2.get("classification") or "資料不足"),
            "score": float(stock.get("strategy_score") or stock.get("score") or 0),
            "status": "已提醒" if scan_date == now.date() and code in alerted else "觀察中",
            "payload": {
                "runtime_smart_model": "SMART_V1",
                "market": stock.get("market"),
                "watch_line": stock.get("watch_line"),
                "prev_high": stock.get("prev_high"),
                "rel_vol": stock.get("rel_vol"),
                "broker_2d": {
                    "classification": chip2.get("classification"),
                    "top1_concentration_pct": chip2.get("top1_concentration_pct"),
                    "top3_concentration_pct": chip2.get("top3_concentration_pct"),
                    "major_buyer_overlap_pct": chip2.get("major_buyer_overlap_pct"),
                    "flip_to_sell_count": chip2.get("flip_to_sell_count"),
                    "high_turnover_share_pct": chip2.get("high_turnover_share_pct"),
                },
            },
        })
    return rows


def _dashboard_alerts(now):
    rows = []
    counters = {}
    session_date = now.date().isoformat()
    for trade in list(legacy._today_trades or []):
        code = str(trade.get("code") or "").strip()
        time_text = str(trade.get("time") or now.strftime("%H:%M"))[:5]
        if not code or trade.get("entry") in (None, 0):
            continue
        key = (code, time_text)
        counters[key] = counters.get(key, 0) + 1
        alert_id = f"{session_date}:{code}:{time_text}:{counters[key]}"
        rows.append({
            "id": alert_id,
            "scan_date": session_date,
            "alert_time": time_text,
            "code": code,
            "name": str(trade.get("name") or legacy.STOCK_NAMES.get(code, code)),
            "strategy": "V2.9",
            "entry": float(trade.get("entry")),
            "stop": float(trade.get("stop") or trade.get("entry")),
            "target": float(trade.get("target_2r") or trade.get("target") or trade.get("entry")),
            "score": float(trade.get("score") or 0),
            "grade": str(trade.get("grade") or ""),
            "setup": str(trade.get("setup") or trade.get("strategy_type") or ""),
            "reentry": bool(trade.get("reentry")),
            "payload": {
                "market": trade.get("market"),
                "target_scalp": trade.get("target_scalp"),
                "scalp_2": trade.get("scalp_2"),
                "scalp_3": trade.get("scalp_3"),
                "scalp_5": trade.get("scalp_5"),
                "watch_line": trade.get("watch_line"),
                "open_volume_today": trade.get("open_volume_today"),
                "open_volume_tier": trade.get("open_volume_tier"),
            },
        })
    return rows



def _parse_smart_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return TW_TZ.localize(dt)
        return dt.astimezone(TW_TZ)
    except Exception:
        return None


def _smart_session_end(day):
    return TW_TZ.localize(datetime(day.year, day.month, day.day, 11, 30))


def _smart_entry_from_alert(alert, now):
    signal = float(alert["entry"])
    stop = float(alert["stop"])
    zone_low = signal
    zone_high = min(
        float(v2.v21.move_ticks(signal, +2)),
        float(v2.v21.move_ticks(stop, -1)),
    )
    if zone_high < zone_low:
        zone_high = zone_low

    ideal = min(float(v2.v21.move_ticks(signal, +1)), zone_high)
    if ideal < zone_low:
        ideal = zone_low

    smart_risk = stop - ideal
    baseline_risk = stop - signal
    if smart_risk <= 0 or baseline_risk <= 0:
        return None

    signal_dt = TW_TZ.localize(
        datetime.strptime(
            f"{alert['scan_date']} {alert['alert_time']}",
            "%Y-%m-%d %H:%M",
        )
    )
    expires_at = min(
        signal_dt + timedelta(minutes=SMART_ENTRY_TTL_MINUTES),
        _smart_session_end(signal_dt.date()),
    )

    target_1r = float(v2.v21.floor_to_tick(ideal - smart_risk))
    target_2r = float(v2.v21.floor_to_tick(ideal - smart_risk * 2))
    baseline_target_1r = float(v2.v21.floor_to_tick(signal - baseline_risk))
    baseline_target_2r = float(v2.v21.floor_to_tick(signal - baseline_risk * 2))
    target_scalp3 = (
        (alert.get("payload") or {}).get("scalp_3")
        or float(v2.v21.move_ticks(ideal, -3))
    )

    return {
        "id": f"SMART:{alert['id']}",
        "source_alert_id": alert["id"],
        "scan_date": alert["scan_date"],
        "signal_time": alert["alert_time"],
        "code": alert["code"],
        "name": alert["name"],
        "strategy": "V2.9",
        "model": "SMART_V1",
        "signal_entry": signal,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "ideal_entry": ideal,
        "stop": stop,
        "target_scalp3": float(target_scalp3),
        "target_1r": target_1r,
        "target_2r": target_2r,
        "baseline_target_1r": baseline_target_1r,
        "baseline_target_2r": baseline_target_2r,
        "expires_at": expires_at.isoformat(),
        "status": "pending",
        "filled_at": None,
        "fill_price": None,
        "first_event": None,
        "first_event_at": None,
        "lowest_price": None,
        "highest_price": None,
        "mfe_pct": None,
        "mae_pct": None,
        "price_5m": None,
        "price_15m": None,
        "price_30m": None,
        "price_60m": None,
        "hit_scalp3": False,
        "hit_1r": False,
        "hit_2r": False,
        "hit_stop": False,
        "baseline_lowest_price": signal,
        "baseline_highest_price": signal,
        "baseline_mfe_pct": 0.0,
        "baseline_mae_pct": 0.0,
        "baseline_price_5m": None,
        "baseline_price_15m": None,
        "baseline_price_30m": None,
        "baseline_price_60m": None,
        "baseline_hit_1r": False,
        "baseline_hit_2r": False,
        "baseline_hit_stop": False,
        "baseline_first_event": None,
        "baseline_first_event_at": None,
        "baseline_done": False,
        "samples": 0,
        "payload": {
            "rule": "V2.9訊號後，上方1檔作為SMART_V1預期限價；20分鐘未成交取消",
            "zone_rule": "訊號價至上方2檔，且不得貼近/越過原結構停損",
            "tracking": "Bot訊號基準與SMART_V1皆依掃描頻率抽樣60分鐘；非逐筆tick回放",
            "setup": alert.get("setup"),
            "grade": alert.get("grade"),
            "score": alert.get("score"),
            "reentry": bool(alert.get("reentry")),
        },
    }

def _ensure_smart_entries(now):
    alerts = _dashboard_alerts(now)
    with _smart_lock:
        for alert in alerts:
            smart_id = f"SMART:{alert['id']}"
            if smart_id in _smart_entries:
                continue

            # Never invent a Smart Entry retroactively after a deploy/restart.
            # The prediction must be frozen at signal time to keep the research
            # free of hindsight bias. Restored rows come from Supabase instead.
            try:
                signal_dt = TW_TZ.localize(
                    datetime.strptime(
                        f"{alert['scan_date']} {alert['alert_time']}",
                        "%Y-%m-%d %H:%M",
                    )
                )
                if abs((now - signal_dt).total_seconds()) > 180:
                    continue
            except Exception:
                continue

            item = _smart_entry_from_alert(alert, now)
            if item:
                _smart_entries[smart_id] = item

        cutoff = now.date() - timedelta(days=2)
        stale = []
        for smart_id, item in _smart_entries.items():
            try:
                if datetime.strptime(item["scan_date"], "%Y-%m-%d").date() < cutoff:
                    stale.append(smart_id)
            except Exception:
                pass
        for smart_id in stale:
            _smart_entries.pop(smart_id, None)


def _smart_rows():
    with _smart_lock:
        return [dict(item) for item in _smart_entries.values()]


def _restore_pending_smart_entries():
    global _smart_restore_done
    if _smart_restore_done or not getattr(legacy, "TG_TOKEN", ""):
        return
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/fetch_short_bot_pending_smart_entries",
            headers={
                "apikey": SUPABASE_PUBLISHABLE_KEY,
                "Content-Type": "application/json",
                "x-short-bot-token": legacy.TG_TOKEN,
            },
            json={},
            timeout=10,
        )
        if r.status_code != 200:
            return
        rows = r.json() or []
        if isinstance(rows, dict):
            rows = rows.get("result") or rows.get("data") or []
        if not isinstance(rows, list):
            rows = []
        with _smart_lock:
            for item in rows:
                if isinstance(item, dict) and item.get("id"):
                    _smart_entries.setdefault(item["id"], item)
        _smart_restore_done = True
        logger.info("Smart Entry restore: %s active rows", len(rows))
    except Exception as exc:
        logger.info("Smart Entry restore error: %s", exc)


def _update_smart_entries(now):
    if now.weekday() >= 5:
        return

    with _smart_lock:
        active_ids = [
            smart_id
            for smart_id, item in _smart_entries.items()
            if item.get("status") in ("pending", "filled")
            or not bool(item.get("baseline_done"))
        ]

    for smart_id in active_ids:
        with _smart_lock:
            item = _smart_entries.get(smart_id)
            if not item:
                continue
            item = dict(item)

        expires_at = _parse_smart_dt(item.get("expires_at"))
        filled_at = _parse_smart_dt(item.get("filled_at"))
        try:
            signal_dt = TW_TZ.localize(
                datetime.strptime(
                    f"{item['scan_date']} {item['signal_time']}",
                    "%Y-%m-%d %H:%M",
                )
            )
        except Exception:
            signal_dt = now

        if item.get("status") == "pending" and expires_at and now >= expires_at:
            item["status"] = "expired"
            item["first_event"] = item.get("first_event") or "not_filled"
            item["first_event_at"] = item.get("first_event_at") or now.isoformat()

        needs_quote = (
            item.get("status") in ("pending", "filled")
            or not bool(item.get("baseline_done"))
        )
        if not needs_quote:
            with _smart_lock:
                _smart_entries[smart_id] = item
            continue

        try:
            quote = legacy.fugle_quote(item["code"])
        except Exception as exc:
            logger.debug("Smart Entry quote %s: %s", item.get("code"), exc)
            quote = None
        if not quote or quote.get("current") in (None, 0):
            with _smart_lock:
                _smart_entries[smart_id] = item
            continue

        price = float(quote["current"])
        item["samples"] = int(item.get("samples") or 0) + 1

        # Baseline = the original V2.9 alert entry. Track it even when SMART_V1
        # never fills, so we can distinguish "strategy was good" from
        # "smart waiting rule was too conservative".
        if not bool(item.get("baseline_done")):
            baseline = float(item["signal_entry"])
            b_low = min(float(item.get("baseline_lowest_price") or baseline), price)
            b_high = max(float(item.get("baseline_highest_price") or baseline), price)
            item["baseline_lowest_price"] = b_low
            item["baseline_highest_price"] = b_high
            item["baseline_mfe_pct"] = round((baseline - b_low) / baseline * 100, 4)
            item["baseline_mae_pct"] = round((b_high - baseline) / baseline * 100, 4)

            elapsed_signal = max(0.0, (now - signal_dt).total_seconds() / 60.0)
            for minutes, field in (
                (5, "baseline_price_5m"),
                (15, "baseline_price_15m"),
                (30, "baseline_price_30m"),
                (60, "baseline_price_60m"),
            ):
                if elapsed_signal >= minutes and item.get(field) is None:
                    item[field] = price

            if price <= float(item["baseline_target_1r"]):
                item["baseline_hit_1r"] = True
            if price <= float(item["baseline_target_2r"]):
                item["baseline_hit_2r"] = True
            if price >= float(item["stop"]):
                item["baseline_hit_stop"] = True

            if not item.get("baseline_first_event"):
                if item["baseline_hit_stop"]:
                    item["baseline_first_event"] = "stop_first"
                    item["baseline_first_event_at"] = now.isoformat()
                elif item["baseline_hit_2r"]:
                    item["baseline_first_event"] = "2r_first"
                    item["baseline_first_event_at"] = now.isoformat()

            if (
                elapsed_signal >= SMART_ENTRY_TRACK_MINUTES
                or now >= _smart_session_end(now.date())
            ):
                item["baseline_done"] = True
                if not item.get("baseline_first_event"):
                    item["baseline_first_event"] = "window_end"
                    item["baseline_first_event_at"] = now.isoformat()

        if item.get("status") == "pending":
            if price >= float(item["stop"]):
                item["status"] = "invalidated"
                item["first_event"] = "stop_before_fill"
                item["first_event_at"] = now.isoformat()
            elif price >= float(item["ideal_entry"]):
                item["status"] = "filled"
                item["filled_at"] = now.isoformat()
                item["fill_price"] = float(item["ideal_entry"])
                item["lowest_price"] = float(item["ideal_entry"])
                item["highest_price"] = float(item["ideal_entry"])
                filled_at = now

        if item.get("status") == "filled":
            fill = float(item.get("fill_price") or item["ideal_entry"])
            low = min(float(item.get("lowest_price") or fill), price)
            high = max(float(item.get("highest_price") or fill), price)
            item["lowest_price"] = low
            item["highest_price"] = high
            item["mfe_pct"] = round((fill - low) / fill * 100, 4)
            item["mae_pct"] = round((high - fill) / fill * 100, 4)

            filled_at = filled_at or _parse_smart_dt(item.get("filled_at")) or now
            elapsed = max(0.0, (now - filled_at).total_seconds() / 60.0)
            for minutes, field in (
                (5, "price_5m"),
                (15, "price_15m"),
                (30, "price_30m"),
                (60, "price_60m"),
            ):
                if elapsed >= minutes and item.get(field) is None:
                    item[field] = price

            if (
                item.get("target_scalp3") is not None
                and price <= float(item["target_scalp3"])
            ):
                item["hit_scalp3"] = True
            if price <= float(item["target_1r"]):
                item["hit_1r"] = True
            if price <= float(item["target_2r"]):
                item["hit_2r"] = True
            if price >= float(item["stop"]):
                item["hit_stop"] = True

            if not item.get("first_event"):
                if item["hit_stop"]:
                    item["first_event"] = "stop_first"
                    item["first_event_at"] = now.isoformat()
                elif item["hit_2r"]:
                    item["first_event"] = "2r_first"
                    item["first_event_at"] = now.isoformat()

            if (
                elapsed >= SMART_ENTRY_TRACK_MINUTES
                or now >= _smart_session_end(now.date())
            ):
                item["status"] = "completed"
                if not item.get("first_event"):
                    item["first_event"] = "window_end"
                    item["first_event_at"] = now.isoformat()

        with _smart_lock:
            _smart_entries[smart_id] = item

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
        _ensure_smart_entries(now)
        _push_dashboard_snapshot()
    except Exception as exc:
        logger.error("V2.9 guarded intraday scan error: %s", exc)
    finally:
        _scan_lock.release()


legacy.intraday_monitor = guarded_intraday_monitor


def precise_intraday_loop():
    logger.info("V2.9 precise intraday loop started")
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
            logger.error("V2.9 precise loop error: %s", exc)
            time.sleep(5)


def runtime_status_text():
    now = datetime.now(TW_TZ)
    stats = v2._market_stats
    return (
        f"🧭 <b>Short-bot V2.9 Runtime</b>\n"
        f"時間：{now.strftime('%m/%d %H:%M:%S')}\n"
        f"最後精準掃描：{_last_scan_text or '尚未執行'}\n"
        f"觀察名單：{len(legacy._watchlist_today)} 支\n"
        f"今日已提醒：{len(legacy._alerted_today)} 支\n"
        f"試撮紀錄：{len(v2._trial_history)} 支\n"
        f"五檔紀錄：{len(v2._book_history)} 支\n"
        f"開盤首筆紀錄：{len(v2._open_store)} 筆\n"
        f"全市場快照：{stats.get('all_rows', 0)} 支\n"
        f"動態第一階段：{stats.get('eligible_rows', 0)} 支\n"
        f"歷史深篩母池：{stats.get('prefilter_symbols', len(legacy.SYMBOLS))} 支\n"
        f"結構停損上限：{v2.MAX_STRUCTURAL_RISK_PCT:g}%\n"
        f"鎖漲停B級上限：+{v2.LOCKED_B_MAX_PCT:g}%\n"
        f"每股最多提醒：{v2.MAX_ALERTS_PER_SYMBOL} 次\n"
        f"分點籌碼：{'已啟用' if legacy.FINMIND_TOKEN else '未啟用'}｜近1/3/5日\n"
        f"乖離過大等待線：+{v2.EXTREME_GAIN_WAIT_PCT:g}%\n"
        f"處置股排除：{v2._disposal_stats.get('active', 0)} 支\n"
        f"2日籌碼分類：{v2._chip2_stats.get('classified', {})}\n"
        f"Smart Entry：SMART_V1｜記錄 {len(_smart_rows())} 筆\n"
        "指令：/brokers 全分點；/broker 6226 單股；/chips2 二日；/chip2 6226；/disposals 處置\n"
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
            v2.preopen_scan_once_v29()
        except Exception as exc:
            logger.info("manual V2.9 trial scan: %s", exc)
        _push_dashboard_snapshot(force=True)
        legacy.tg_only(chat_id, v2.format_preopen_summary_v29())
        return
    if text in ["/status", "狀態"] and chat_id:
        legacy.last_update_id = update_id
        legacy.tg_only(chat_id, runtime_status_text())
        return
    if text in ["/brokers", "分點"] and chat_id:
        legacy.last_update_id = update_id
        legacy.tg_only(chat_id, v2.format_broker_watchlist_v29())
        return
    if chat_id and (text.startswith("/broker ") or text.startswith("分點 ")):
        legacy.last_update_id = update_id
        code = text.split(maxsplit=1)[1].strip()
        legacy.tg_only(chat_id, v2.format_broker_detail_v29(code))
        return
    if text in ["/chips2", "2日籌碼", "二日籌碼"] and chat_id:
        legacy.last_update_id = update_id
        legacy.tg_only(chat_id, v2.format_chip2_watchlist_v29())
        return
    if chat_id and (text.startswith("/chip2 ") or text.startswith("2日籌碼 ") or text.startswith("二日籌碼 ")):
        legacy.last_update_id = update_id
        code = text.split(maxsplit=1)[1].strip()
        legacy.tg_only(chat_id, v2.format_chip2_detail_v29(code))
        return
    if text in ["/disposals", "處置"] and chat_id:
        legacy.last_update_id = update_id
        legacy.tg_only(chat_id, v2.v28.format_disposal_status_v28())
        return
    _base_handle_update(update)


legacy.handle_update = handle_update_runtime



def _push_dashboard_snapshot(force=False):
    """Persist Bot observations/alerts without storing a new database secret.

    The existing Telegram bot token is sent only over HTTPS to the Supabase RPC.
    PostgreSQL stores only its SHA-256 hash, which the authenticated owner
    registers from the private dashboard.
    """
    global _dashboard_last_fingerprint
    if not getattr(legacy, "TG_TOKEN", ""):
        return False
    if not _dashboard_push_lock.acquire(blocking=False):
        return False
    try:
        now = datetime.now(TW_TZ)
        candidates = _dashboard_candidates(now)
        alerts = _dashboard_alerts(now)
        smart_entries = _smart_rows()
        if not candidates and not alerts and not smart_entries:
            return False

        payload = {
            "p_candidates": candidates,
            "p_alerts": alerts,
            "p_smart_entries": smart_entries,
        }
        # Normalize any uncommon numeric/date-like values nested in diagnostic
        # payloads while preserving the explicit top-level numeric fields.
        payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not force and fingerprint == _dashboard_last_fingerprint:
            return True

        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/ingest_short_bot",
            headers={
                "apikey": SUPABASE_PUBLISHABLE_KEY,
                "Content-Type": "application/json",
                "x-short-bot-token": legacy.TG_TOKEN,
            },
            json=payload,
            timeout=10,
        )
        if r.status_code in (200, 201, 204):
            _dashboard_last_fingerprint = fingerprint
            logger.info(
                "Dashboard Supabase sync OK: candidates=%s alerts=%s smart=%s",
                len(candidates), len(alerts), len(smart_entries),
            )
            return True

        # 401/403 is expected only before the owner dashboard has registered
        # the Telegram-token hash for secure background ingest.
        logger.info("Dashboard Supabase sync status=%s", r.status_code)
        return False
    except Exception as exc:
        logger.info("Dashboard Supabase sync error: %s", exc)
        return False
    finally:
        _dashboard_push_lock.release()


def dashboard_sync_loop():
    logger.info("Dashboard Supabase sync loop started")
    while True:
        try:
            now = datetime.now(TW_TZ)
            _restore_pending_smart_entries()
            _ensure_smart_entries(now)
            _update_smart_entries(now)
            _push_dashboard_snapshot()
        except Exception as exc:
            logger.info("Dashboard sync loop error: %s", exc)
        time.sleep(60)


@app.route("/dashboard-bot-auth", methods=["GET", "OPTIONS"])
def dashboard_bot_auth():
    if request.method == "OPTIONS":
        return _dashboard_cors(make_response("", 204))
    if not _dashboard_owner():
        return _dashboard_cors(
            make_response(jsonify({"error": "unauthorized"}), 401)
        )
    token = getattr(legacy, "TG_TOKEN", "")
    if not token:
        return _dashboard_cors(
            make_response(jsonify({"error": "telegram token unavailable"}), 503)
        )
    response = jsonify({
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    })
    return _dashboard_cors(response)


@app.route("/dashboard-live", methods=["GET", "OPTIONS"])
def dashboard_live():
    if request.method == "OPTIONS":
        return _dashboard_cors(make_response("", 204))

    user = _dashboard_owner()
    if not user:
        return _dashboard_cors(
            make_response(jsonify({"error": "unauthorized"}), 401)
        )

    now = datetime.now(TW_TZ)
    response = jsonify({
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "candidate_scan_date": dashboard_candidate_scan_date(now).isoformat(),
        "candidates": _dashboard_candidates(now),
        "alerts": _dashboard_alerts(now),
        "smart_entries": _smart_rows(),
        "runtime": {
            "version": "2.9-runtime",
            "last_precise_scan": _last_scan_text,
            "watchlist": len(legacy._watchlist_today),
            "alerted_today": len(legacy._alerted_today),
            "smart_entries": len(_smart_rows()),
            "smart_model": "SMART_V1",
            "primary_end": "10:00",
            "secondary_end": "11:30",
        },
    })
    return _dashboard_cors(response)


@app.route("/runtime-status")
def runtime_status():
    return {
        "status": "ok",
        "version": "2.9-runtime",
        "mode": "alerts_only",
        "last_precise_scan": _last_scan_text,
        "watchlist": len(legacy._watchlist_today),
        "alerted_today": len(legacy._alerted_today),
        "trial_symbols": len(v2._trial_history),
        "book_symbols": len(v2._book_history),
        "open_auction_store": len(v2._open_store),
        "dynamic_market": dict(v2._market_stats),
        "dynamic_max_symbols": v2.DYNAMIC_MAX_SYMBOLS,
        "dynamic_min_volume": v2.DYNAMIC_MIN_VOLUME,
        "locked_b_max_pct": v2.LOCKED_B_MAX_PCT,
        "broker_inventory_enabled": bool(legacy.FINMIND_TOKEN),
        "broker_lookback_calendar_days": v2.BROKER_LOOKBACK_CAL_DAYS,
        "broker_top_n": v2.BROKER_TOP_N,
        "broker_cache_symbols": len({k[0] for k, item in v2._broker_cache.items() if item.get("value", {}).get("available")}),
        "extreme_gain_wait_pct": v2.EXTREME_GAIN_WAIT_PCT,
        "disposal": dict(v2._disposal_stats),
        "broker_2d": dict(v2._chip2_stats),
        "chip2_big_min_lots": v2.CHIP2_BIG_MIN_LOTS,
        "chip2_big_prev_volume_pct": v2.CHIP2_BIG_PREV_VOL_PCT,
        "chip2_major_top_n": v2.CHIP2_MAJOR_TOP_N,
        "chip2_chaos_score_min": v2.CHIP2_CHAOS_SCORE_MIN,
        "max_structural_risk_pct": v2.MAX_STRUCTURAL_RISK_PCT,
        "max_alerts_per_symbol": v2.MAX_ALERTS_PER_SYMBOL,
        "smart_entries": len(_smart_rows()),
        "smart_model": "SMART_V1",
        "primary_end": "10:00",
        "secondary_end": "11:30",
        "time": datetime.now(TW_TZ).isoformat(),
    }


threading.Thread(
    target=precise_intraday_loop,
    daemon=True,
    name="v29-precise-intraday",
).start()
threading.Thread(
    target=dashboard_sync_loop,
    daemon=True,
    name="dashboard-supabase-sync",
).start()
logger.info("Short-bot V2.9 production runtime loaded")
