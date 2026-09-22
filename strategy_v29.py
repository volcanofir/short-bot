"""Short-bot strategy overlay V2.9.

V2.9 keeps V2.8 and quantifies the mentor's "two-day chip chaos" idea for each
already-selected candidate. The new broker-branch metrics are:
- 2-day net-buy branch count
- 2-day large net-buy branch count
- Top1 / Top3 positive net-buy concentration
- top-buyer overlap between the latest two trading days
- prior-day buyers that flipped to net selling on the latest day
- high-turnover branch share
- final label: 集中 / 普通 / 分散雜亂

These metrics are context only. "分散雜亂" may add one soft confidence point
only after the price/volume strategy has already produced a valid short setup.
It never creates an entry by itself.

Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import strategy_v28 as v28

app = v28.app
legacy = v28.legacy
logger = v28.logger
TW_TZ = v28.TW_TZ

v27 = v28.v27
v26 = v28.v26
v25 = v28.v25
v24 = v28.v24
v23 = v28.v23
v22 = v28.v22
v21 = v28.v21

_trial_history = v28._trial_history
_book_history = v28._book_history
_open_store = v28._open_store
_first_bar_cache = v28._first_bar_cache
_market_stats = v28._market_stats
_broker_cache = v28._broker_cache
_disposal_stats = v28._disposal_stats

SAFE_OPEN_NORMAL_PCT = v28.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = v28.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = v28.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = v28.SAFE_OPEN_VOL_PCT
MAX_STRUCTURAL_RISK_PCT = v28.MAX_STRUCTURAL_RISK_PCT
REBOUND_MIN_DROP_PCT = v28.REBOUND_MIN_DROP_PCT
REBOUND_MIN_BOUNCE_PCT = v28.REBOUND_MIN_BOUNCE_PCT
MAX_ALERTS_PER_SYMBOL = v28.MAX_ALERTS_PER_SYMBOL
MONITOR_END_MINUTE = v28.MONITOR_END_MINUTE
PRIMARY_END_MINUTE = v28.PRIMARY_END_MINUTE
DYNAMIC_MIN_VOLUME = v28.DYNAMIC_MIN_VOLUME
DYNAMIC_MAX_SYMBOLS = v28.DYNAMIC_MAX_SYMBOLS
LOCKED_B_MAX_PCT = v28.LOCKED_B_MAX_PCT
EXTREME_GAIN_WAIT_PCT = v28.EXTREME_GAIN_WAIT_PCT
BROKER_LOOKBACK_CAL_DAYS = v28.BROKER_LOOKBACK_CAL_DAYS
BROKER_TOP_N = v28.BROKER_TOP_N

CHIP2_MIN_BRANCH_LOTS = max(
    20, int(os.environ.get("V29_CHIP2_MIN_BRANCH_LOTS", str(v27.BROKER_MIN_LOTS)))
)
CHIP2_BIG_MIN_LOTS = max(100, int(os.environ.get("V29_CHIP2_BIG_MIN_LOTS", "300")))
CHIP2_BIG_PREV_VOL_PCT = float(os.environ.get("V29_CHIP2_BIG_PREV_VOL_PCT", "0.5"))
CHIP2_MAJOR_TOP_N = max(3, min(10, int(os.environ.get("V29_CHIP2_MAJOR_TOP_N", "5"))))
CHIP2_TURNOVER_NET_RATIO = float(os.environ.get("V29_CHIP2_TURNOVER_NET_RATIO", "0.15"))
CHIP2_CHAOS_SCORE_MIN = max(2, int(os.environ.get("V29_CHIP2_CHAOS_SCORE_MIN", "3")))
CHIP2_CACHE_SECONDS = max(900, int(os.environ.get("V29_CHIP2_CACHE_SECONDS", "21600")))
CHIP2_WORKERS = max(1, min(6, int(os.environ.get("V29_CHIP2_WORKERS", "4"))))

_base_screen_v28 = v28.screen_v28
_base_preopen_v28 = v28.preopen_scan_once_v28
_base_preopen_summary = v28.format_preopen_summary_v28
_base_report = v28.format_report_v28
_base_line_morning = v28.format_line_morning_v28
_base_daily_summary = v28.format_daily_summary_v28
_base_format_test = v28.format_test_v28
_base_confidence = v26.core.v23._confidence_v23
_original_fetch_branch_rows = v27._fetch_finmind_branch_rows

_chip2_lock = threading.Lock()
_chip2_raw_cache = {}
_chip2_stats = {
    "enabled": bool(legacy.FINMIND_TOKEN),
    "cached_symbols": 0,
    "classified": {"集中": 0, "普通": 0, "分散雜亂": 0, "資料不足": 0},
}


def _raw_cache_key(code: str, target_date):
    return (str(code), target_date.isoformat())


def _fetch_branch_rows_cached(code: str, target_date, force: bool = False):
    key = _raw_cache_key(code, target_date)
    now_ts = time.time()
    with _chip2_lock:
        cached = _chip2_raw_cache.get(key)
        if cached and not force and now_ts - cached["ts"] < CHIP2_CACHE_SECONDS:
            return list(cached["rows"])

    rows = _original_fetch_branch_rows(str(code), target_date) or []
    with _chip2_lock:
        _chip2_raw_cache[key] = {"ts": now_ts, "rows": list(rows)}
        _chip2_stats["cached_symbols"] = len({
            k[0] for k, item in _chip2_raw_cache.items() if item.get("rows")
        })
    return rows


# V2.7 broker inventory and V2.9 two-day analytics share one raw FinMind response.
v27._fetch_finmind_branch_rows = _fetch_branch_rows_cached


def _branch_key(row):
    bid = str(
        row.get("securities_trader_id")
        or row.get("broker_id")
        or row.get("dealer_id")
        or ""
    ).strip()
    name = str(
        row.get("securities_trader_branch_name")
        or row.get("securities_trader")
        or row.get("broker")
        or row.get("dealer_name")
        or bid
        or ""
    ).strip()
    return (bid, name), name or bid


def _shares(value):
    try:
        return int(float(str(value or 0).replace(",", "").strip()))
    except Exception:
        return 0


def _daily_branch_nets(rows, target_date):
    """Aggregate each branch into buy/sell/net lots for the latest two trade dates."""
    raw = defaultdict(lambda: defaultdict(lambda: {"buy": 0, "sell": 0, "name": ""}))
    dates = set()

    for row in rows or []:
        d = str(row.get("date") or "")[:10]
        try:
            d_obj = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if d_obj > target_date:
            continue

        key, name = _branch_key(row)
        if not name:
            continue
        buy = _shares(row.get("buy") if row.get("buy") is not None else row.get("buy_volume"))
        sell = _shares(row.get("sell") if row.get("sell") is not None else row.get("sell_volume"))
        if buy == 0 and sell == 0:
            continue

        item = raw[d_obj][key]
        item["buy"] += buy
        item["sell"] += sell
        item["name"] = name
        dates.add(d_obj)

    ordered = sorted(dates)
    if len(ordered) < 2:
        return None

    prev_date, latest_date = ordered[-2], ordered[-1]
    out = {}
    keys = set(raw[prev_date]) | set(raw[latest_date])
    for key in keys:
        p = raw[prev_date].get(key, {"buy": 0, "sell": 0, "name": ""})
        c = raw[latest_date].get(key, {"buy": 0, "sell": 0, "name": ""})
        name = c.get("name") or p.get("name") or key[1] or key[0]
        prev_buy = p["buy"] / 1000.0
        prev_sell = p["sell"] / 1000.0
        cur_buy = c["buy"] / 1000.0
        cur_sell = c["sell"] / 1000.0
        out[key] = {
            "broker": name,
            "prev_buy": round(prev_buy, 1),
            "prev_sell": round(prev_sell, 1),
            "prev_net": round(prev_buy - prev_sell, 1),
            "cur_buy": round(cur_buy, 1),
            "cur_sell": round(cur_sell, 1),
            "cur_net": round(cur_buy - cur_sell, 1),
            "two_buy": round(prev_buy + cur_buy, 1),
            "two_sell": round(prev_sell + cur_sell, 1),
            "two_net": round((prev_buy + cur_buy) - (prev_sell + cur_sell), 1),
            "two_turnover": round(prev_buy + prev_sell + cur_buy + cur_sell, 1),
        }

    return {
        "prev_date": prev_date,
        "latest_date": latest_date,
        "branches": out,
    }


def _top_positive_set(branches, field, n=5):
    ranked = [
        (key, b[field])
        for key, b in branches.items()
        if b.get(field, 0) > 0
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return {key for key, value in ranked[:n] if value >= CHIP2_MIN_BRANCH_LOTS}


def _pct(num, den):
    return round(num / den * 100.0, 1) if den else 0.0


def compute_chip2_metrics_v29(stock, rows):
    daily = _daily_branch_nets(rows, legacy.get_last_trading_day())
    if not daily:
        return {
            "available": False,
            "classification": "資料不足",
            "reason": "不足2個交易日分點資料",
        }

    branches = daily["branches"]
    prev_vol = max(float(stock.get("vol") or 0), 0.0)
    big_threshold = max(
        float(CHIP2_BIG_MIN_LOTS),
        prev_vol * CHIP2_BIG_PREV_VOL_PCT / 100.0,
    )

    positive_2d = [b for b in branches.values() if b["two_net"] > 0]
    net_buyer_count = len(positive_2d)
    large_buyers = [b for b in positive_2d if b["two_net"] >= big_threshold]
    large_buyer_count = len(large_buyers)

    total_positive = sum(b["two_net"] for b in positive_2d)
    ranked_positive = sorted(
        positive_2d, key=lambda b: b["two_net"], reverse=True
    )
    top1 = ranked_positive[0]["two_net"] if ranked_positive else 0.0
    top3 = sum(b["two_net"] for b in ranked_positive[:3])
    top1_concentration = _pct(top1, total_positive)
    top3_concentration = _pct(top3, total_positive)

    prev_top = _top_positive_set(branches, "prev_net", CHIP2_MAJOR_TOP_N)
    cur_top = _top_positive_set(branches, "cur_net", CHIP2_MAJOR_TOP_N)
    if prev_top and cur_top:
        overlap_count = len(prev_top & cur_top)
        overlap_pct = _pct(overlap_count, min(len(prev_top), len(cur_top)))
    else:
        overlap_count = 0
        overlap_pct = 0.0

    flip_to_sell = [
        b for b in branches.values()
        if b["prev_net"] >= CHIP2_MIN_BRANCH_LOTS
        and b["cur_net"] <= -CHIP2_MIN_BRANCH_LOTS
    ]
    flip_to_sell_count = len(flip_to_sell)

    active = [
        b for b in branches.values()
        if b["two_turnover"] >= CHIP2_MIN_BRANCH_LOTS
    ]
    turnover_branches = [
        b for b in active
        if b["two_turnover"] >= max(big_threshold * 2.0, CHIP2_BIG_MIN_LOTS * 2.0)
        and abs(b["two_net"]) <= b["two_turnover"] * CHIP2_TURNOVER_NET_RATIO
    ]
    high_turnover_share = _pct(len(turnover_branches), len(active))

    chaos_score = 0
    reasons = []
    if net_buyer_count >= 8:
        chaos_score += 1
        reasons.append(f"2日淨買分點{net_buyer_count}家")
    if total_positive >= big_threshold and top3_concentration < 50:
        chaos_score += 1
        reasons.append(f"Top3僅{top3_concentration:.0f}%")
    if prev_top and cur_top and overlap_pct < 40:
        chaos_score += 1
        reasons.append(f"主要買方重疊{overlap_pct:.0f}%")
    if flip_to_sell_count >= 2:
        chaos_score += 1
        reasons.append(f"買轉賣{flip_to_sell_count}家")
    if high_turnover_share >= 40:
        chaos_score += 1
        reasons.append(f"高周轉占{high_turnover_share:.0f}%")

    concentrated = (
        total_positive > 0
        and top3_concentration >= 65
        and net_buyer_count <= 6
        and (not prev_top or not cur_top or overlap_pct >= 50)
        and high_turnover_share < 40
    )
    if chaos_score >= CHIP2_CHAOS_SCORE_MIN:
        classification = "分散雜亂"
    elif concentrated:
        classification = "集中"
    else:
        classification = "普通"

    top_buyers = [
        {
            "broker": b["broker"],
            "two_net": b["two_net"],
            "cur_net": b["cur_net"],
            "prev_net": b["prev_net"],
        }
        for b in ranked_positive[:5]
    ]
    flip_names = [
        {
            "broker": b["broker"],
            "prev_net": b["prev_net"],
            "cur_net": b["cur_net"],
        }
        for b in sorted(flip_to_sell, key=lambda b: b["cur_net"])[:5]
    ]

    return {
        "available": True,
        "prev_date": daily["prev_date"].isoformat(),
        "latest_date": daily["latest_date"].isoformat(),
        "net_buyer_count": net_buyer_count,
        "large_buyer_count": large_buyer_count,
        "large_threshold_lots": round(big_threshold, 0),
        "top1_concentration_pct": top1_concentration,
        "top3_concentration_pct": top3_concentration,
        "major_buyer_overlap_pct": overlap_pct,
        "major_buyer_overlap_count": overlap_count,
        "flip_to_sell_count": flip_to_sell_count,
        "high_turnover_share_pct": high_turnover_share,
        "active_branch_count": len(active),
        "high_turnover_branch_count": len(turnover_branches),
        "chaos_score": chaos_score,
        "classification": classification,
        "classification_reasons": reasons,
        "top_buyers": top_buyers,
        "flip_to_sell": flip_names,
    }


def _enrich_chip2(stock, force=False):
    c = dict(stock)
    target = legacy.get_last_trading_day()
    rows = _fetch_branch_rows_cached(c["code"], target, force=force)
    c["broker_2d"] = compute_chip2_metrics_v29(c, rows)
    return c


def screen_v29(force: bool = False):
    candidates = _base_screen_v28(force=force)
    if not candidates:
        return []

    if not legacy.FINMIND_TOKEN:
        return [
            dict(c, broker_2d={
                "available": False,
                "classification": "資料不足",
                "reason": "未設定FINMIND_API_TOKEN",
            })
            for c in candidates
        ]

    enriched = []
    with ThreadPoolExecutor(max_workers=CHIP2_WORKERS) as ex:
        futs = {ex.submit(_enrich_chip2, c, force): c for c in candidates}
        for fut in as_completed(futs):
            base = futs[fut]
            try:
                enriched.append(fut.result())
            except Exception as exc:
                logger.info("V2.9 chip2 enrich %s: %s", base.get("code"), exc)
                fallback = dict(base)
                fallback["broker_2d"] = {
                    "available": False,
                    "classification": "資料不足",
                    "reason": "2日分點分析失敗",
                }
                enriched.append(fallback)

    order = {c["code"]: i for i, c in enumerate(candidates)}
    enriched.sort(key=lambda c: order.get(c.get("code"), 9999))

    classified = {"集中": 0, "普通": 0, "分散雜亂": 0, "資料不足": 0}
    for c in enriched:
        label = c.get("broker_2d", {}).get("classification", "資料不足")
        classified[label] = classified.get(label, 0) + 1
    _chip2_stats["classified"] = classified

    logger.info(
        "V2.9 2-day broker chips enriched: total=%s chaos=%s",
        len(enriched), classified.get("分散雜亂", 0),
    )
    return enriched


# Patch every screen path that inherited preopen/report code can resolve.
v28.screen_v28 = screen_v29
v27.screen_v27 = screen_v29
v26.screen_v26 = screen_v29
v26.core.screen_v26 = screen_v29
v25.screen_v25 = screen_v29
v24.screen_v24 = screen_v29
v23.screen_v23 = screen_v29
v22.screen_v22 = screen_v29
v21.screen_v2 = screen_v29
legacy.screen = screen_v29


def _chip2_one_line(stock):
    m = stock.get("broker_2d") or {}
    if not m.get("available"):
        return f"{stock.get('code')} {stock.get('name')}｜2日籌碼資料不足"
    return (
        f"{stock.get('code')} {stock.get('name')}｜{m['classification']}｜"
        f"淨買{m['net_buyer_count']}家／大額{m['large_buyer_count']}家｜"
        f"Top1 {m['top1_concentration_pct']:.0f}%／Top3 {m['top3_concentration_pct']:.0f}%｜"
        f"重疊{m['major_buyer_overlap_pct']:.0f}%｜買轉賣{m['flip_to_sell_count']}家｜"
        f"高周轉{m['high_turnover_share_pct']:.0f}%"
    )


def format_chip2_watchlist_v29():
    items = list(legacy._watchlist_today or [])
    if not items:
        return "🧩 <b>2日分點結構｜V2.9</b>\n目前沒有觀察名單。"

    lines = ["🧩 <b>2日分點結構｜V2.9</b>"]
    for stock in items:
        if not stock.get("broker_2d"):
            try:
                stock.update(_enrich_chip2(stock))
            except Exception:
                pass
        lines.append("• " + _chip2_one_line(stock))
    lines.append("")
    lines.append("分類只做籌碼背景；不會單獨觸發空點。")
    return "\n".join(lines)


def format_chip2_detail_v29(code: str):
    code = str(code).strip()
    stock = next(
        (x for x in legacy._watchlist_today if str(x.get("code")) == code),
        None,
    )
    if stock is None:
        stock = {
            "code": code,
            "name": legacy.STOCK_NAMES.get(code, code),
            "vol": 0,
        }
    if not stock.get("broker_2d"):
        stock = _enrich_chip2(stock)

    m = stock.get("broker_2d") or {}
    if not m.get("available"):
        return (
            f"🧩 <b>{code} {stock.get('name', code)} 2日分點</b>\n"
            f"{m.get('reason', '資料不足')}"
        )

    lines = [
        f"🧩 <b>{code} {stock.get('name', code)}｜2日分點：{m['classification']}</b>",
        f"日期：{m['prev_date']} → {m['latest_date']}",
        f"淨買分點：{m['net_buyer_count']}家",
        f"大額淨買：{m['large_buyer_count']}家（門檻約{m['large_threshold_lots']:.0f}張）",
        f"Top1 / Top3買超集中：{m['top1_concentration_pct']:.1f}% / {m['top3_concentration_pct']:.1f}%",
        f"主要買方重疊率：{m['major_buyer_overlap_pct']:.1f}%",
        f"前日買、今日轉賣：{m['flip_to_sell_count']}家",
        f"高周轉分點占比：{m['high_turnover_share_pct']:.1f}%",
        f"雜亂分數：{m['chaos_score']}",
    ]
    if m.get("classification_reasons"):
        lines.append("判讀：" + "、".join(m["classification_reasons"]))

    if m.get("top_buyers"):
        lines.extend(["", "📈 <b>2日主要淨買</b>"])
        for b in m["top_buyers"]:
            lines.append(
                f"• {b['broker']}｜2日{b['two_net']:+g}張"
                f"（前{b['prev_net']:+g}／今{b['cur_net']:+g}）"
            )

    if m.get("flip_to_sell"):
        lines.extend(["", "🔻 <b>買超轉賣</b>"])
        for b in m["flip_to_sell"]:
            lines.append(
                f"• {b['broker']}｜前{b['prev_net']:+g} → 今{b['cur_net']:+g}張"
            )

    lines.extend([
        "",
        "⚠️ 分點是營業據點彙總資料；分類為策略背景，不代表單一主力帳戶。",
    ])
    return "\n".join(lines)


def _confidence_v29(
    stock, setup_name, ideal_pullback, volume_state, trial, book, ps,
    special_limit_setup=False,
):
    score, grade, reasons = _base_confidence(
        stock, setup_name, ideal_pullback, volume_state, trial, book, ps,
        special_limit_setup=special_limit_setup,
    )
    m = stock.get("broker_2d") or {}
    if m.get("available"):
        if m.get("classification") == "分散雜亂":
            score += 1
            reasons.append(
                f"2日籌碼分散雜亂：Top3 {m['top3_concentration_pct']:.0f}%"
                f"／重疊{m['major_buyer_overlap_pct']:.0f}%"
            )
        elif m.get("classification") == "集中":
            reasons.append(
                f"2日籌碼集中：Top3 {m['top3_concentration_pct']:.0f}%"
            )

    grade = "A" if score >= 8 else "B" if score >= v21.ALERT_SCORE_MIN else "C"
    return score, grade, reasons


# Existing intraday monitor resolves this confidence function dynamically.
v26.core.v23._confidence_v23 = _confidence_v29


def preopen_scan_once_v29():
    return _base_preopen_v28()


def format_preopen_summary_v29():
    base = _base_preopen_summary().replace("V2.8", "V2.9")
    items = list(legacy._watchlist_today or [])
    if not items:
        return base
    lines = ["", "🧩 <b>2日分點結構</b>"]
    for stock in items:
        lines.append("• " + _chip2_one_line(stock))
    return base + "\n" + "\n".join(lines)


def format_report_v29(candidates):
    text = _base_report(candidates).replace("V2.8", "V2.9")
    if candidates:
        lines = ["", "🧩 2日分點結構"]
        for c in candidates:
            lines.append("• " + _chip2_one_line(c))
        text += "\n" + "\n".join(lines)
    return text


def format_line_morning_v29(candidates):
    text = _base_line_morning(candidates).replace("V2.8", "V2.9")
    if candidates:
        lines = ["", "🧩 2日籌碼"]
        for c in candidates:
            lines.append("• " + _chip2_one_line(c))
        text += "\n" + "\n".join(lines)
    return text


def format_daily_summary_v29():
    return _base_daily_summary().replace("V2.8", "V2.9")


def format_test_v29():
    return (
        _base_format_test().replace("V2.8", "V2.9")
        + "\n✅ V2.9二日籌碼：淨買分點數／大額分點數／Top1與Top3集中度"
        + "\n✅ V2.9延續度：兩日主要買方重疊率＋前日買超今日轉賣家數"
        + "\n✅ V2.9周轉：高周轉分點占比＋集中/普通/分散雜亂分類"
        + "\n✅ V2.9雜亂僅在已有有效空方型態後軟性+1分，不單獨觸發進場"
    )


legacy.format_report = format_report_v29
legacy.format_line_morning = format_line_morning_v29
legacy.format_daily_summary = format_daily_summary_v29
legacy.format_test = format_test_v29


def _v29_status():
    return {
        "status": "ok",
        "version": "2.9",
        "mode": "alerts_only",
        "dynamic_market": dict(_market_stats),
        "disposal": dict(_disposal_stats),
        "extreme_gain_wait_pct": EXTREME_GAIN_WAIT_PCT,
        "broker_2d": dict(_chip2_stats),
        "chip2_big_min_lots": CHIP2_BIG_MIN_LOTS,
        "chip2_big_prev_volume_pct": CHIP2_BIG_PREV_VOL_PCT,
        "chip2_major_top_n": CHIP2_MAJOR_TOP_N,
        "chip2_chaos_score_min": CHIP2_CHAOS_SCORE_MIN,
        "time": datetime.now(TW_TZ).isoformat(),
    }


if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v29_status

logger.info("Short-bot strategy V2.9 two-day broker-chip overlay loaded")
