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
        if not candidates and not alerts:
            return False

        payload = {"p_candidates": candidates, "p_alerts": alerts}
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
                "Dashboard Supabase sync OK: candidates=%s alerts=%s",
                len(candidates), len(alerts),
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
        "runtime": {
            "version": "2.9-runtime",
            "last_precise_scan": _last_scan_text,
            "watchlist": len(legacy._watchlist_today),
            "alerted_today": len(legacy._alerted_today),
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
