"""Short-bot strategy overlay V2.3.

V2.3 refines V2.2 with rules repeatedly confirmed from the mentor notebooks:
- price is primary, but prior-day high and prior-close +2.5% are TWO independent
  resistance references; only breaking both cancels the normal weak thesis.
- first-bar safety is shown in three tiers: 2.0%, 1.6% (保), 1.4% (更保).
- prior-day actual 09:00 one-minute volume (red number) is fetched from Fugle
  first, with Yahoo as a fallback.
- prior locked-limit names with preopen/next-day strength >= +6% are hard-avoid.
- thick bid stacks that fail to lift price, or are subsequently broken, add a
  weakness confirmation instead of being treated as support.
- high-then-retrace days are allowed into the candidate pool as a soft setup.
- notebook-repeat symbols missing from the legacy fixed universe are added.

Alerts/paper analysis only. No order placement.
"""

from __future__ import annotations

import os
import time
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

import strategy_v22 as v22

v21 = v22.base
app = v22.app
legacy = v22.legacy
logger = v22.logger
TW_TZ = v22.TW_TZ
UA = v22.UA
_trial_history = v22._trial_history
_book_history = v22._book_history

# ---------------------------------------------------------------------------
# V2.3 configuration
# ---------------------------------------------------------------------------
SAFE_OPEN_NORMAL_PCT = float(os.environ.get("V23_SAFE_OPEN_NORMAL_PCT", "0.020"))
SAFE_OPEN_PROTECT_PCT = float(os.environ.get("V23_SAFE_OPEN_PROTECT_PCT", "0.016"))
SAFE_OPEN_STRICT_PCT = float(os.environ.get("V23_SAFE_OPEN_STRICT_PCT", "0.014"))
SAFE_OPEN_VOL_PCT = SAFE_OPEN_NORMAL_PCT  # runtime compatibility
LOOSE_OPEN_VOL_PCT = float(os.environ.get("V23_LOOSE_OPEN_VOL_PCT", "0.030"))
REQUIRE_RED_VOLUME_SHRINK = os.environ.get("V23_REQUIRE_RED_VOLUME_SHRINK", "true").lower() == "true"

SPIKE_HIGH_MIN_PCT = float(os.environ.get("V23_SPIKE_HIGH_MIN_PCT", "5.0"))
SPIKE_RETRACE_MIN_PCT = float(os.environ.get("V23_SPIKE_RETRACE_MIN_PCT", "2.0"))
BID_THICK_RATIO = float(os.environ.get("V23_BID_THICK_RATIO", "2.5"))
BID_WALL_BREAK_TICKS = int(os.environ.get("V23_BID_WALL_BREAK_TICKS", "1"))
FIRST_BAR_CACHE_SECONDS = int(os.environ.get("V23_FIRST_BAR_CACHE_SECONDS", "900"))
FIRST_BAR_WORKERS = max(1, int(os.environ.get("V23_FIRST_BAR_WORKERS", "4")))

LOCKED_OPEN_MAX_PCT = v22.LOCKED_OPEN_MAX_PCT
LOCKED_RALLY_MAX_PCT = v22.LOCKED_RALLY_MAX_PCT
LOCKED_HARD_AVOID_PCT = v22.LOCKED_HARD_AVOID_PCT

_first_bar_cache = {}
_first_bar_lock = threading.Lock()
_wall_history = defaultdict(lambda: deque(maxlen=12))
_previous_daily_summary = legacy.format_daily_summary
_previous_format_test = legacy.format_test

# ---------------------------------------------------------------------------
# Notebook universe corrections / additions
# ---------------------------------------------------------------------------
# Keep the fixed universe bounded for the free Render instance, but never miss
# symbols that repeatedly appear in the mentor's hand-written lists.
MENTOR_SYMBOLS = {
    "2481": ("強茂", "2481.TW"),
    "2426": ("鼎元", "2426.TW"),
    "3094": ("聯傑", "3094.TW"),
    "6538": ("倉和", "6538.TWO"),
    "6179": ("亞通", "6179.TWO"),
    "6505": ("台塑化", "6505.TW"),
    "1409": ("新纖", "1409.TW"),
}
# 3714 was renamed; the legacy table still carried an obsolete name.
legacy.STOCK_NAMES["3714"] = "富采"
for _code, (_name, _symbol) in MENTOR_SYMBOLS.items():
    legacy.STOCK_NAMES[_code] = _name
    if _symbol not in legacy.SYMBOLS:
        legacy.SYMBOLS.append(_symbol)

# ---------------------------------------------------------------------------
# Screening refinement: high intraday strength followed by retrace
# ---------------------------------------------------------------------------
def _metrics_from_rows_v23(rows, idx: int):
    metrics = v22._metrics_from_rows_v22(rows, idx)
    if metrics is None or idx <= 0:
        return metrics

    day = rows[idx]
    prev_close = rows[idx - 1]["close"]
    if not prev_close:
        return metrics

    high_pct = (day["high"] - prev_close) / prev_close * 100
    close_pct = (day["close"] - prev_close) / prev_close * 100
    retrace_pct = high_pct - close_pct
    spike_retrace = (
        day["vol"] >= legacy.SCREEN_MIN_VOL
        and high_pct >= SPIKE_HIGH_MIN_PCT
        and retrace_pct >= SPIKE_RETRACE_MIN_PCT
        and close_pct >= -2.0
    )

    if spike_retrace:
        types = list(metrics.get("strategy_types", []))
        notes = list(metrics.get("daily_notes", []))
        if "沖高回落" not in types:
            types.append("沖高回落")
        notes.append(f"盤中最高+{high_pct:.1f}%後回落{retrace_pct:.1f}%")
        metrics["strategy_types"] = types
        metrics["daily_notes"] = notes
        metrics["strategy_score"] = int(metrics.get("strategy_score", 0)) + 2
        metrics["eligible"] = True

    metrics["intraday_high_pct"] = round(high_pct, 2)
    metrics["intraday_retrace_pct"] = round(retrace_pct, 2)
    return metrics


def _profile_for_symbol_v23(symbol: str, target_date):
    rows = v21._fetch_daily_rows(symbol)
    if not rows:
        return None
    eligible_indices = [i for i, row in enumerate(rows) if row["date"] <= target_date]
    if not eligible_indices:
        return None
    idx = eligible_indices[-1]
    metrics = _metrics_from_rows_v23(rows, idx)
    if not metrics or not metrics.get("eligible"):
        return None

    day = metrics["today"]
    if day["close"] <= 0 or day["close"] > legacy.SCREEN_MAX_PRICE:
        return None

    code = symbol.replace(".TW", "").replace(".TWO", "")
    market = "上市" if symbol.endswith(".TW") else "上櫃"
    trade = v21.calc_trade_v2(day["close"])
    return {
        "market": market,
        "code": code,
        "name": legacy.STOCK_NAMES.get(code, code),
        "symbol": symbol,
        "close": round(day["close"], 2),
        "pct": metrics["pct"],
        "vol": day["vol"],
        "prev_high": round(day["high"], 2),
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
        "intraday_high_pct": metrics.get("intraday_high_pct"),
        "intraday_retrace_pct": metrics.get("intraday_retrace_pct"),
        "safe_open_volume": max(1, int(day["vol"] * SAFE_OPEN_NORMAL_PCT)),
        "safe_open_protect": max(1, int(day["vol"] * SAFE_OPEN_PROTECT_PCT)),
        "safe_open_strict": max(1, int(day["vol"] * SAFE_OPEN_STRICT_PCT)),
        "loose_open_volume": max(1, int(day["vol"] * LOOSE_OPEN_VOL_PCT)),
        **trade,
    }

# The original V2.1 screen resolves these names dynamically from strategy_v2.
v21._metrics_from_rows = _metrics_from_rows_v23
v21._profile_for_symbol = _profile_for_symbol_v23

# ---------------------------------------------------------------------------
# Fugle-first 09:00 one-minute volume (red number)
# ---------------------------------------------------------------------------
def _parse_fugle_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TW_TZ)
    except Exception:
        return None


def _fugle_first_bar(code: str, target_date):
    if not legacy.FUGLE_TOKEN:
        return None
    headers = {"X-API-KEY": legacy.FUGLE_TOKEN}
    now = datetime.now(TW_TZ)

    try:
        if target_date == now.date():
            if now.hour < 9 or (now.hour == 9 and now.minute < 1):
                return None
            url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/{code}"
            params = {"timeframe": "1", "sort": "asc"}
        else:
            url = f"https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{code}"
            ds = target_date.isoformat()
            params = {
                "timeframe": "1", "from": ds, "to": ds,
                "fields": "open,high,low,close,volume", "sort": "asc",
            }

        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            logger.info("Fugle first-bar %s %s status=%s", code, target_date, r.status_code)
            return None
        rows = r.json().get("data", []) or []
        for row in rows:
            dt = _parse_fugle_dt(row.get("date"))
            if not dt or dt.date() != target_date:
                continue
            if dt.hour == 9 and dt.minute == 0:
                # Fugle documents regular-lot candle volume in lots (張).
                return int(row.get("volume") or 0)
    except Exception as exc:
        logger.debug("Fugle first-bar %s %s: %s", code, target_date, exc)
    return None


def fetch_first_bar_volume_v23(symbol: str, target_date, force: bool = False):
    key = (symbol, str(target_date))
    now_ts = time.time()
    with _first_bar_lock:
        cached = _first_bar_cache.get(key)
        if cached and not force and now_ts - cached["ts"] < FIRST_BAR_CACHE_SECONDS:
            return cached["value"]

    code = symbol.split(".")[0]
    value = _fugle_first_bar(code, target_date)
    source = "fugle"
    if value is None:
        value = v22.fetch_first_bar_volume(symbol, target_date, force=force)
        source = "yahoo" if value is not None else "none"

    if value is not None:
        now = datetime.now(TW_TZ)
        complete = target_date < now.date() or (target_date == now.date() and (now.hour > 9 or now.minute >= 1))
        if complete:
            with _first_bar_lock:
                _first_bar_cache[key] = {"ts": now_ts, "value": int(value), "source": source}
        logger.info("V2.3 first-bar %s %s=%s source=%s", code, target_date, value, source)
    return value


def _enrich_open_volume_v23(c):
    c = dict(c)
    symbol = c.get("symbol") or legacy.symbol_for_code(c["code"], c.get("market"))
    target_date = legacy.get_last_trading_day()
    vol = int(c.get("vol") or 0)
    c["safe_open_volume"] = max(1, int(vol * SAFE_OPEN_NORMAL_PCT))
    c["safe_open_protect"] = max(1, int(vol * SAFE_OPEN_PROTECT_PCT))
    c["safe_open_strict"] = max(1, int(vol * SAFE_OPEN_STRICT_PCT))
    c["loose_open_volume"] = max(1, int(vol * LOOSE_OPEN_VOL_PCT))
    c["prev_open_volume"] = fetch_first_bar_volume_v23(symbol, target_date)
    return c


def screen_v23(force: bool = False):
    # _v21_screen is the preserved original V2.1 scanner; its profile global was
    # patched above, so it now sees V2.3 candidate types without recursion.
    candidates = v22._v21_screen(force=force)
    if not candidates:
        return []

    enriched = []
    with ThreadPoolExecutor(max_workers=FIRST_BAR_WORKERS) as ex:
        futs = {ex.submit(_enrich_open_volume_v23, c): c for c in candidates}
        for fut in as_completed(futs):
            try:
                enriched.append(fut.result())
            except Exception as exc:
                logger.debug("V2.3 first-bar enrich error: %s", exc)
                enriched.append(dict(futs[fut]))

    enriched.sort(
        key=lambda x: (x.get("strategy_score", 0), x.get("rel_vol", 0), x.get("vol", 0)),
        reverse=True,
    )
    for c in enriched:
        c["signal"] = v21._signal_for_candidate(c)
    logger.info("V2.3 screen: %s candidates / %s symbols", len(enriched), len(legacy.SYMBOLS))
    return enriched

v21.screen_v2 = screen_v23
v22.screen_v22 = screen_v23
legacy.screen = screen_v23

# ---------------------------------------------------------------------------
# Price: prior high and +2.5% are independent references
# ---------------------------------------------------------------------------
def price_state(stock, day_high=None, prev_high=None):
    ph = prev_high if prev_high is not None else stock.get("prev_high")
    plus25 = stock.get("watch_line")
    high = day_high

    if stock.get("limit_up_locked"):
        broken_plus = bool(high is not None and plus25 is not None and high >= plus25)
        return {
            "prev_high": ph,
            "plus25": plus25,
            "held_prev": None,
            "held_plus": not broken_plus,
            "held_count": 0 if broken_plus else 1,
            "cancel": broken_plus,
            "ceiling": plus25,
            "text": f"鎖漲停特殊 +2.5% {plus25}",
        }

    held_prev = bool(ph is not None and (high is None or high < ph))
    held_plus = bool(plus25 is not None and (high is None or high < plus25))
    available = [x for x in (ph, plus25) if x is not None]
    ceiling = max(available) if available else None
    # Normal names lose the weak-price thesis only after BOTH references break.
    cancel = bool(ph is not None and plus25 is not None and not held_prev and not held_plus)
    held_count = int(held_prev) + int(held_plus)
    return {
        "prev_high": ph,
        "plus25": plus25,
        "held_prev": held_prev,
        "held_plus": held_plus,
        "held_count": held_count,
        "cancel": cancel,
        "ceiling": ceiling,
        "text": f"昨高 {ph if ph is not None else 'N/A'}｜+2.5% {plus25 if plus25 is not None else 'N/A'}",
    }


def resistance_for_stock_v23(stock, prev_high=None):
    return price_state(stock, None, prev_high).get("ceiling") or stock.get("watch_line")

v22.resistance_for_stock = resistance_for_stock_v23

# ---------------------------------------------------------------------------
# Opening-volume tiers
# ---------------------------------------------------------------------------
def _today_first_bar_v23(stock):
    now = datetime.now(TW_TZ)
    if now.hour == 9 and now.minute < 1:
        return None
    symbol = stock.get("symbol") or legacy.symbol_for_code(stock["code"], stock.get("market"))
    return fetch_first_bar_volume_v23(symbol, now.date())


def opening_volume_state_v23(stock, today_first_bar):
    total = max(int(stock.get("vol") or 0), 1)
    normal = max(1, int(stock.get("safe_open_volume") or total * SAFE_OPEN_NORMAL_PCT))
    protect = max(1, int(stock.get("safe_open_protect") or total * SAFE_OPEN_PROTECT_PCT))
    strict = max(1, int(stock.get("safe_open_strict") or total * SAFE_OPEN_STRICT_PCT))
    loose = max(1, int(stock.get("loose_open_volume") or total * LOOSE_OPEN_VOL_PCT))
    prev_red = stock.get("prev_open_volume")

    if today_first_bar is None:
        return {
            "available": False, "safe": False, "weaker_than_prev": False,
            "danger": False, "today": None, "safe_blue": normal,
            "protect_blue": protect, "strict_blue": strict, "prev_red": prev_red,
            "ratio_prev": None, "tier": "等待", "tier_score": 0,
            "notes": ["第一盤量尚未完成"],
        }

    if today_first_bar <= strict:
        tier, tier_score = "更保≤1.4%", 3
    elif today_first_bar <= protect:
        tier, tier_score = "保≤1.6%", 2
    elif today_first_bar <= normal:
        tier, tier_score = "安全≤2.0%", 1
    elif today_first_bar <= loose:
        tier, tier_score = "2~3%偏多", 0
    else:
        tier, tier_score = ">3%量大", 0

    weaker = bool(prev_red is not None and prev_red > 0 and today_first_bar < prev_red)
    ratio_prev = today_first_bar / prev_red if prev_red else None
    danger = False
    notes = [tier]
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
        "safe": today_first_bar <= normal,
        "weaker_than_prev": weaker,
        "danger": danger,
        "today": today_first_bar,
        "safe_blue": normal,
        "protect_blue": protect,
        "strict_blue": strict,
        "prev_red": prev_red,
        "ratio_prev": ratio_prev,
        "tier": tier,
        "tier_score": tier_score,
        "notes": notes,
    }

v22._today_first_bar = _today_first_bar_v23
v22.opening_volume_state = opening_volume_state_v23

# ---------------------------------------------------------------------------
# Dynamic order book: thick bids are not automatically support
# ---------------------------------------------------------------------------
def analyze_order_book_v23(code: str, quote, resistance_line=None):
    core = v21.analyze_order_book_v2(code, quote, resistance_line)
    bids = quote.get("bids", []) or []
    asks = quote.get("asks", []) or []
    current = quote.get("current") or quote.get("trial_price")
    bid_size = sum(x.get("size", 0) for x in bids)
    ask_size = sum(x.get("size", 0) for x in asks)
    ratio = bid_size / ask_size if ask_size > 0 else (999.0 if bid_size > 0 else 0.0)
    dominant = max(bids, key=lambda x: x.get("size", 0), default=None)
    dom_price = dominant.get("price") if dominant else None
    dom_size = dominant.get("size", 0) if dominant else 0

    hist = _wall_history[code]
    prev = hist[-1] if hist else None
    extra_score = 0
    notes = []

    if prev and current is not None:
        tick = v21.tick_size(current)
        price_delta = current - prev["price"]

        # Screenshot-confirmed pattern: visually thick bids can be swept through.
        if prev.get("bid_ask_ratio", 0) >= BID_THICK_RATIO and prev.get("dom_price") is not None:
            break_level = v21.move_ticks(prev["dom_price"], -BID_WALL_BREAK_TICKS)
            if current <= break_level:
                extra_score += 2
                notes.append(f"厚委買失守{prev['dom_price']}")

        # No abnormal ask wall, yet thick bids still cannot lift the price:
        # demand is weaker than the static book suggests.
        if ratio >= BID_THICK_RATIO and price_delta <= tick * 0.25:
            extra_score += 1
            notes.append("委買厚但價格推不動")
        elif ask_size <= max(bid_size, 1) and price_delta < -tick * 0.5:
            extra_score += 1
            notes.append("委賣不大仍下跌→需求弱")

    hist.append({
        "ts": datetime.now(TW_TZ),
        "price": current or 0,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_ask_ratio": ratio,
        "dom_price": dom_price,
        "dom_size": dom_size,
    })

    reason_parts = [core.get("reason", "")]
    if notes:
        reason_parts.extend(notes)
    core["weak_score"] = int(core.get("weak_score", 0)) + extra_score
    core["reason"] = "、".join(x for x in reason_parts if x)
    core["dominant_bid_price"] = dom_price
    core["dominant_bid_size"] = dom_size
    core["bid_ask_ratio"] = round(ratio, 2) if ratio < 999 else 999
    return core

# ---------------------------------------------------------------------------
# Preopen summary
# ---------------------------------------------------------------------------
def preopen_scan_once_v23():
    v21._reset_v2_state_if_needed()
    if not legacy._watchlist_today:
        candidates = screen_v23()
        if candidates:
            v21._populate_watchlist(candidates)

    for stock in list(legacy._watchlist_today):
        q = v21.fugle_quote_v2(stock["code"])
        if not q:
            continue
        v21._record_trial(stock, q)
        ps = price_state(stock, q.get("trial_price"), stock.get("prev_high"))
        book = analyze_order_book_v23(stock["code"], q, ps.get("ceiling"))
        try:
            legacy.log_orderbook_snapshot(stock, q, ps.get("ceiling"), book, "preopen_trial")
        except Exception:
            pass
        time.sleep(0.15)


def format_preopen_summary_v23():
    rows = []
    for stock in legacy._watchlist_today:
        s = v21._trial_summary(stock)
        if not s.get("available"):
            continue
        hard_avoid = bool(
            stock.get("limit_up_locked")
            and s.get("price_pct") is not None
            and s["price_pct"] >= LOCKED_HARD_AVOID_PCT
        )
        score = 2 if s.get("good") else 1 if s.get("volume_shrink") else 0
        if s.get("danger") or hard_avoid:
            score -= 3
        rows.append((score, stock, s, hard_avoid))
    rows.sort(key=lambda x: x[0], reverse=True)

    if not rows:
        return "🧪 <b>08:44 試撮摘要｜V2.3</b>\n\n目前觀察名單沒有可用試撮資料"

    lines = ["🧪 <b>08:44 試撮摘要｜窮大叔量價法 V2.3</b>"]
    for score, stock, s, hard_avoid in rows[:8]:
        icon = "⛔" if hard_avoid else "🟢" if s.get("good") else "🔴" if s.get("danger") else "🟡"
        total = max(int(stock.get("vol") or 0), 1)
        normal = max(1, int(stock.get("safe_open_volume") or total * SAFE_OPEN_NORMAL_PCT))
        protect = max(1, int(stock.get("safe_open_protect") or total * SAFE_OPEN_PROTECT_PCT))
        strict = max(1, int(stock.get("safe_open_strict") or total * SAFE_OPEN_STRICT_PCT))
        red = stock.get("prev_open_volume")
        red_text = f"{red:,}張" if red else "無資料"
        notes = "、".join(s.get("notes", [])) or "資料不足"
        if hard_avoid:
            notes += f"、前日鎖漲停且試撮≥+{LOCKED_HARD_AVOID_PCT:g}%→避開"
        lines.append(
            f"{icon} <b>{stock['code']} {stock['name']}</b>｜{stock.get('strategy_type','')}\n"
            f"  試撮 {s.get('price_pct',0):+.2f}%｜末筆量 {s.get('size',0):,}張｜{notes}\n"
            f"  🟢昨高 {stock.get('prev_high')}｜+2.5% {stock.get('watch_line')}\n"
            f"  🔵2%≤{normal:,}｜保1.6%≤{protect:,}｜更保1.4%≤{strict:,}張\n"
            f"  🔴前日實際第一盤 {red_text}"
        )
    lines.append("\n09:00後：昨高/+2.5%分開看；兩條都突破才取消一般弱勢。第一盤越縮越安全。")
    return "\n".join(lines)

v21.preopen_scan_once = preopen_scan_once_v23
v21.format_preopen_summary = format_preopen_summary_v23
v22.preopen_scan_once_v22 = preopen_scan_once_v23
v22.format_preopen_summary_v22 = format_preopen_summary_v23

# ---------------------------------------------------------------------------
# Live intraday V2.3
# ---------------------------------------------------------------------------
def _confidence_v23(stock, setup_name, ideal_pullback, volume_state, trial, book, ps, special_limit_setup=False):
    score = 0
    reasons = []
    if stock.get("strategy_score", 0) >= 4:
        score += 1
        reasons.append(stock.get("strategy_type", "量價候選"))
    if stock.get("rel_vol", 0) >= v21.REL_VOL_STRONG:
        score += 1
        reasons.append(f"昨量比{stock['rel_vol']:.1f}x")
    if ideal_pullback:
        score += 2
        reasons.append("小拉約1%")
    else:
        score += 1
    if "破開盤" in setup_name or "轉弱" in setup_name:
        score += 1
        reasons.append("跌破開盤價")

    tier_score = int(volume_state.get("tier_score", 0))
    if tier_score >= 3:
        score += 2
        reasons.append("第一盤≤更保1.4%")
    elif tier_score >= 2:
        score += 2
        reasons.append("第一盤≤保1.6%")
    elif tier_score >= 1:
        score += 1
        reasons.append("第一盤≤2%")
    if volume_state.get("weaker_than_prev"):
        score += 1
        reasons.append("第一盤低於前日紅字")

    if trial.get("good"):
        score += 1
        reasons.append("試撮量價吻合")
    elif trial.get("volume_shrink"):
        score += 1
        reasons.append("試撮量縮")

    held = int(ps.get("held_count", 0))
    if held >= 2:
        score += 2
        reasons.append("昨高/+2.5%都壓住")
    elif held == 1:
        score += 1
        reasons.append("昨高/+2.5%仍有一條壓住")

    if book.get("weak_score", 0) > 0:
        score += min(int(book["weak_score"]), 2)
        reasons.append("五檔轉弱/厚買失守")
    if special_limit_setup:
        score += 2
        reasons.append("隔日沖停損結構")
    if trial.get("danger") or book.get("danger") or volume_state.get("danger"):
        score -= 3

    grade = "A" if score >= 8 else "B" if score >= v21.ALERT_SCORE_MIN else "C"
    return score, grade, reasons


def intraday_monitor_v23():
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5 or not (9 <= now.hour < legacy.INTRADAY_ALERT_END_HOUR):
        return

    v21._reset_v2_state_if_needed()
    legacy.reset_daily_state()
    if not legacy._watchlist_today:
        logger.info("V2.3 intraday: watchlist empty; rebuilding")
        candidates = screen_v23()
        if not candidates:
            return
        v21._populate_watchlist(candidates)

    for stock in list(legacy._watchlist_today):
        code = stock["code"]
        if code in legacy._alerted_today:
            continue
        quote = v21.fugle_quote_v2(code)
        if not quote:
            continue

        current = quote["current"]
        pct_now = quote["pct"]
        open_price = quote.get("open")
        open_pct = quote.get("open_pct")
        day_high = quote.get("day_high") or current
        prev_high = stock.get("prev_high") or legacy.get_prev_day_high(code, stock.get("market"))
        ps = price_state(stock, day_high, prev_high)

        if ps["cancel"]:
            logger.info("%s V2.3 price gate: both prior-high and +2.5%% broken", code)
            continue

        today_first = _today_first_bar_v23(stock)
        vol_state = opening_volume_state_v23(stock, today_first)
        if not vol_state["available"]:
            continue
        if vol_state["danger"]:
            logger.info("%s V2.3 opening volume not weak: %s", code, vol_state["notes"])
            continue

        ceiling = ps.get("ceiling") or stock.get("watch_line")
        pullback = v21.VALID_PULLBACK_MIN <= pct_now < v21.VALID_PULLBACK_MAX and current < ceiling
        ideal_pullback = v21.IDEAL_PULLBACK_MIN <= pct_now <= v21.IDEAL_PULLBACK_MAX
        break_open = bool(
            open_price and open_pct is not None
            and current < open_price and current < ceiling
            and (
                open_pct < legacy.WEAK_OPEN_MAX_PCT
                or (stock.get("two_strong_opens") and open_pct < legacy.HOT_MONEY_OPEN_MAX_PCT)
            )
        )

        setup_name = None
        special_limit_setup = False
        if stock.get("limit_up_locked"):
            if not open_price or open_pct is None:
                continue
            high_pct = (day_high - stock["close"]) / stock["close"] * 100 if stock["close"] else 0
            rally_from_open = (day_high - open_price) / open_price * 100 if open_price else 999
            if high_pct >= LOCKED_HARD_AVOID_PCT or pct_now >= LOCKED_HARD_AVOID_PCT:
                logger.info("%s V2.3 locked-limit >= %.1f%% hard avoid", code, LOCKED_HARD_AVOID_PCT)
                continue
            locked_setup = (
                open_pct < LOCKED_OPEN_MAX_PCT
                and rally_from_open < LOCKED_RALLY_MAX_PCT
                and current < open_price
                and current < ceiling
            )
            if not locked_setup:
                continue
            setup_name = "鎖漲停隔日沖轉弱"
            special_limit_setup = True
            ideal_pullback = rally_from_open <= LOCKED_RALLY_MAX_PCT
        else:
            if not (pullback or break_open):
                continue
            setup_name = "破開盤價弱勢" if break_open else "小拉升不過壓力"

        trial = v21._trial_summary(stock)
        book = analyze_order_book_v23(code, quote, ceiling)
        try:
            legacy.log_orderbook_snapshot(stock, quote, ceiling, book, "watchlist_scan", setup_name)
        except Exception:
            pass
        if trial.get("danger") or book.get("danger"):
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
            else:
                other_types = [t for t in stock.get("strategy_types", []) if t != "漲停失敗"]
                if not other_types:
                    continue

        score, grade, reasons = _confidence_v23(
            stock, setup_name, ideal_pullback, vol_state, trial, book, ps,
            special_limit_setup=special_limit_setup,
        )
        if score < v21.ALERT_SCORE_MIN:
            continue

        stop_actual = v21.move_ticks(day_high, +1)
        entry_cap = min(open_price, ceiling) if open_price else ceiling
        entry_low = v21.nearest_tick(current)
        entry_high = min(v21.move_ticks(current, +2), v21.move_ticks(entry_cap, -1))
        if entry_high < entry_low:
            entry_high = entry_low
        entry_mid = v21.nearest_tick((entry_low + entry_high) / 2)
        risk = stop_actual - entry_mid
        if risk <= 0:
            continue

        target_2r = v21.floor_to_tick(entry_mid - risk * 2)
        scalp2 = v21.move_ticks(entry_mid, -2)
        scalp3 = v21.move_ticks(entry_mid, -3)
        scalp5 = v21.move_ticks(entry_mid, -5)
        stop_pct = round(risk / entry_mid * 100, 2)

        legacy._alerted_today.add(code)
        legacy._today_trades.append({
            "code": code, "name": stock["name"], "market": stock.get("market"),
            "entry": entry_mid, "stop": stop_actual, "setup": setup_name,
            "target": target_2r, "target_2r": target_2r, "target_scalp": scalp3,
            "scalp_2": scalp2, "scalp_3": scalp3, "scalp_5": scalp5,
            "watch_line": ceiling, "time": now.strftime("%H:%M"),
            "grade": grade, "score": score, "strategy_type": stock.get("strategy_type"),
            "open_volume_today": vol_state.get("today"),
            "open_volume_safe": vol_state.get("safe_blue"),
            "open_volume_prev": vol_state.get("prev_red"),
            "open_volume_tier": vol_state.get("tier"),
        })

        trial_text = "、".join(trial.get("notes", [])) if trial.get("available") else "未取得試撮歷史"
        daily_text = "、".join(stock.get("daily_notes", [])) or "量價候選"
        reason_text = "、".join(reasons)
        open_text = f"{open_price}（{open_pct:+.2f}%）" if open_price and open_pct is not None else "無資料"
        vol_text = "、".join(vol_state["notes"])
        prev_status = "壓住" if ps.get("held_prev") else "已過"
        plus_status = "壓住" if ps.get("held_plus") else "已過"
        red_text = f"{vol_state['prev_red']:,}張" if vol_state.get("prev_red") else "無資料"

        alert = (
            f"🚨 <b>{grade}級短空候選｜V2.3｜{now.strftime('%H:%M')}</b>\n\n"
            f"<b>{code} {stock['name']}</b> [{stock['market']}]｜分數 <b>{score}</b>\n"
            f"  🧩 類型：{stock.get('strategy_type','量價候選')}\n"
            f"  ✅ 原因：{reason_text}\n"
            f"  📊 昨日：收 {stock['close']}｜{stock['pct']:+.2f}%｜{stock['vol']:,}張｜量比 {stock.get('rel_vol',0):.1f}x\n"
            f"  🧭 日線：{daily_text}\n"
            f"  🧪 試撮：{trial_text}\n"
            f"  📍 現價：<b>{current}</b>（{pct_now:+.2f}%）｜開盤 {open_text}\n"
            f"  🟢 昨高 {ps.get('prev_high')}【{prev_status}】｜+2.5% {ps.get('plus25')}【{plus_status}】\n"
            f"  🔵 2%≤{vol_state['safe_blue']:,}｜保1.6%≤{vol_state['protect_blue']:,}｜更保1.4%≤{vol_state['strict_blue']:,}\n"
            f"  🔴 前日第一盤：{red_text}\n"
            f"  📦 今日第一盤：<b>{vol_state['today']:,}張</b>｜{vol_text}\n"
            f"  📚 五檔：買 {book['bid_size']:,} / 賣 {book['ask_size']:,}｜{book['reason']}\n\n"
            f"  ━━━━━━ 紙上進場參考 ━━━━━━\n"
            f"  🎯 掛空：<b>{entry_low}~{entry_high}</b>（試算 {entry_mid}）\n"
            f"  🛑 停損：早盤高點上一檔 <b>{stop_actual}</b>（約 +{stop_pct:.2f}%）\n"
            f"  ⚡ Scalp：2檔 {scalp2}｜<b>3檔 {scalp3}</b>｜5檔 {scalp5}\n"
            f"  💰 2R：<b>{target_2r}</b>\n\n"
            "⚠️ 一般標的昨高與+2.5%兩條都突破才取消弱勢；厚委買若推不動/失守反而是弱勢確認"
        )
        legacy.tg_only(legacy.CHAT_ID, alert)
        time.sleep(0.2)

v21.intraday_monitor_v2 = intraday_monitor_v23
v22.intraday_monitor_v22 = intraday_monitor_v23
legacy.intraday_monitor = intraday_monitor_v23

# ---------------------------------------------------------------------------
# Reports / status
# ---------------------------------------------------------------------------
def format_report_v23(candidates):
    last_day = legacy.get_last_trading_day()
    next_day = legacy.get_next_trading_day(last_day)
    if not candidates:
        return f"📊 <b>{last_day.strftime('%m/%d')} V2.3 收盤篩選</b>\n\n今日沒有符合量價/乖離/沖高回落/漲停結構的標的"

    v21._populate_watchlist(candidates)
    lines = [f"📊 <b>{last_day.strftime('%m/%d')} 收盤｜{next_day.strftime('%m/%d')} V2.3觀察名單（{len(candidates)}支）</b>"]
    for c in candidates:
        vol = max(int(c.get("vol") or 0), 1)
        normal = max(1, int(c.get("safe_open_volume") or vol * SAFE_OPEN_NORMAL_PCT))
        protect = max(1, int(c.get("safe_open_protect") or vol * SAFE_OPEN_PROTECT_PCT))
        strict = max(1, int(c.get("safe_open_strict") or vol * SAFE_OPEN_STRICT_PCT))
        red = c.get("prev_open_volume")
        red_text = f"{red:,}張" if red else "無資料"
        notes = "、".join(c.get("daily_notes", [])) or "一般量價候選"
        lines.append(
            f"\n{c['signal']} <b>{c['code']} {c['name']}</b>｜{c.get('strategy_type','')}\n"
            f"  收 {c['close']}｜{c['pct']:+.2f}%｜{c['vol']:,}張｜量比 {c.get('rel_vol',0):.1f}x\n"
            f"  {notes}\n"
            f"  🟢昨高 <b>{c.get('prev_high')}</b>｜+2.5% <b>{c.get('watch_line')}</b>（兩條分開看）\n"
            f"  🔵2%≤{normal:,}｜保1.6%≤{protect:,}｜更保1.4%≤{strict:,}張\n"
            f"  🔴前日實際第一盤：<b>{red_text}</b>"
        )
    lines.append(
        "\n規則：價格優先 → 試撮量縮 → 第一盤與🔴前日量比較 → 看2/1.6/1.4%安全層級 → 小拉/破開盤 + 五檔確認\n"
        "一般標的：昨高與+2.5%兩條都突破才取消弱勢；前日鎖漲停仍套用更嚴格隔日沖規則。\n"
        "⚠️ 只提醒，不自動下單"
    )
    return "\n".join(lines)


def format_line_morning_v23(candidates):
    next_day = legacy.get_next_trading_day(legacy.get_last_trading_day())
    if not candidates:
        return f"📊 {next_day.strftime('%m/%d')} 今日無V2.3短空觀察標的"
    lines = [f"📊 {next_day.strftime('%m/%d')} 短空觀察 V2.3"]
    for c in candidates[:8]:
        red = c.get("prev_open_volume")
        lines.append(
            f"\n{'🔴' if c.get('strategy_score',0)>=6 else '🟠'} {c['code']} {c['name']}｜{c.get('strategy_type','')}\n"
            f"昨高 {c.get('prev_high')}｜+2.5% {c.get('watch_line')}｜昨量 {c.get('vol',0):,}\n"
            f"紅字前日第一盤 {red if red is not None else 'N/A'}｜等試撮/第一盤量縮 + 小拉轉弱"
        )
    lines.append("\n08:44 Telegram再依試撮強弱排序；一般標的兩條價格壓力都突破才取消弱勢。")
    return "\n".join(lines)


def format_daily_summary_v23():
    return _previous_daily_summary().replace("V2.2", "V2.3")


def format_test_v23():
    base_text = _previous_format_test().replace("V2.2", "V2.3")
    return (
        base_text
        + "\n✅ V2.3價格：昨高/+2.5%雙壓力獨立判斷"
        + "\n✅ V2.3開盤量：2.0% / 保1.6% / 更保1.4%"
        + "\n✅ 紅字第一盤：Fugle主、Yahoo備援"
        + "\n✅ 五檔：厚委買推不動/失守列弱勢確認"
        + f"\n✅ 母池：{len(legacy.SYMBOLS)}支（含筆記補充標的）"
    )

legacy.format_report = format_report_v23
legacy.format_line_morning = format_line_morning_v23
legacy.format_daily_summary = format_daily_summary_v23
legacy.format_test = format_test_v23


def _index_v23():
    return "📈 短空機器人 V2.3 運行中（雙壓力 + 三級第一盤量 + Fugle紅字 + 厚買失守｜僅提醒不下單）"

legacy.app.view_functions["index"] = _index_v23


def _v23_status():
    return {
        "status": "ok",
        "version": "2.3",
        "mode": "alerts_only",
        "symbols": len(legacy.SYMBOLS),
        "watchlist": len(legacy._watchlist_today),
        "alerted_today": len(legacy._alerted_today),
        "trial_symbols": len(_trial_history),
        "book_symbols": len(_book_history),
        "first_bar_cache": len(_first_bar_cache),
        "safe_tiers": [SAFE_OPEN_NORMAL_PCT, SAFE_OPEN_PROTECT_PCT, SAFE_OPEN_STRICT_PCT],
        "price_gate": "prior_high_and_plus_2_5_independent",
        "time": datetime.now(TW_TZ).isoformat(),
    }

if "v2_status" in legacy.app.view_functions:
    legacy.app.view_functions["v2_status"] = _v23_status

logger.info("Short-bot strategy V2.3 overlay loaded")
