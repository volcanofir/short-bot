"""Short-bot strategy overlay V2.1.

The original app.py is intentionally left untouched.  This module imports the
legacy application, replaces only strategy/data functions, and exposes the same
Flask app object for Gunicorn.  Rollback is therefore only a start-command
change away.

V2.1 goals:
- relative-volume screening instead of absolute volume alone
- overextended / three-day-stall candidates
- 08:30~08:45 Fugle trial-match observation
- dynamic order-book interpretation (not a single static ratio)
- Taiwan stock tick-size aware prices
- scalp (3 ticks) and 2R side-by-side paper results/backtests
- correct Fugle cumulative volume field and short-side transaction tax basis
- backtest the full configured symbol universe by default

This remains an alert/paper-analysis bot.  It does not place orders.
"""

from __future__ import annotations

import os
import time
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from collections import defaultdict, deque

import requests

import app as legacy

app = legacy.app
logger = legacy.logger
TW_TZ = legacy.TW_TZ
UA = legacy.UA

# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------
REL_VOL_MIN = float(os.environ.get("V2_REL_VOL_MIN", "1.5"))
REL_VOL_STRONG = float(os.environ.get("V2_REL_VOL_STRONG", "2.0"))
THREE_DAY_GAIN_MIN = float(os.environ.get("V2_THREE_DAY_GAIN_MIN", "8.0"))
TEN_DAY_GAIN_MIN = float(os.environ.get("V2_TEN_DAY_GAIN_MIN", "10.0"))
SMA5_EXTENSION_MIN = float(os.environ.get("V2_SMA5_EXTENSION_MIN", "4.0"))
STALL_EXTENSION_MIN = float(os.environ.get("V2_STALL_EXTENSION_MIN", "2.0"))

IDEAL_PULLBACK_MIN = float(os.environ.get("V2_IDEAL_PULLBACK_MIN", "0.6"))
IDEAL_PULLBACK_MAX = float(os.environ.get("V2_IDEAL_PULLBACK_MAX", "1.4"))
VALID_PULLBACK_MIN = float(os.environ.get("V2_VALID_PULLBACK_MIN", "0.5"))
VALID_PULLBACK_MAX = float(os.environ.get("V2_VALID_PULLBACK_MAX", "2.5"))
ALERT_SCORE_MIN = int(os.environ.get("V2_ALERT_SCORE_MIN", "5"))

TRIAL_VOL_GOOD_PCT = float(os.environ.get("V2_TRIAL_VOL_GOOD_PCT", "0.02"))
TRIAL_VOL_LOOSE_PCT = float(os.environ.get("V2_TRIAL_VOL_LOOSE_PCT", "0.03"))
PREOPEN_START_HOUR = 8
PREOPEN_START_MINUTE = 30
PREOPEN_END_MINUTE = 46  # monitor through 08:45
PREOPEN_SUMMARY_MINUTE = 44

SCREEN_LIMIT = int(os.environ.get("V2_SCREEN_LIMIT", "12"))
SCREEN_CACHE_SECONDS = int(os.environ.get("V2_SCREEN_CACHE_SECONDS", "600"))
SCREEN_WORKERS = max(1, int(os.environ.get("V2_SCREEN_WORKERS", "6")))
BACKTEST_SYMBOL_LIMIT = int(os.environ.get("BACKTEST_SYMBOL_LIMIT", "0"))  # 0 = all
BACKTEST_WORKERS = max(1, int(os.environ.get("BACKTEST_WORKERS", "4")))

# Keep legacy display/help values aligned with V2.
legacy.RESISTANCE_PCT = 2.5
legacy.OPEN_PULLBACK_MIN_PCT = VALID_PULLBACK_MIN
legacy.OPEN_VOLUME_PCT = TRIAL_VOL_GOOD_PCT
legacy.OPEN_VOLUME_LOOSE_MULTIPLIER = TRIAL_VOL_LOOSE_PCT / TRIAL_VOL_GOOD_PCT
legacy.INTRADAY_ALERT_END_HOUR = 10

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
_book_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=8))
_trial_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
_screen_cache = {"target": None, "ts": 0.0, "items": []}
_v2_reset_date = None
_preopen_summary_date = None
_state_lock = threading.Lock()

# Save originals that are useful after monkey-patching.
_original_format_test = legacy.format_test


# ---------------------------------------------------------------------------
# Taiwan stock tick helpers
# ---------------------------------------------------------------------------
def tick_size(price: float) -> float:
    """TWSE/TPEX common-stock price increment."""
    p = float(price)
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.1
    if p < 500:
        return 0.5
    if p < 1000:
        return 1.0
    return 5.0


def _decimals_for_tick(tick: float) -> int:
    if tick < 0.1:
        return 2
    if tick < 1:
        return 1
    return 0


def floor_to_tick(price: float) -> float:
    tick = tick_size(max(price - 1e-9, 0.01))
    units = math.floor((price + 1e-10) / tick)
    return round(units * tick, _decimals_for_tick(tick))


def ceil_to_tick(price: float) -> float:
    tick = tick_size(max(price + 1e-9, 0.01))
    units = math.ceil((price - 1e-10) / tick)
    return round(units * tick, _decimals_for_tick(tick))


def nearest_tick(price: float) -> float:
    lo = floor_to_tick(price)
    hi = ceil_to_tick(price)
    return lo if abs(price - lo) <= abs(hi - price) else hi


def move_ticks(price: float, steps: int) -> float:
    p = float(price)
    if steps == 0:
        return nearest_tick(p)
    direction = 1 if steps > 0 else -1
    for _ in range(abs(steps)):
        probe = p + (1e-9 if direction > 0 else -1e-9)
        tick = tick_size(max(probe, 0.01))
        p += direction * tick
        p = round(p, _decimals_for_tick(tick))
    return p


# ---------------------------------------------------------------------------
# Fees / trade references
# ---------------------------------------------------------------------------
def calc_pnl_v2(entry: float, exit_price: float, lots: int = 1):
    """Paper P/L for a same-day short, using standard (undiscounted) fee rate.

    A short sells at entry and buys back at exit, so the day-trade transaction
    tax is charged on the sell (entry) not on the buyback.
    """
    gross = (entry - exit_price) * 1000 * lots
    fee = (entry + exit_price) * 1000 * lots * 0.001425
    tax = entry * 1000 * lots * 0.0015
    net = gross - fee - tax
    return round(gross), round(net)


def calc_trade_v2(close: float):
    watch_line = ceil_to_tick(close * (1 + legacy.RESISTANCE_PCT / 100))
    stop_ref = move_ticks(ceil_to_tick(close * 1.02), 0)
    risk = max(stop_ref - close, tick_size(close))
    target_2r = floor_to_tick(close - risk * 2)
    return {
        "watch_line": watch_line,
        "stop_ref": stop_ref,
        "target_ref": target_2r,
        "target_2r": target_2r,
        "scalp_2": move_ticks(close, -2),
        "scalp_3": move_ticks(close, -3),
        "scalp_5": move_ticks(close, -5),
        "stop_pct": round(risk / close * 100, 2),
        "target_pct": round(risk * 2 / close * 100, 2),
    }


# ---------------------------------------------------------------------------
# Fugle quote + trial-match data
# ---------------------------------------------------------------------------
def fugle_quote_v2(code: str):
    if not legacy.FUGLE_TOKEN:
        return None
    try:
        url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{code}"
        r = requests.get(url, headers={"X-API-KEY": legacy.FUGLE_TOKEN}, timeout=8)
        if r.status_code != 200:
            logger.info("Fugle quote %s status=%s", code, r.status_code)
            return None
        data = r.json()
        trial = data.get("lastTrial") or {}
        total = data.get("total") or {}

        prev_close = data.get("previousClose") or data.get("referencePrice")
        current = data.get("lastPrice") or data.get("closePrice") or trial.get("price")
        open_price = data.get("openPrice")
        day_high = data.get("highPrice")
        day_low = data.get("lowPrice")
        volume = total.get("tradeVolume")
        if volume is None:
            volume = data.get("totalVolume", 0)

        if not current or not prev_close or float(prev_close) <= 0:
            return None

        current = float(current)
        prev_close = float(prev_close)
        pct = (current - prev_close) / prev_close * 100
        open_pct = ((float(open_price) - prev_close) / prev_close * 100
                    if open_price else None)
        trial_price = trial.get("price")
        trial_pct = ((float(trial_price) - prev_close) / prev_close * 100
                     if trial_price else None)

        return {
            "current": round(current, 2),
            "prev_close": round(prev_close, 2),
            "open": round(float(open_price), 2) if open_price else None,
            "open_pct": round(open_pct, 2) if open_pct is not None else None,
            "pct": round(pct, 2),
            "vol": int(volume or 0),
            "day_high": round(float(day_high), 2) if day_high else None,
            "day_low": round(float(day_low), 2) if day_low else None,
            "bids": legacy.normalize_order_levels(data.get("bids", [])),
            "asks": legacy.normalize_order_levels(data.get("asks", [])),
            "is_trial": bool(data.get("isTrial", False)),
            "trial_price": round(float(trial_price), 2) if trial_price else None,
            "trial_pct": round(trial_pct, 2) if trial_pct is not None else None,
            "trial_size": int(trial.get("size") or 0),
            "trial_bid": trial.get("bid"),
            "trial_ask": trial.get("ask"),
            "trade_volume_at_bid": int(total.get("tradeVolumeAtBid") or 0),
            "trade_volume_at_ask": int(total.get("tradeVolumeAtAsk") or 0),
            "raw": data,
        }
    except Exception as e:
        logger.error("Fugle V2 quote error %s: %s", code, e)
        return None


# ---------------------------------------------------------------------------
# Daily profile / screening
# ---------------------------------------------------------------------------
def _fetch_daily_rows(symbol: str, range_str: str = "3mo"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": range_str, "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return []
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        rows = []
        for ts, o, h, l, c, v in zip(
            chart.get("timestamp", []),
            q.get("open", []), q.get("high", []), q.get("low", []),
            q.get("close", []), q.get("volume", []),
        ):
            if None in (o, h, l, c, v):
                continue
            rows.append({
                "date": datetime.fromtimestamp(ts, TW_TZ).date(),
                "open": float(o), "high": float(h), "low": float(l),
                "close": float(c), "vol": int(v) // 1000,
            })
        return rows
    except Exception as e:
        logger.debug("daily rows %s: %s", symbol, e)
        return []


def _metrics_from_rows(rows, idx: int):
    if idx < 10:
        return None
    today = rows[idx]
    prev = rows[idx - 1]
    if prev["close"] <= 0:
        return None

    pct = (today["close"] - prev["close"]) / prev["close"] * 100
    prior5 = rows[idx-5:idx]
    prior10 = rows[idx-10:idx]
    avg5_vol = sum(x["vol"] for x in prior5) / len(prior5) if prior5 else 0
    avg10_vol = sum(x["vol"] for x in prior10) / len(prior10) if prior10 else 0
    rel_vol = today["vol"] / avg5_vol if avg5_vol > 0 else 0

    last5 = rows[idx-4:idx+1]
    sma5 = sum(x["close"] for x in last5) / len(last5)
    extension = (today["close"] - sma5) / sma5 * 100 if sma5 else 0

    three_base = rows[idx-3]["close"] if idx >= 3 else prev["close"]
    ten_base = rows[idx-10]["close"] if idx >= 10 else rows[0]["close"]
    three_day_gain = (today["close"] - three_base) / three_base * 100 if three_base else 0
    ten_day_gain = (today["close"] - ten_base) / ten_base * 100 if ten_base else 0

    recent3 = rows[idx-2:idx+1]
    prior_window = rows[max(0, idx-10):idx-2]
    recent3_high = max(x["high"] for x in recent3)
    prior_high = max((x["high"] for x in prior_window), default=recent3_high)
    three_day_no_high = recent3_high <= prior_high

    strong_open_days = 0
    for j in (idx - 1, idx):
        if j <= 0:
            continue
        pc = rows[j - 1]["close"]
        if pc > 0 and (rows[j]["open"] - pc) / pc * 100 >= 9:
            strong_open_days += 1

    explosive = (
        legacy.SCREEN_MIN_PCT <= pct <= legacy.SCREEN_MAX_PCT
        and today["vol"] >= legacy.SCREEN_MIN_VOL
        and rel_vol >= REL_VOL_MIN
    )
    extended = (
        today["vol"] >= legacy.SCREEN_MIN_VOL
        and three_day_gain >= THREE_DAY_GAIN_MIN
        and extension >= SMA5_EXTENSION_MIN
        and pct >= -2.0
    )
    stall = (
        today["vol"] >= legacy.SCREEN_MIN_VOL
        and ten_day_gain >= TEN_DAY_GAIN_MIN
        and three_day_no_high
        and extension >= STALL_EXTENSION_MIN
        and -2.0 <= pct <= 3.5
    )

    types = []
    if explosive:
        types.append("爆量強勢")
    if extended:
        types.append("連漲乖離")
    if stall:
        types.append("高檔三日不過高")

    notes = []
    if rel_vol >= REL_VOL_STRONG:
        notes.append(f"5日量比{rel_vol:.1f}x")
    elif rel_vol >= REL_VOL_MIN:
        notes.append(f"量比{rel_vol:.1f}x")
    if three_day_gain >= THREE_DAY_GAIN_MIN:
        notes.append(f"3日+{three_day_gain:.1f}%")
    if extension >= SMA5_EXTENSION_MIN:
        notes.append(f"乖離MA5 +{extension:.1f}%")
    if three_day_no_high:
        notes.append("三日不過高")
    if today["close"] < sma5:
        notes.append("收盤跌破5日線")
    if strong_open_days >= 2:
        notes.append("連2日強勢開盤")

    strategy_score = 0
    if explosive:
        strategy_score += 3
    if rel_vol >= REL_VOL_STRONG:
        strategy_score += 2
    elif rel_vol >= REL_VOL_MIN:
        strategy_score += 1
    if extended:
        strategy_score += 2
    if stall:
        strategy_score += 2
    if extension >= 6:
        strategy_score += 1

    return {
        "today": today,
        "pct": round(pct, 2),
        "avg5_vol": round(avg5_vol),
        "avg10_vol": round(avg10_vol),
        "rel_vol": round(rel_vol, 2),
        "sma5": round(sma5, 2),
        "sma5_extension": round(extension, 2),
        "three_day_gain": round(three_day_gain, 2),
        "ten_day_gain": round(ten_day_gain, 2),
        "three_day_no_high": three_day_no_high,
        "two_strong_opens": strong_open_days >= 2,
        "strategy_types": types,
        "daily_notes": notes,
        "strategy_score": strategy_score,
        "eligible": bool(types),
    }


def _profile_for_symbol(symbol: str, target_date):
    rows = _fetch_daily_rows(symbol)
    if not rows:
        return None
    eligible_indices = [i for i, row in enumerate(rows) if row["date"] <= target_date]
    if not eligible_indices:
        return None
    idx = eligible_indices[-1]
    metrics = _metrics_from_rows(rows, idx)
    if not metrics or not metrics["eligible"]:
        return None
    today = metrics["today"]
    if today["close"] <= 0 or today["close"] > legacy.SCREEN_MAX_PRICE:
        return None

    code = symbol.replace(".TW", "").replace(".TWO", "")
    market = "上市" if symbol.endswith(".TW") else "上櫃"
    name = legacy.STOCK_NAMES.get(code, code)
    trade = calc_trade_v2(today["close"])

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
        **trade,
    }


def _signal_for_candidate(c):
    s = c.get("strategy_score", 0)
    if s >= 6:
        return "🔴 A級觀察"
    if s >= 4:
        return "🟠 B級觀察"
    return "🟡 C級觀察"


def screen_v2(force: bool = False):
    target_date = legacy.get_last_trading_day()
    now_ts = time.time()
    with _state_lock:
        if (not force and _screen_cache["target"] == target_date
                and now_ts - _screen_cache["ts"] < SCREEN_CACHE_SECONDS):
            return [dict(x) for x in _screen_cache["items"]]

    found = []
    with ThreadPoolExecutor(max_workers=SCREEN_WORKERS) as ex:
        futures = {ex.submit(_profile_for_symbol, sym, target_date): sym for sym in legacy.SYMBOLS}
        for fut in as_completed(futures):
            try:
                item = fut.result()
                if item:
                    found.append(item)
            except Exception as e:
                logger.debug("screen worker %s: %s", futures[fut], e)

    found.sort(key=lambda x: (
        x.get("strategy_score", 0), x.get("rel_vol", 0), x.get("vol", 0)
    ), reverse=True)
    selected = found[:SCREEN_LIMIT]

    for c in selected:
        try:
            c.update(legacy.fetch_broker_context(c["code"]))
        except Exception:
            pass
        c["signal"] = _signal_for_candidate(c)

    with _state_lock:
        _screen_cache.update({"target": target_date, "ts": now_ts,
                              "items": [dict(x) for x in selected]})
    logger.info("V2 screen: %s candidates from %s symbols", len(selected), len(legacy.SYMBOLS))
    return selected


# ---------------------------------------------------------------------------
# Trial-match + dynamic order book
# ---------------------------------------------------------------------------
def _reset_v2_state_if_needed():
    global _v2_reset_date, _preopen_summary_date
    today = datetime.now(TW_TZ).date()
    if _v2_reset_date != today:
        _book_history.clear()
        _trial_history.clear()
        _preopen_summary_date = None
        _v2_reset_date = today


def _record_trial(stock, quote):
    code = stock["code"]
    size = int(quote.get("trial_size") or 0)
    price = quote.get("trial_price")
    if price is None:
        return None
    snap = {
        "ts": datetime.now(TW_TZ),
        "price": price,
        "pct": quote.get("trial_pct"),
        "size": size,
        "bid": quote.get("trial_bid"),
        "ask": quote.get("trial_ask"),
        "bid_size": sum(x.get("size", 0) for x in quote.get("bids", [])),
        "ask_size": sum(x.get("size", 0) for x in quote.get("asks", [])),
    }
    hist = _trial_history[code]
    if not hist or (hist[-1]["price"], hist[-1]["size"], hist[-1]["bid"], hist[-1]["ask"]) != (snap["price"], snap["size"], snap["bid"], snap["ask"]):
        hist.append(snap)
    return snap


def _trial_summary(stock):
    hist = _trial_history.get(stock["code"])
    if not hist:
        return {"available": False, "good": False, "danger": False, "notes": []}
    last = hist[-1]
    prev = hist[-2] if len(hist) >= 2 else None
    yesterday_vol = max(int(stock.get("vol") or 0), 1)
    ratio = last["size"] / yesterday_vol
    good = last["size"] > 0 and ratio <= TRIAL_VOL_GOOD_PCT
    loose = last["size"] > 0 and ratio <= TRIAL_VOL_LOOSE_PCT
    ideal_price = last.get("pct") is not None and VALID_PULLBACK_MIN <= last["pct"] <= 1.8
    danger = False
    notes = []

    if good:
        notes.append(f"試撮量縮{ratio*100:.2f}%昨量")
    elif loose:
        notes.append(f"試撮量尚可{ratio*100:.2f}%昨量")
    elif last["size"]:
        notes.append(f"試撮量偏大{ratio*100:.2f}%昨量")
    if ideal_price:
        notes.append(f"試撮小拉{last['pct']:+.2f}%")

    if prev and prev["size"] > 0:
        size_growth = (last["size"] - prev["size"]) / prev["size"]
        price_up = last["price"] > prev["price"]
        if size_growth >= 0.5 and price_up:
            danger = True
            notes.append("試撮量增價漲→不硬空")
        elif size_growth <= -0.3 and last["price"] <= prev["price"]:
            notes.append("試撮量退且價格不強")

    return {
        "available": True,
        "good": bool(good and ideal_price and not danger),
        "volume_shrink": good,
        "danger": danger,
        "ratio": ratio,
        "price_pct": last.get("pct"),
        "size": last["size"],
        "notes": notes,
    }


def analyze_order_book_v2(code: str, quote, resistance_line: float):
    bids = quote.get("bids", []) or []
    asks = quote.get("asks", []) or []
    bid_size = sum(x.get("size", 0) for x in bids)
    ask_size = sum(x.get("size", 0) for x in asks)
    ratio = ask_size / bid_size if bid_size > 0 else None
    current = quote.get("current")
    hist = _book_history[code]
    prev = hist[-1] if hist else None

    notes = []
    weak_score = 0
    danger = False

    if prev and current is not None:
        tick = tick_size(current)
        price_delta = current - prev["price"]
        bid_drop = ((bid_size - prev["bid_size"]) / prev["bid_size"]
                    if prev["bid_size"] > 0 else 0)
        ask_growth = ((ask_size - prev["ask_size"]) / prev["ask_size"]
                      if prev["ask_size"] > 0 else 0)

        if bid_drop <= -0.35 and price_delta <= tick * 0.25:
            weak_score += 2
            notes.append("委買快速撤退且價格推不動")

        if bid_size >= max(ask_size * 1.5, 1) and abs(price_delta) <= tick * 0.25:
            weak_score += 1
            notes.append("大買單掛著但價格不動")

        if ask_size >= max(bid_size * 1.8, 1) and price_delta >= tick * 0.75:
            danger = True
            notes.append("大賣壓仍被吃掉、價格續漲")
        elif ask_growth >= 0.5 and price_delta > tick * 0.5:
            danger = True
            notes.append("委賣增加但價格仍上行")

        if price_delta < -tick * 0.5:
            weak_score += 1
            notes.append("價格較前次掃描轉弱")

    if resistance_line and current is not None:
        distance = (resistance_line - current) / resistance_line * 100
        if 0 <= distance <= 0.5:
            notes.append("接近弱勢基準")

    if not notes:
        notes.append("五檔暫無明顯動態訊號")

    snap = {"price": current or 0, "bid_size": bid_size, "ask_size": ask_size,
            "ts": datetime.now(TW_TZ)}
    hist.append(snap)

    return {
        "bid_size": bid_size,
        "ask_size": ask_size,
        "ask_bid_ratio": round(ratio, 2) if ratio is not None else None,
        "weak_score": weak_score,
        "danger": danger,
        "reason": "、".join(notes),
    }


def _populate_watchlist(candidates):
    legacy._watchlist_today.clear()
    legacy._watchlist_today.extend([
        {**c, "est_vol": max(1, int(c["vol"] * TRIAL_VOL_GOOD_PCT))}
        for c in candidates
    ])


def preopen_scan_once():
    _reset_v2_state_if_needed()
    if not legacy._watchlist_today:
        candidates = screen_v2()
        if candidates:
            _populate_watchlist(candidates)
    for stock in list(legacy._watchlist_today):
        q = fugle_quote_v2(stock["code"])
        if not q:
            continue
        _record_trial(stock, q)
        resistance = min(stock.get("watch_line", float("inf")), stock.get("prev_high") or float("inf"))
        book = analyze_order_book_v2(stock["code"], q, resistance)
        try:
            legacy.log_orderbook_snapshot(stock, q, resistance, book, "preopen_trial")
        except Exception:
            pass
        time.sleep(0.15)


def format_preopen_summary():
    rows = []
    for stock in legacy._watchlist_today:
        s = _trial_summary(stock)
        if not s["available"]:
            continue
        score = (2 if s["good"] else 1 if s.get("volume_shrink") else 0)
        if s["danger"]:
            score -= 2
        rows.append((score, stock, s))
    rows.sort(key=lambda x: x[0], reverse=True)
    if not rows:
        return "🧪 <b>08:44 試撮摘要</b>\n\n目前觀察名單沒有可用試撮資料"

    lines = ["🧪 <b>08:44 試撮摘要｜窮大叔量價法 V2.1</b>"]
    for score, stock, s in rows[:8]:
        icon = "🟢" if s["good"] else "🔴" if s["danger"] else "🟡"
        notes = "、".join(s["notes"]) or "資料不足"
        lines.append(
            f"{icon} <b>{stock['code']} {stock['name']}</b>｜{stock.get('strategy_type','')}\n"
            f"  試撮 {s.get('price_pct', 0):+.2f}%｜末筆量 {s.get('size',0):,}張｜{notes}"
        )
    lines.append("\n09:00 後仍需等價格轉弱與五檔動態確認；不是自動下單訊號")
    return "\n".join(lines)


def preopen_loop():
    global _preopen_summary_date
    logger.info("V2 preopen loop started")
    while True:
        try:
            now = datetime.now(TW_TZ)
            if now.weekday() < 5 and now.hour == PREOPEN_START_HOUR:
                if PREOPEN_START_MINUTE <= now.minute < PREOPEN_END_MINUTE:
                    preopen_scan_once()
                    if (now.minute >= PREOPEN_SUMMARY_MINUTE
                            and _preopen_summary_date != now.date()):
                        if legacy.CHAT_ID:
                            legacy.tg_only(legacy.CHAT_ID, format_preopen_summary())
                        _preopen_summary_date = now.date()
                    time.sleep(30)
                    continue
                if now.minute < PREOPEN_START_MINUTE:
                    time.sleep(30)
                    continue
            time.sleep(300)
        except Exception as e:
            logger.error("V2 preopen loop error: %s", e)
            time.sleep(30)


# ---------------------------------------------------------------------------
# Live intraday candidate logic
# ---------------------------------------------------------------------------
def _confidence(stock, quote, setup_name, ideal_pullback, volume_shrink,
                trial, book, day_high, resistance_line):
    score = 0
    reasons = []

    if stock.get("strategy_score", 0) >= 4:
        score += 1
        reasons.append(stock.get("strategy_type", "量價候選"))
    if stock.get("rel_vol", 0) >= REL_VOL_STRONG:
        score += 1
        reasons.append(f"昨量比{stock['rel_vol']:.1f}x")
    if ideal_pullback:
        score += 2
        reasons.append("小拉約1%")
    else:
        score += 1
    if setup_name == "破開盤價弱勢":
        score += 1
        reasons.append("跌破開盤價")
    if volume_shrink:
        score += 1
        reasons.append("早盤量縮")
    if trial.get("good"):
        score += 1
        reasons.append("試撮量價吻合")
    if day_high < resistance_line:
        score += 1
        reasons.append("未過昨高/+2.5%")
    if book.get("weak_score", 0) > 0:
        score += min(book["weak_score"], 2)
        reasons.append("五檔轉弱")

    if trial.get("danger") or book.get("danger"):
        score -= 3
    grade = "A" if score >= 7 else "B" if score >= ALERT_SCORE_MIN else "C"
    return score, grade, reasons


def intraday_monitor_v2():
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5 or not (9 <= now.hour < legacy.INTRADAY_ALERT_END_HOUR):
        return

    _reset_v2_state_if_needed()
    legacy.reset_daily_state()

    if not legacy._watchlist_today:
        logger.info("V2 intraday: watchlist empty; rebuilding from prior close")
        candidates = screen_v2()
        if not candidates:
            return
        _populate_watchlist(candidates)

    elapsed_min = max(0, (now.hour - 9) * 60 + now.minute)

    for stock in list(legacy._watchlist_today):
        code = stock["code"]
        if code in legacy._alerted_today:
            continue
        quote = fugle_quote_v2(code)
        if not quote:
            continue

        current = quote["current"]
        pct_now = quote["pct"]
        open_price = quote.get("open")
        open_pct = quote.get("open_pct")
        day_high = quote.get("day_high") or current
        prev_high = stock.get("prev_high") or legacy.get_prev_day_high(code, stock.get("market"))
        resistance_line = min(stock["watch_line"], prev_high) if prev_high else stock["watch_line"]

        if day_high >= resistance_line:
            continue

        pullback = VALID_PULLBACK_MIN <= pct_now < VALID_PULLBACK_MAX and current < resistance_line
        ideal_pullback = IDEAL_PULLBACK_MIN <= pct_now <= IDEAL_PULLBACK_MAX
        break_open = (
            open_price and open_pct is not None
            and current < open_price and current < resistance_line
            and (open_pct < legacy.WEAK_OPEN_MAX_PCT
                 or (stock.get("two_strong_opens") and open_pct < legacy.HOT_MONEY_OPEN_MAX_PCT))
        )
        if not (pullback or break_open):
            continue
        setup_name = "破開盤價弱勢" if break_open else "小拉升不過基準"

        good_vol = max(1, int(stock["vol"] * TRIAL_VOL_GOOD_PCT))
        loose_vol = max(1, int(stock["vol"] * TRIAL_VOL_LOOSE_PCT))
        volume_shrink = quote["vol"] <= good_vol if elapsed_min <= 10 else quote["vol"] <= max(loose_vol, int(stock["vol"] * 0.08))
        if elapsed_min <= 10 and quote["vol"] > loose_vol:
            logger.info("%s early volume %s > %s; skip", code, quote["vol"], loose_vol)
            continue

        trial = _trial_summary(stock)
        book = analyze_order_book_v2(code, quote, resistance_line)
        try:
            legacy.log_orderbook_snapshot(stock, quote, resistance_line, book, "watchlist_scan", setup_name)
        except Exception:
            pass

        if trial.get("danger") or book.get("danger"):
            logger.info("%s V2 danger filter: trial=%s book=%s", code, trial.get("notes"), book.get("reason"))
            continue

        score, grade, reasons = _confidence(
            stock, quote, setup_name, ideal_pullback, volume_shrink,
            trial, book, day_high, resistance_line
        )
        if score < ALERT_SCORE_MIN:
            continue

        stop_actual = move_ticks(day_high, +1)
        entry_cap = min(open_price, resistance_line) if break_open and open_price else resistance_line
        entry_low = nearest_tick(current)
        entry_high = min(move_ticks(current, +2), move_ticks(entry_cap, -1))
        if entry_high < entry_low:
            entry_high = entry_low
        entry_mid = nearest_tick((entry_low + entry_high) / 2)
        risk = stop_actual - entry_mid
        if risk <= 0:
            continue

        target_2r = floor_to_tick(entry_mid - risk * 2)
        scalp2 = move_ticks(entry_mid, -2)
        scalp3 = move_ticks(entry_mid, -3)
        scalp5 = move_ticks(entry_mid, -5)
        stop_pct = round(risk / entry_mid * 100, 2)

        legacy._alerted_today.add(code)
        legacy._today_trades.append({
            "code": code, "name": stock["name"], "market": stock.get("market"),
            "entry": entry_mid, "stop": stop_actual, "setup": setup_name,
            "target": target_2r, "target_2r": target_2r,
            "target_scalp": scalp3, "scalp_2": scalp2, "scalp_3": scalp3, "scalp_5": scalp5,
            "watch_line": resistance_line, "time": now.strftime("%H:%M"),
            "grade": grade, "score": score, "strategy_type": stock.get("strategy_type"),
        })

        trial_text = "、".join(trial.get("notes", [])) if trial.get("available") else "未取得/伺服器重啟後無試撮歷史"
        daily_text = "、".join(stock.get("daily_notes", [])) or "量價候選"
        broker_text = "、".join(stock.get("broker_notes", [])) or ("未接分點資料" if not legacy.FINMIND_TOKEN else "分點無明顯異常")
        reason_text = "、".join(reasons)
        open_text = f"{open_price}（{open_pct:+.2f}%）" if open_price and open_pct is not None else "無資料"

        alert = (
            f"🚨 <b>{grade}級短空候選｜V2.1｜{now.strftime('%H:%M')}</b>\n\n"
            f"<b>{code} {stock['name']}</b> [{stock['market']}]｜分數 <b>{score}</b>\n"
            f"  🧩 類型：{stock.get('strategy_type','量價候選')}\n"
            f"  ✅ 原因：{reason_text}\n"
            f"  📊 昨日：收 {stock['close']}｜{stock['pct']:+.2f}%｜{stock['vol']:,}張｜量比 {stock.get('rel_vol',0):.1f}x\n"
            f"  🧭 日線：{daily_text}\n"
            f"  🧪 試撮：{trial_text}\n"
            f"  📍 現價：<b>{current}</b>（{pct_now:+.2f}%）｜開盤 {open_text}\n"
            f"  🧱 今日高 {day_high}｜弱勢基準 <b>{resistance_line}</b>｜昨高 {prev_high or 'N/A'}\n"
            f"  📦 早盤量：{quote['vol']:,}張｜參考量縮 ≤{good_vol:,}（放寬 {loose_vol:,}）\n"
            f"  📚 五檔：買 {book['bid_size']:,} / 賣 {book['ask_size']:,}｜{book['reason']}\n"
            f"  🧾 分點：{broker_text}\n\n"
            f"  ━━━━━━ 紙上進場參考 ━━━━━━\n"
            f"  🎯 掛空：<b>{entry_low}~{entry_high}</b>（試算 {entry_mid}）\n"
            f"  🛑 停損：早盤高點上一檔 <b>{stop_actual}</b>（約 +{stop_pct:.2f}%）\n"
            f"  ⚡ Scalp：2檔 {scalp2}｜<b>3檔 {scalp3}</b>｜5檔 {scalp5}\n"
            f"  💰 2R：<b>{target_2r}</b>\n\n"
            f"⚠️ 候選提醒，不是自動下單；若委賣被持續吃掉且價格上推就取消空方想法"
        )
        legacy.tg_only(legacy.CHAT_ID, alert)
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def format_report_v2(candidates):
    last_day = legacy.get_last_trading_day()
    next_day = legacy.get_next_trading_day(last_day)
    if not candidates:
        return (
            f"📊 <b>{last_day.strftime('%m/%d')} V2.1 收盤篩選</b>\n\n"
            "今日沒有符合『爆量強勢 / 連漲乖離 / 高檔三日不過高』的標的"
        )

    _populate_watchlist(candidates)
    lines = [f"📊 <b>{last_day.strftime('%m/%d')} 收盤｜{next_day.strftime('%m/%d')} V2.1觀察名單（{len(candidates)}支）</b>"]
    for c in candidates:
        est = max(1, int(c["vol"] * TRIAL_VOL_GOOD_PCT))
        loose = max(1, int(c["vol"] * TRIAL_VOL_LOOSE_PCT))
        notes = "、".join(c.get("daily_notes", [])) or "一般量價候選"
        lines.append(
            f"\n{c['signal']} <b>{c['code']} {c['name']}</b>｜{c.get('strategy_type','')}\n"
            f"  收 {c['close']}｜{c['pct']:+.2f}%｜{c['vol']:,}張｜5日量比 <b>{c.get('rel_vol',0):.1f}x</b>\n"
            f"  3日 {c.get('three_day_gain',0):+.1f}%｜MA5乖離 {c.get('sma5_extension',0):+.1f}%｜{notes}\n"
            f"  弱勢基準：昨高 {c.get('prev_high')} 與 +2.5%({c['watch_line']}) 取低\n"
            f"  試撮/開盤量：理想 ≤{est:,}張；放寬 ≤{loose:,}張\n"
            f"  Scalp參考：{c['scalp_2']} / {c['scalp_3']} / {c['scalp_5']}｜2R參考 {c['target_2r']}"
        )
    lines.append(
        "\n🧪 08:30~08:45 自動收集試撮；08:44 Telegram摘要\n"
        "🔔 09:00~10:00 監控小拉約1%、破開盤價、早盤量縮與五檔撤單/吸收\n"
        "⚠️ 只做候選提醒，不自動下單"
    )
    return "\n".join(lines)


def format_line_morning_v2(candidates):
    next_day = legacy.get_next_trading_day(legacy.get_last_trading_day())
    if not candidates:
        return f"📊 {next_day.strftime('%m/%d')} 今日無V2.1短空觀察標的"
    lines = [f"📊 {next_day.strftime('%m/%d')} 短空觀察 V2.1"]
    for c in candidates[:8]:
        lines.append(
            f"\n{'🔴' if c.get('strategy_score',0)>=6 else '🟠'} {c['code']} {c['name']}｜{c.get('strategy_type','')}\n"
            f"昨收 {c['close']} {c['pct']:+.2f}%｜量比 {c.get('rel_vol',0):.1f}x｜3日 {c.get('three_day_gain',0):+.1f}%\n"
            f"看：08:30試撮量縮 → 小拉約1% → 不過昨高/+2.5% → 價格轉弱"
        )
    lines.append("\n08:44 Telegram會再發試撮摘要；盤中候選仍需確認五檔沒有強勢吸收")
    return "\n".join(lines)


def _resolve_paper_exit(t, target):
    code = t["code"]
    stop = t["stop"]
    et, ep, result = legacy.find_exit_time(code, t["time"], target, stop)
    if et and ep and result:
        return et, ep, result
    q = fugle_quote_v2(code)
    if q:
        day_low = q.get("day_low") or q["current"]
        day_high = q.get("day_high") or q["current"]
        if day_high >= stop:
            return "盤中", stop, "❌ 停損/順序不明"
        if day_low <= target:
            return "盤中", target, "✅ 目標達到"
        return "13:25", q["current"], "⚪ 收盤了結"
    return "13:25", t["entry"], "⚪ 無即時資料"


def format_daily_summary_v2():
    now = datetime.now(TW_TZ)
    if not legacy._today_trades:
        return f"📋 <b>{now.strftime('%m/%d')} V2.1收盤結算</b>\n\n今日無盤中候選進場記錄"

    lines = [f"📋 <b>{now.strftime('%m/%d')} V2.1紙上結算</b>",
             f"共 {len(legacy._today_trades)} 筆｜同時比較 Scalp 3檔 vs 2R"]
    scalp_total = 0
    r2_total = 0
    for t in legacy._today_trades:
        scalp_target = t.get("target_scalp") or move_ticks(t["entry"], -3)
        r2_target = t.get("target_2r") or t["target"]
        st, sp, sr = _resolve_paper_exit(t, scalp_target)
        rt, rp, rr = _resolve_paper_exit(t, r2_target)
        _, scalp_net = calc_pnl_v2(t["entry"], sp)
        _, r2_net = calc_pnl_v2(t["entry"], rp)
        scalp_total += scalp_net
        r2_total += r2_net
        lines.append(
            f"\n<b>{t['code']} {t['name']}</b>｜{t.get('grade','')}級 {t.get('setup','')}\n"
            f"  進場 {t['time']} @ {t['entry']}｜停損 {t['stop']}\n"
            f"  ⚡3檔：{st} @ {sp}｜{sr}｜淨 {scalp_net:+,}元/張\n"
            f"  💰2R：{rt} @ {rp}｜{rr}｜淨 {r2_net:+,}元/張"
        )
    lines.append(
        f"\n━━━━━━━━━━━━\n"
        f"⚡ Scalp 3檔合計：<b>{scalp_total:+,}元</b>\n"
        f"💰 2R合計：<b>{r2_total:+,}元</b>\n"
        "⚠️ 紙上試算，手續費以牌告0.1425%雙邊估算；未套個人折扣"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Efficient V2 backtest (historical trial/book data is not available)
# ---------------------------------------------------------------------------
def _fetch_intraday_map(symbol: str, period_days: int):
    interval = "1m" if period_days <= 7 else "1h"
    range_str = "7d" if period_days <= 7 else "1mo" if period_days <= 30 else "3mo"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": range_str, "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=12)
        if r.status_code != 200:
            return {}, interval
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return {}, interval
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        out = defaultdict(list)
        for ts, o, h, l, c, v in zip(
            chart.get("timestamp", []), q.get("open", []), q.get("high", []),
            q.get("low", []), q.get("close", []), q.get("volume", []),
        ):
            if None in (o, h, l, c):
                continue
            dt = datetime.fromtimestamp(ts, TW_TZ)
            if not (9 <= dt.hour <= 13):
                continue
            out[dt.date()].append({
                "dt": dt, "open": float(o), "high": float(h), "low": float(l),
                "close": float(c), "vol": int(v or 0) // 1000,
            })
        for day in out:
            out[day].sort(key=lambda x: x["dt"])
        return dict(out), interval
    except Exception as e:
        logger.debug("intraday map %s: %s", symbol, e)
        return {}, interval


def _simulate_short(rows, start_idx, entry, stop, target):
    for row in rows[start_idx:]:
        if row["high"] >= stop:
            return row["dt"].strftime("%H:%M"), stop, "停損"
        if row["low"] <= target:
            return row["dt"].strftime("%H:%M"), target, "達標"
    return rows[-1]["dt"].strftime("%H:%M"), rows[-1]["close"], "收盤/末筆"


def backtest_symbol_v2(symbol: str, period_days: int):
    daily = _fetch_daily_rows(symbol, "3mo")
    if len(daily) < 12:
        return []
    intraday_map, interval = _fetch_intraday_map(symbol, period_days)
    if not intraday_map:
        return []

    cutoff = (datetime.now(TW_TZ) - timedelta(days=period_days)).date()
    trades = []
    code = symbol.replace(".TW", "").replace(".TWO", "")
    name = legacy.STOCK_NAMES.get(code, code)

    for i in range(10, len(daily) - 1):
        today = daily[i]
        next_day = daily[i + 1]
        if today["date"] < cutoff:
            continue
        m = _metrics_from_rows(daily, i)
        if not m or not m["eligible"] or today["close"] > legacy.SCREEN_MAX_PRICE:
            continue
        bars = intraday_map.get(next_day["date"], [])
        if not bars:
            continue

        watch_line = ceil_to_tick(today["close"] * 1.025)
        resistance = min(watch_line, today["high"])
        open_price = bars[0]["open"]
        open_pct = (open_price - today["close"]) / today["close"] * 100
        cum_vol = 0
        setup_idx = None
        setup_name = None
        ideal = False

        for idx, row in enumerate(bars):
            if row["dt"].hour >= 10:
                break
            cum_vol += row.get("vol", 0)
            current = row["close"]
            pct_now = (current - today["close"]) / today["close"] * 100
            high_so_far = max(x["high"] for x in bars[:idx+1])
            if high_so_far >= resistance:
                break
            pullback = VALID_PULLBACK_MIN <= pct_now < VALID_PULLBACK_MAX and current < resistance
            break_open = current < open_price and current < resistance and open_pct < legacy.WEAK_OPEN_MAX_PCT
            if not (pullback or break_open):
                continue

            if interval == "1m" and idx <= 10:
                loose = max(1, int(today["vol"] * TRIAL_VOL_LOOSE_PCT))
                if cum_vol > loose:
                    continue
            setup_idx = idx
            setup_name = "破開盤價弱勢" if break_open else "小拉升不過基準"
            ideal = IDEAL_PULLBACK_MIN <= pct_now <= IDEAL_PULLBACK_MAX
            break

        if setup_idx is None:
            continue

        row = bars[setup_idx]
        high_so_far = max(x["high"] for x in bars[:setup_idx+1])
        entry = nearest_tick(row["close"])
        stop = move_ticks(high_so_far, +1)
        risk = stop - entry
        if risk <= 0:
            continue
        target_2r = floor_to_tick(entry - risk * 2)
        target_scalp = move_ticks(entry, -3)

        st, sp, sr = _simulate_short(bars, setup_idx, entry, stop, target_scalp)
        rt, rp, rr = _simulate_short(bars, setup_idx, entry, stop, target_2r)
        _, scalp_net = calc_pnl_v2(entry, sp)
        _, r2_net = calc_pnl_v2(entry, rp)
        notional = entry * 1000
        scalp_pct = scalp_net / notional * 100 if notional else 0
        r2_pct = r2_net / notional * 100 if notional else 0

        trades.append({
            "code": code, "name": name,
            "date": today["date"].strftime("%m/%d"),
            "next_date": next_day["date"].strftime("%m/%d"),
            "close": round(today["close"], 2), "pct": m["pct"],
            "rel_vol": m["rel_vol"], "three_day_gain": m["three_day_gain"],
            "strategy_type": "+".join(m["strategy_types"]),
            "open": round(open_price, 2), "open_pct": round(open_pct, 2),
            "entry": entry, "entry_time": row["dt"].strftime("%H:%M"),
            "watch_line": resistance, "stop": stop,
            "target": target_2r, "target_2r": target_2r, "target_scalp": target_scalp,
            "profit": round(r2_pct, 3), "win": r2_net > 0,
            "scalp_profit": round(scalp_pct, 3), "scalp_win": scalp_net > 0,
            "r2_result": rr, "r2_exit_time": rt,
            "scalp_result": sr, "scalp_exit_time": st,
            "setup": setup_name + ("｜約1%" if ideal else ""),
            "interval": interval,
            "hit_in_hour": rr == "達標" and rt < "10:00",
            "notes": m["daily_notes"],
        })
    return trades


def run_backtest_v2(period_days: int):
    symbols = list(legacy.SYMBOLS)
    if BACKTEST_SYMBOL_LIMIT > 0:
        symbols = symbols[:BACKTEST_SYMBOL_LIMIT]
    all_trades = []
    with ThreadPoolExecutor(max_workers=BACKTEST_WORKERS) as ex:
        futs = {ex.submit(backtest_symbol_v2, s, period_days): s for s in symbols}
        for fut in as_completed(futs):
            try:
                all_trades.extend(fut.result())
            except Exception as e:
                logger.debug("backtest worker %s: %s", futs[fut], e)

    symbol_results = {}
    grouped = defaultdict(list)
    for t in all_trades:
        grouped[t["code"]].append(t)
    for code, trades in grouped.items():
        total = len(trades)
        r2wins = sum(1 for t in trades if t["win"])
        swins = sum(1 for t in trades if t["scalp_win"])
        symbol_results[code] = {
            "name": trades[0]["name"], "total": total,
            "wins": r2wins, "win_rate": round(r2wins / total * 100, 1),
            "scalp_wins": swins, "scalp_win_rate": round(swins / total * 100, 1),
            "avg_profit": round(sum(t["profit"] for t in trades) / total, 3),
            "avg_scalp": round(sum(t["scalp_profit"] for t in trades) / total, 3),
            "hour_wins": sum(1 for t in trades if t.get("hit_in_hour")),
            "hour_win_rate": round(sum(1 for t in trades if t.get("hit_in_hour")) / total * 100, 1),
        }
    return symbol_results, all_trades


def format_backtest_v2(period_days, symbol_results, all_trades):
    interval_note = "1分K（可近似測早盤量縮/Scalp）" if period_days <= 7 else "1小時K（Scalp僅粗略，不視為真實1~3分鐘績效）"
    lines = [f"📈 <b>V2.1回測｜近{period_days}天</b>",
             f"範圍：{len(legacy.SYMBOLS) if BACKTEST_SYMBOL_LIMIT <= 0 else BACKTEST_SYMBOL_LIMIT}支｜{interval_note}"]
    if not all_trades:
        lines.append("\n此期間沒有可回測的V2候選進場")
        return "\n".join(lines)

    total = len(all_trades)
    r2wins = sum(1 for t in all_trades if t["win"])
    swins = sum(1 for t in all_trades if t["scalp_win"])
    avg2r = sum(t["profit"] for t in all_trades) / total
    avgs = sum(t["scalp_profit"] for t in all_trades) / total
    lines.append(
        f"\n📊 <b>整體 {total}筆</b>\n"
        f"  ⚡ Scalp 3檔：勝率 <b>{swins/total*100:.1f}%</b>｜平均淨報酬 {avgs:+.3f}%\n"
        f"  💰 2R：勝率 <b>{r2wins/total*100:.1f}%</b>｜平均淨報酬 {avg2r:+.3f}%"
    )

    ranking = sorted(symbol_results.items(), key=lambda kv: (
        kv[1]["total"] >= 2, kv[1]["scalp_win_rate"], kv[1]["win_rate"], kv[1]["avg_profit"]
    ), reverse=True)
    if ranking:
        lines.append("\n🏆 <b>樣本較好的標的</b>")
        for code, r in ranking[:5]:
            lines.append(
                f"  {code} {r['name']}｜{r['total']}筆｜Scalp {r['scalp_win_rate']:.1f}%｜2R {r['win_rate']:.1f}%"
            )

    recent = sorted(all_trades, key=lambda x: (x["date"], x["entry_time"]), reverse=True)[:4]
    lines.append("\n📅 <b>最近案例</b>")
    for t in recent:
        lines.append(
            f"  {t['code']} {t['name']}｜{t['date']}→{t['next_date']}｜{t['strategy_type']}\n"
            f"   昨{t['pct']:+.1f}% 量比{t['rel_vol']:.1f}x｜{t['setup']} {t['entry_time']}@{t['entry']}\n"
            f"   Scalp {t['scalp_result']} {t['scalp_profit']:+.3f}%｜2R {t['r2_result']} {t['profit']:+.3f}%"
        )
    lines.append(
        "\n⚠️ 歷史回測沒有08:30試撮完整序列、逐筆五檔與股期資料；這些條件只會從V2上線後即時累積。"
    )
    return "\n".join(lines)


def format_test_v2():
    base = _original_format_test()
    return (
        base
        + "\n\n✅ Strategy overlay：V2.1"
        + "\n✅ 台股跳動單位：啟用"
        + "\n✅ 試撮監控：08:30~08:45 / 08:44摘要"
        + f"\n✅ 相對爆量門檻：{REL_VOL_MIN:.1f}x（強爆量 {REL_VOL_STRONG:.1f}x）"
        + f"\n✅ 回測範圍：{'全部'+str(len(legacy.SYMBOLS))+'支' if BACKTEST_SYMBOL_LIMIT <= 0 else str(BACKTEST_SYMBOL_LIMIT)+'支'}"
    )


# ---------------------------------------------------------------------------
# Monkey-patch the legacy module.
# ---------------------------------------------------------------------------
legacy.calc_pnl = calc_pnl_v2
legacy.calc_trade = calc_trade_v2
legacy.fugle_quote = fugle_quote_v2
legacy.screen = screen_v2
legacy.intraday_monitor = intraday_monitor_v2
legacy.format_report = format_report_v2
legacy.format_line_morning = format_line_morning_v2
legacy.format_daily_summary = format_daily_summary_v2
legacy.run_backtest = run_backtest_v2
legacy.format_backtest = format_backtest_v2
legacy.format_test = format_test_v2


def _index_v2():
    return "📈 短空機器人 V2.1 運行中（相對爆量 + 試撮 + 動態五檔 + Scalp/2R｜僅提醒不下單）"

legacy.app.view_functions["index"] = _index_v2


@app.route("/v2-status")
def v2_status():
    return {
        "status": "ok",
        "version": "2.1",
        "mode": "alerts_only",
        "watchlist": len(legacy._watchlist_today),
        "alerted_today": len(legacy._alerted_today),
        "trial_symbols": len(_trial_history),
        "book_symbols": len(_book_history),
        "time": datetime.now(TW_TZ).isoformat(),
    }


threading.Thread(target=preopen_loop, daemon=True, name="v2-preopen").start()
logger.info("Short-bot strategy V2.1 overlay loaded")
