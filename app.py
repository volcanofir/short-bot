import os
import re
import requests
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
import logging
import threading
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
FUGLE_TOKEN = os.environ.get("FUGLE_API_TOKEN", "")

TW_TZ = pytz.timezone("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# ── 盤中監控狀態 ──
_watchlist_today = []
_alerted_today = set()
_last_reset_date = None
_today_trades = []  # 今日盤中提醒進場的交易記錄 {code, name, entry, stop, target, watch_line}

STOCK_NAMES = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2382": "廣達",
    "3711": "日月光投控", "2308": "台達電", "2303": "聯電", "2379": "瑞昱",
    "2301": "光寶科", "2344": "華邦電", "2337": "旺宏", "2360": "致茂",
    "3034": "聯詠", "3036": "文曄", "2388": "威盛", "2376": "技嘉",
    "6669": "緯穎", "2347": "聯強", "2357": "華碩", "2395": "研華",
    "3008": "大立光", "2449": "京元電子", "2408": "南亞科", "2353": "宏碁",
    "2385": "群光", "2323": "中環", "2340": "光磊", "3041": "揚智",
    "2351": "順德", "6533": "晶心科", "3529": "力旺", "6271": "同欣電",
    "3231": "緯創", "2368": "金像電", "2409": "友達", "2882": "國泰金",
    "2881": "富邦金", "2886": "兆豐金", "2884": "玉山金", "2891": "中信金",
    "2883": "開發金", "2880": "華南金", "2885": "元大金", "2887": "台新金",
    "2892": "第一金", "1301": "台塑", "1303": "南亞", "1326": "台化",
    "2002": "中鋼", "1402": "遠東新", "2207": "和泰車", "2201": "裕隆",
    "1216": "統一", "2912": "統一超", "1101": "台泥", "1102": "亞泥",
    "2603": "長榮", "2609": "陽明", "2615": "萬海", "2618": "長榮航",
    "2610": "華航", "3023": "信邦", "3037": "欣興", "5274": "信驊",
    "3705": "永信", "4966": "譜瑞-KY", "6515": "穎崴", "3714": "聯合骨科",
    "8069": "元太", "6770": "力積電", "6472": "寶齡富錦", "3596": "智易",
    "6488": "環球晶", "4919": "新唐", "3545": "旭曜", "6510": "精測",
    "3086": "華義", "6531": "愛普", "6278": "台表科", "5347": "世界",
    "3035": "智原", "6257": "矽格", "2421": "建準", "4736": "泰博",
    "3443": "創意", "4958": "臻鼎-KY", "6670": "復盛應用", "6548": "長科",
    "3047": "訊舟", "6491": "晶碩", "3661": "世芯-KY", "2399": "映泰",
    "6116": "彩晶", "3374": "精材", "2433": "互盛電", "3680": "家登",
    "6592": "和潤企業", "4977": "眾達-KY", "8046": "南電", "6415": "矽力-KY",
    "3016": "嘉澤", "2429": "銘旺科", "6285": "啟碁", "3694": "新美齊",
    "2441": "超豐", "2913": "農林", "3702": "大聯大", "6239": "力成",
    "2492": "華新科", "3013": "晟銘電", "2474": "可成", "6443": "元晶",
    "3533": "嘉聯益", "6409": "旭隼", "2412": "中華電", "4938": "和碩",
    "3006": "晶豪科", "2327": "國巨", "2356": "英業達", "2324": "仁寶",
    "2352": "佳世達", "2377": "微星", "2383": "台光電", "2392": "正崴",
    "2405": "訊碁", "2498": "宏達電", "5871": "中租-KY", "3481": "群創",
    "2393": "億光", "6147": "頎邦", "2363": "矽統", "3045": "台灣大",
    "6789": "采鈺", "6269": "台郡", "3583": "辛耘", "4961": "天鈺",
    "6191": "精成科", "8112": "至上", "6756": "立積", "6679": "鈺太",
    "6477": "安集", "6446": "藥華藥", "3653": "健策", "3189": "景碩",
    "5269": "祥碩", "6227": "佳必琪", "3311": "閎康",
}

SYMBOLS = [
    "2330.TW","2317.TW","2454.TW","2382.TW","3711.TW","2308.TW","2303.TW",
    "2379.TW","2301.TW","2344.TW","2337.TW","2360.TW","3034.TW","3036.TW",
    "2388.TW","2376.TW","6669.TW","2347.TW","2357.TW","2395.TW","3008.TW",
    "2449.TW","2408.TW","2353.TW","2385.TW","2323.TW","2340.TW","3041.TW",
    "2351.TW","6533.TW","3529.TW","6271.TW","3231.TW","2368.TW","2409.TW",
    "2882.TW","2881.TW","2886.TW","2884.TW","2891.TW","2883.TW","2880.TW",
    "2885.TW","2887.TW","2892.TW","1301.TW","1303.TW","1326.TW","2002.TW",
    "1402.TW","2207.TW","2201.TW","1216.TW","2912.TW","1101.TW","1102.TW",
    "2603.TW","2609.TW","2615.TW","2618.TW","2610.TW","3023.TW","3037.TW",
    "5274.TW","3705.TW","4966.TW","6515.TW","3714.TW","8069.TW","6770.TW",
    "6472.TW","3596.TW","6488.TW","4919.TW","3545.TW","6510.TW","3086.TW",
    "6531.TW","6278.TW","5347.TW","3035.TW","6257.TW","2421.TW","4736.TW",
    "3443.TW","4958.TW","6670.TW","6548.TW","3047.TW","6491.TW","3661.TW",
    "2399.TW","6116.TW","3374.TW","2433.TW","3680.TW","6592.TW","4977.TW",
    "8046.TW","6415.TW","3016.TW","2429.TW","6285.TW","3694.TW","2441.TW",
    "2913.TW","3702.TW","6239.TW","2492.TW","3013.TW","2474.TW","6443.TW",
    "3533.TW","6409.TW","2412.TW","4938.TW","3006.TW","2327.TW","2356.TW",
    "2324.TW","2352.TW","2377.TW","2383.TW","2392.TW","2405.TW","2498.TW",
    "5871.TW","3481.TW","2393.TW","6147.TW","2363.TW","3045.TW",
    "6789.TWO","6269.TWO","3583.TWO","4961.TWO","6191.TWO","8112.TWO",
    "6756.TWO","6679.TWO","6477.TWO","6446.TWO","3653.TWO","3189.TWO",
    "5269.TWO","6227.TWO","3311.TWO",
]


# ──────────────────────────────────────────────
# 發送函式
# ──────────────────────────────────────────────

def tg_send(chat_id, text, parse_mode="HTML"):
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        logger.error(f"TG send error: {e}")


def line_send(text):
    """發送純文字到 LINE"""
    if not LINE_TOKEN or not LINE_USER_ID:
        return
    try:
        clean = re.sub(r'<[^>]+>', '', text)
        clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LINE_TOKEN}"},
            json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": clean}]},
            timeout=10
        )
        logger.info("LINE sent OK")
    except Exception as e:
        logger.error(f"LINE send error: {e}")


def tg_only(chat_id, text):
    """只發 Telegram"""
    if chat_id:
        tg_send(chat_id, text)


def broadcast(chat_id, text):
    """同時發 Telegram + LINE"""
    if chat_id:
        tg_send(chat_id, text)
    line_send(text)


# ──────────────────────────────────────────────
# 日期工具
# ──────────────────────────────────────────────

def get_last_trading_day():
    now = datetime.now(TW_TZ)
    d = now.date()
    if now.hour < 13 or (now.hour == 13 and now.minute < 30):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def get_next_trading_day(d):
    next_d = d + timedelta(days=1)
    while next_d.weekday() >= 5:
        next_d += timedelta(days=1)
    return next_d


def calc_trade(close):
    watch_line = round(close * 1.025, 1)
    stop_ref = round(close * 1.020, 1)
    risk = stop_ref - close
    target_ref = round(close - risk * 2, 1)
    stop_pct = round(risk / close * 100, 2)
    target_pct = round(risk * 2 / close * 100, 2)
    return {
        "watch_line": watch_line, "stop_ref": stop_ref,
        "target_ref": target_ref, "stop_pct": stop_pct, "target_pct": target_pct
    }


def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


# ──────────────────────────────────────────────
# Fugle 即時報價
# ──────────────────────────────────────────────

def fugle_quote(code):
    if not FUGLE_TOKEN:
        return None
    try:
        url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{code}"
        r = requests.get(url, headers={"X-API-KEY": FUGLE_TOKEN}, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        current = data.get("lastPrice") or data.get("closePrice")
        prev_close = data.get("previousClose") or data.get("referencePrice")
        day_high = data.get("highPrice")
        day_low = data.get("lowPrice")
        volume = data.get("totalVolume", 0)
        if not current or not prev_close or prev_close <= 0:
            return None
        pct = (current - prev_close) / prev_close * 100
        return {
            "current": round(current, 2),
            "prev_close": round(prev_close, 2),
            "pct": round(pct, 2),
            "vol": int(volume) // 1000,
            "day_high": round(day_high, 2) if day_high else None,
            "day_low": round(day_low, 2) if day_low else None,
        }
    except Exception as e:
        logger.error(f"Fugle quote error {code}: {e}")
        return None


# ──────────────────────────────────────────────
# 資料抓取（Yahoo 收盤後）
# ──────────────────────────────────────────────

def fetch_stock_history(symbol, days=5):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": f"{days}d", "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = q.get("close", [])
        volumes = q.get("volume", [])
        timestamps = chart.get("timestamp", [])
        valid = [(t, c, v) for t, c, v in zip(timestamps, closes, volumes)
                 if c is not None and v is not None]
        if len(valid) < 2:
            return None
        prev_close = valid[-2][1]
        last_close = valid[-1][1]
        last_vol = valid[-1][2]
        if prev_close <= 0:
            return None
        pct = (last_close - prev_close) / prev_close * 100
        vol = int(last_vol) // 1000
        return {"close": round(last_close, 2), "pct": round(pct, 2), "vol": vol}
    except Exception:
        return None


def fetch_intraday_yahoo(symbol, target_date):
    now = datetime.now(TW_TZ)
    days_ago = (now.date() - target_date).days
    interval = "1m" if days_ago <= 6 else "1h"
    range_str = "7d" if days_ago <= 6 else "1mo"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": range_str, "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        highs = q.get("high", [])
        lows = q.get("low", [])
        timestamps = chart.get("timestamp", [])
        open_highs, open_lows = [], []
        for ts, h, l in zip(timestamps, highs, lows):
            if h is None or l is None:
                continue
            dt = datetime.fromtimestamp(ts, TW_TZ)
            if dt.date() != target_date:
                continue
            open_t = dt.replace(hour=9, minute=0, second=0, microsecond=0)
            close_t = dt.replace(hour=10, minute=0, second=0, microsecond=0)
            if open_t <= dt <= close_t:
                open_highs.append(h)
                open_lows.append(l)
        if not open_highs:
            return None
        return {"max_high": max(open_highs), "min_low": min(open_lows), "interval": interval}
    except Exception as e:
        logger.error(f"Intraday error {symbol}: {e}")
        return None


def fetch_all():
    results = []
    headers = {"User-Agent": UA, "Accept": "application/json"}
    for i in range(0, len(SYMBOLS), 40):
        batch = SYMBOLS[i:i+40]
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={','.join(batch)}"
        try:
            r = requests.get(url, headers=headers, timeout=12)
            quotes = r.json().get("quoteResponse", {}).get("result", []) or []
            for q in quotes:
                try:
                    symbol = str(q.get("symbol", ""))
                    code = symbol.replace(".TW", "").replace(".TWO", "")
                    name = STOCK_NAMES.get(code, q.get("shortName", code))
                    close = float(q.get("regularMarketPrice", 0) or 0)
                    prev = float(q.get("regularMarketPreviousClose", 0) or 0)
                    chg = float(q.get("regularMarketChange", 0) or 0)
                    vol = int(q.get("regularMarketVolume", 0) or 0) // 1000
                    if close <= 0 or prev <= 0 or close > 1000:
                        continue
                    pct = chg / prev * 100
                    market = "上市" if symbol.endswith(".TW") else "上櫃"
                    if 3.0 <= pct <= 7.0 and vol >= 500:
                        trade = calc_trade(close)
                        results.append({
                            "market": market, "code": code, "name": name,
                            "close": round(close, 2), "pct": round(pct, 2),
                            "vol": vol, "signal": get_signal(pct, vol), **trade
                        })
                except Exception:
                    continue
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"v7 error: {e}")

    if not results:
        for symbol in SYMBOLS[:100]:
            data = fetch_stock_history(symbol)
            if data and 3.0 <= data["pct"] <= 7.0 and data["vol"] >= 500 and data["close"] <= 1000:
                code = symbol.replace(".TW", "").replace(".TWO", "")
                market = "上市" if symbol.endswith(".TW") else "上櫃"
                name = STOCK_NAMES.get(code, code)
                trade = calc_trade(data["close"])
                results.append({
                    "market": market, "code": code, "name": name,
                    "close": data["close"], "pct": data["pct"],
                    "vol": data["vol"], "signal": get_signal(data["pct"], data["vol"]),
                    **trade
                })
            time.sleep(0.05)

    logger.info(f"fetch_all: {len(results)} results")
    return results


def screen():
    candidates = fetch_all()
    seen = {}
    for c in candidates:
        code = c["code"]
        if code not in seen or c["vol"] > seen[code]["vol"]:
            seen[code] = c
    result = list(seen.values())
    result.sort(key=lambda x: x["vol"], reverse=True)
    return result[:10]


# ──────────────────────────────────────────────
# 盤中監控（Fugle 即時，含進場建議）
# ──────────────────────────────────────────────

def reset_daily_state():
    global _alerted_today, _last_reset_date, _today_trades
    today = datetime.now(TW_TZ).date()
    if _last_reset_date != today:
        _alerted_today = set()
        _today_trades.clear()
        _last_reset_date = today
        logger.info(f"Daily reset for {today}")


def get_prev_day_high(code):
    """取得昨日最高價（用於判斷是否突破前高）"""
    symbol = f"{code}.TW"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "5d", "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        highs = q.get("high", [])
        valid_highs = [h for h in highs if h is not None]
        if len(valid_highs) < 2:
            return None
        # 倒數第二根日K的高點 = 昨日高點
        return round(valid_highs[-2], 2)
    except Exception as e:
        logger.error(f"get_prev_day_high {code}: {e}")
        return None


def intraday_monitor():
    """
    盤中監控：每5分鐘掃描觀察名單
    用 Fugle 取得即時報價 + 當日高點
    符合條件時推播進場建議（只發 Telegram，不發 LINE）
    """
    global _alerted_today
    now = datetime.now(TW_TZ)

    if now.weekday() >= 5:
        return
    if not (9 <= now.hour < 13 or (now.hour == 13 and now.minute <= 30)):
        return

    reset_daily_state()

    if not _watchlist_today:
        return

    logger.info(f"Intraday monitor: {len(_watchlist_today)} stocks")

    for stock in _watchlist_today:
        code = stock["code"]
        if code in _alerted_today:
            continue

        quote = fugle_quote(code)
        if not quote:
            continue

        current = quote["current"]
        pct_now = quote["pct"]
        day_high = quote["day_high"] or current
        watch_line = stock["watch_line"]

        # 基本進場條件：漲幅在 0.5%~2.5% 之間，還沒過觀察空點
        if not (0.5 <= pct_now < 2.5 and current < watch_line):
            continue

        # 大叔策略：量縮確認（盤中成交量低於昨日總量的 2%）
        est_vol_threshold = stock.get("est_vol", 0)
        if est_vol_threshold > 0 and quote["vol"] > est_vol_threshold * 3:
            # 盤中量是累積的，用3倍試撮門檻當參考（避免太早篩掉）
            logger.info(f"{code} 盤中量{quote['vol']} > 門檻{est_vol_threshold*3}，量未縮，跳過")
            continue

        # 大叔策略：今日高點已突破昨日高點 → 不空，放棄
        prev_high = get_prev_day_high(code)
        if prev_high and day_high >= prev_high:
            logger.info(f"{code} 今日高{day_high} >= 昨日高{prev_high}，跳過不空")
            continue

        _alerted_today.add(code)

        # 以當日實際高點為停損基準（大叔策略：過前高即停損）
        stop_actual = round(day_high + 0.1, 1)  # 過今日高點一檔
        risk = stop_actual - current
        target_actual = round(current - risk * 2, 1)
        stop_pct = round(risk / current * 100, 2)
        target_pct = round(risk * 2 / current * 100, 2)

        # 建議掛空區間：現價到觀察空點之間
        entry_low = round(current * 1.005, 1)
        entry_high = round(watch_line - 0.1, 1)
        entry_mid = round((entry_low + entry_high) / 2, 1)

        # 記錄交易（供收盤結算用）
        _today_trades.append({
            "code": code, "name": stock["name"],
            "entry": entry_mid, "stop": stop_actual,
            "target": target_actual, "watch_line": watch_line,
            "time": now.strftime("%H:%M"),
        })

        now_str = now.strftime("%H:%M")
        prev_high_str = f"{prev_high}" if prev_high else "無資料"
        alert = (
            f"🚨 <b>盤中進場提醒 {now_str}</b>\n\n"
            f"{stock['signal']} <b>{code} {stock['name']}</b> [{stock['market']}]\n"
            f"  📍 現價：<b>{current}</b> 元（+{pct_now:.2f}%）\n"
            f"  📊 今日高點：<b>{day_high}</b> 元 ✅（未突破昨高 {prev_high_str} 元）\n"
            f"  昨收：{stock['close']} 元（昨漲 +{stock['pct']}%）\n\n"
            f"  ━━━━━━ 進場建議 ━━━━━━\n"
            f"  🎯 掛空區間：<b>{entry_low}~{entry_high}</b> 元（試算以 {entry_mid} 元計）\n"
            f"     （等小拉升掛高空，漲不過 {watch_line} 才空）\n\n"
            f"  🛑 停損：過今日高點 <b>{stop_actual}</b> 元（+{stop_pct}%）\n"
            f"  💰 目標：<b>{target_actual}</b> 元（-{target_pct}%，賺賠比 2:1）\n\n"
            f"  ⚠️ 確認試撮量縮 + 趨勢線後再掛單\n"
            f"  ⚠️ 全程當沖，收盤前了結"
        )

        # 盤中提醒只發 Telegram，不打擾 LINE
        tg_only(CHAT_ID, alert)
        logger.info(f"Intraday alert: {code}")

        time.sleep(0.3)


# ──────────────────────────────────────────────
# 回測
# ──────────────────────────────────────────────


def calc_pnl(entry, exit_price, lots=1):
    """計算當沖做空損益（單張）含手續費與稅"""
    gross = (entry - exit_price) * 1000 * lots
    fee = (entry + exit_price) * 1000 * lots * 0.001425  # 買賣手續費
    tax = exit_price * 1000 * lots * 0.0015              # 證交稅（當沖減半）
    net = gross - fee - tax
    return round(gross), round(net)



def find_exit_time(code, entry_time_str, target, stop):
    """
    用 Fugle 分鐘K找到目標價或停損的精確觸及時間
    entry_time_str: "HH:MM" 格式的進場時間
    target: 目標價（空單往下）
    stop: 停損價（空單往上）
    回傳 (exit_time_str, exit_price, result_type)
    """
    if not FUGLE_TOKEN:
        return None, None, None
    try:
        url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/{code}"
        r = requests.get(url,
                         headers={"X-API-KEY": FUGLE_TOKEN},
                         params={"timeFrame": 1},  # 1分K
                         timeout=10)
        if r.status_code != 200:
            return None, None, None

        data = r.json()
        candles = data.get("candles", [])
        if not candles:
            return None, None, None

        today = datetime.now(TW_TZ).date()

        # 解析進場時間
        entry_h, entry_m = map(int, entry_time_str.split(":"))
        entry_dt = datetime.now(TW_TZ).replace(
            hour=entry_h, minute=entry_m, second=0, microsecond=0)

        for c in candles:
            try:
                t_str = c.get("date", "") or c.get("datetime", "")
                if not t_str:
                    continue
                if "T" in t_str:
                    dt = datetime.fromisoformat(
                        t_str.replace("Z", "+00:00")).astimezone(TW_TZ)
                else:
                    dt = datetime.strptime(
                        t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW_TZ)

                # 只看今天、進場時間之後的K棒
                if dt.date() != today or dt < entry_dt:
                    continue

                high = c.get("high", 0)
                low = c.get("low", 0)
                t_str_out = dt.strftime("%H:%M")

                # 同一根K棒先判斷停損（保守）
                if high and high >= stop:
                    return t_str_out, stop, "❌ 停損"
                if low and low <= target:
                    return t_str_out, target, "✅ 目標達到"
            except Exception:
                continue

        return None, None, None
    except Exception as e:
        logger.error(f"find_exit_time {code}: {e}")
        return None, None, None

def format_daily_summary():
    """收盤日結算：計算今日所有盤中提醒假設進場的損益"""
    now = datetime.now(TW_TZ)
    date_str = now.strftime("%m/%d")

    if not _today_trades:
        return f"📋 <b>{date_str} 今日收盤結算</b>\n\n今日無盤中提醒進場記錄"

    lines = [f"📋 <b>{date_str} 今日收盤結算（試算）</b>",
             f"共 {len(_today_trades)} 筆進場\n"]

    total_gross = 0
    total_net = 0

    for t in _today_trades:
        code = t["code"]
        entry = t["entry"]
        target = t["target"]
        stop = t["stop"]

        # 取收盤價
        q = fugle_quote(code)
        if q:
            close_price = q["current"]
            day_low = q.get("day_low") or close_price
            day_high_now = q.get("day_high") or close_price
        else:
            # Fugle 收盤後可能無資料，用 Yahoo 備用
            hist = fetch_stock_history(f"{code}.TW")
            close_price = hist["close"] if hist else entry
            day_low = close_price
            day_high_now = close_price

        # 先嘗試用 Fugle 分鐘K找精確出場時間
        exit_time_exact, exit_price_exact, result_exact = find_exit_time(
            code, t["time"], target, stop)

        if exit_time_exact and exit_price_exact and result_exact:
            # Fugle 分鐘K找到精確時間
            exit_price = exit_price_exact
            result = result_exact
            exit_time = exit_time_exact
        else:
            # 備用：用日K高低點判斷，出場時間估算
            if day_low <= target:
                exit_price = target
                result = "✅ 目標達到"
                exit_time = "盤中"
            elif day_high_now >= stop:
                exit_price = stop
                result = "❌ 停損"
                exit_time = "盤中"
            else:
                exit_price = close_price
                result = "⚪ 收盤了結"
                exit_time = "13:25"

        gross, net = calc_pnl(entry, exit_price)
        total_gross += gross
        total_net += net
        pnl_str = f"+{net:,}" if net >= 0 else f"{net:,}"

        icon = "✅" if net >= 0 else "❌"
        lines.append(
            f"{icon} <b>{code} {t['name']}</b>\n"
            f"  進場：{t['time']} @ {entry}元 → 出場：{exit_time} @ {exit_price}元\n"
            f"  結果：{result}\n"
            f"  每張損益：<b>{pnl_str}</b> 元（含手續費稅）"
        )

    total_str = f"+{total_net:,}" if total_net >= 0 else f"{total_net:,}"
    gross_str = f"+{total_gross:,}" if total_gross >= 0 else f"{total_gross:,}"
    lines.append(
        f"\n━━━━━━━━━━━━\n"
        f"📊 <b>今日合計</b>\n"
        f"  毛利：{gross_str} 元\n"
        f"  淨損益（含費稅）：<b>{total_str}</b> 元\n"
        f"\n⚠️ 以試算進場價（區間中間值）計算，僅供參考"
    )
    return "\n".join(lines)

def backtest_symbol(symbol, period_days):
    range_map = {7: "1mo", 15: "1mo", 30: "3mo"}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": range_map.get(period_days, "1mo"),
              "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        chart = result[0]
        q = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = q.get("close", [])
        volumes = q.get("volume", [])
        timestamps = chart.get("timestamp", [])
        valid = []
        for t, c, v in zip(timestamps, closes, volumes):
            if c is not None and v is not None:
                valid.append({
                    "date": datetime.fromtimestamp(t, TW_TZ).date(),
                    "close": c, "vol": int(v) // 1000
                })
        if len(valid) < 3:
            return []
        cutoff = (datetime.now(TW_TZ) - timedelta(days=period_days)).date()
        valid = [x for x in valid if x["date"] >= cutoff]
        trades = []
        for i in range(1, len(valid) - 1):
            today = valid[i]
            next_day = valid[i+1]
            prev_close = valid[i-1]["close"]
            if prev_close <= 0 or today["close"] <= 0:
                continue
            pct = (today["close"] - prev_close) / prev_close * 100
            if not (3.0 <= pct <= 7.0 and today["vol"] >= 500 and today["close"] <= 1000):
                continue
            watch_line = round(today["close"] * 1.025, 1)
            stop_price = round(today["close"] * 1.020, 1)
            risk = stop_price - today["close"]
            target_price = round(today["close"] - risk * 2, 1)
            intraday = fetch_intraday_yahoo(symbol, next_day["date"])
            time.sleep(0.1)
            if not intraday:
                profit = round((today["close"] - next_day["close"]) / today["close"] * 100, 2)
                trades.append({
                    "date": today["date"].strftime("%m/%d"),
                    "next_date": next_day["date"].strftime("%m/%d"),
                    "close": today["close"], "pct": round(pct, 2),
                    "watch_line": watch_line, "stop": stop_price, "target": target_price,
                    "profit": profit, "result": "無盤中資料",
                    "win": profit > 0, "hit_in_hour": None
                })
            else:
                h1_high = intraday["max_high"]
                h1_low = intraday["min_low"]
                ivl = intraday["interval"]
                if h1_high >= watch_line:
                    continue
                elif h1_high >= stop_price:
                    profit = round(-(stop_price - today["close"]) / today["close"] * 100, 2)
                    result_type = f"停損（{ivl}）"
                    win, hit = False, False
                elif h1_low <= target_price:
                    profit = round((today["close"] - target_price) / today["close"] * 100, 2)
                    result_type = f"✅1小時達標（{ivl}）"
                    win, hit = True, True
                else:
                    profit = round((today["close"] - next_day["close"]) / today["close"] * 100, 2)
                    result_type = "收盤了結"
                    win = profit > 0
                    hit = False
                trades.append({
                    "date": today["date"].strftime("%m/%d"),
                    "next_date": next_day["date"].strftime("%m/%d"),
                    "close": today["close"], "pct": round(pct, 2),
                    "watch_line": watch_line, "stop": stop_price, "target": target_price,
                    "profit": profit, "result": result_type,
                    "win": win, "hit_in_hour": hit
                })
        return trades
    except Exception as e:
        logger.error(f"Backtest {symbol}: {e}")
        return []


def run_backtest(period_days):
    all_trades = []
    symbol_results = {}
    for symbol in SYMBOLS[:30]:
        trades = backtest_symbol(symbol, period_days)
        if trades:
            code = symbol.replace(".TW", "").replace(".TWO", "")
            name = STOCK_NAMES.get(code, code)
            wins = sum(1 for t in trades if t["win"])
            hour_wins = sum(1 for t in trades if t.get("hit_in_hour"))
            total = len(trades)
            avg_profit = sum(t["profit"] for t in trades) / total
            symbol_results[code] = {
                "name": name, "total": total, "wins": wins,
                "hour_wins": hour_wins,
                "win_rate": round(wins/total*100, 1),
                "hour_win_rate": round(hour_wins/total*100, 1),
                "avg_profit": round(avg_profit, 2),
            }
            all_trades.extend(trades)
    return symbol_results, all_trades


# ──────────────────────────────────────────────
# 訊息格式
# ──────────────────────────────────────────────

def format_report(candidates):
    """完整報告（Telegram 用，含 HTML）"""
    global _watchlist_today
    last_day = get_last_trading_day()
    next_day = get_next_trading_day(last_day)
    last_str = last_day.strftime("%m/%d")
    next_str = next_day.strftime("%m/%d")

    if not candidates:
        return (
            f"📊 <b>{last_str} 收盤篩選完畢</b>\n\n"
            "今日無符合條件的標的\n"
            "（量能不足、漲幅不符或股價超過千元）"
        )

    _watchlist_today = [{**c, "est_vol": max(1, int(c["vol"] * 0.02))} for c in candidates]

    lines = [f"📊 <b>{last_str} 收盤篩選｜{next_str} 觀察名單（{len(candidates)} 支）</b>\n"]
    for c in candidates:
        est_vol = max(1, int(c["vol"] * 0.02))
        lines.append(
            f"{c['signal']} <b>{c['code']} {c['name']}</b> [{c['market']}]\n"
            f"  {last_str}收：<b>{c['close']}</b>元  漲幅：<b>+{c['pct']}%</b>  量：<b>{c['vol']:,}</b>張\n"
            f"  ━━━━━━━━━━━━\n"
            f"  👁 {next_str} 觀察空點：漲不過 <b>{c['watch_line']}</b> 元（+2.5%）才考慮空\n"
            f"  🛑 停損參考：過早盤高點（參考 <b>{c['stop_ref']}</b> 元）\n"
            f"  💰 目標參考：<b>{c['target_ref']}</b> 元（賺賠比 2:1）\n"
            f"  📌 試撮量需低於 {est_vol:,} 張"
        )

    lines.append(
        f"\n⚠️ <b>{next_str} 進場前需確認：</b>\n"
        "① 試撮量縮（低於上方門檻）\n"
        "② 漲不過觀察空點（+2.5%）\n"
        "③ 盤中量縮（低於試撮門檻）\n"
        "④ 趨勢線轉折後再空\n"
        "⑤ 全程當沖，收盤前了結\n\n"
        "🔔 開盤後每5分鐘盤中監控，符合條件 Telegram 即時提醒"
    )
    return "\n".join(lines)


def format_line_morning(candidates):
    """LINE 早上提醒格式（純文字，簡潔）"""
    last_day = get_last_trading_day()
    next_day = get_next_trading_day(last_day)
    next_str = next_day.strftime("%m/%d")

    if not candidates:
        return f"📊 {next_str} 今日無做空觀察標的"

    lines = [f"📊 {next_str} 做空觀察名單（開盤注意）\n"]
    for c in candidates:
        est_vol = max(1, int(c["vol"] * 0.02))
        lines.append(
            f"{'🔴' if '高度' in c['signal'] else '🟡'} {c['code']} {c['name']}\n"
            f"  昨收 {c['close']}元 漲{c['pct']}%\n"
            f"  空點：漲不過 {c['watch_line']} 元\n"
            f"  停損：過早盤高點\n"
            f"  目標：{c['target_ref']} 元\n"
            f"  試撮量 < {est_vol:,} 張"
        )

    lines.append("\n記得確認試撮量縮+趨勢線才進場")
    return "\n".join(lines)


def format_backtest(period_days, symbol_results, all_trades):
    lines = [f"📈 <b>回測報告（近 {period_days} 天）</b>"]
    note = "1分K" if period_days <= 7 else "1小時K"
    lines.append(f"開盤1小時判斷：{note}\n")
    if not all_trades:
        lines.append("此期間無符合條件的進場機會")
        return "\n".join(lines)
    total = len(all_trades)
    wins = sum(1 for t in all_trades if t["win"])
    hour_wins = sum(1 for t in all_trades if t.get("hit_in_hour"))
    win_rate = round(wins / total * 100, 1)
    hour_rate = round(hour_wins / total * 100, 1)
    avg_profit = round(sum(t["profit"] for t in all_trades) / total, 2)
    p_list = [t["profit"] for t in all_trades if t["win"]]
    l_list = [t["profit"] for t in all_trades if not t["win"]]
    avg_win = round(sum(p_list)/len(p_list), 2) if p_list else 0
    avg_loss = round(sum(l_list)/len(l_list), 2) if l_list else 0
    lines.append(
        f"📊 <b>整體統計</b>\n"
        f"  進場次數：{total} 次\n"
        f"  整體勝率：<b>{win_rate}%</b>（{wins}勝{total-wins}敗）\n"
        f"  🕙 1小時內達標：<b>{hour_wins}</b> 次（{hour_rate}%）\n"
        f"  平均獲利：<b>{avg_win:+.2f}%</b> | 平均虧損：<b>{avg_loss:+.2f}%</b>\n"
        f"  平均每筆：<b>{avg_profit:+.2f}%</b>\n"
    )
    good = [(c, r) for c, r in symbol_results.items() if r["total"] >= 2 and r["win_rate"] >= 50]
    good.sort(key=lambda x: (-x[1]["hour_win_rate"], -x[1]["avg_profit"]))
    if good:
        lines.append("🏆 <b>勝率 ≥50% 標的：</b>")
        for code, r in good[:5]:
            lines.append(
                f"  {code} {r['name']}：{r['win_rate']}% | "
                f"1小時{r['hour_win_rate']}%（{r['hour_wins']}/{r['total']}）"
                f" 均{r['avg_profit']:+.2f}%"
            )
    recent = sorted(all_trades, key=lambda x: x["date"], reverse=True)[:4]
    lines.append("\n📅 <b>最近進場記錄：</b>")
    for t in recent:
        icon = "✅" if t["win"] else "❌"
        hour_tag = " 🕙1小時達標" if t.get("hit_in_hour") else ""
        lines.append(
            f"  {icon} {t['date']}收{t['close']} +{t['pct']}%\n"
            f"     空點{t['watch_line']} 停損{t['stop']} 目標{t['target']}\n"
            f"     {t['next_date']} → {t['result']}{hour_tag} {t['profit']:+.2f}%"
        )
    lines.append("\n⚠️ 回測為理想當沖條件，實際須配合試撮量縮+趨勢確認")
    return "\n".join(lines)


def format_test():
    now = datetime.now(TW_TZ).strftime("%m/%d %H:%M")
    last_day = get_last_trading_day()
    next_day = get_next_trading_day(last_day)
    lines = [
        f"🔧 <b>系統測試 {now}</b>",
        f"收盤日：{last_day.strftime('%m/%d')} → 觀察日：{next_day.strftime('%m/%d')}\n"
    ]
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=2330.TW,2615.TW"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=8)
        quotes = r.json().get("quoteResponse", {}).get("result", []) or []
        lines.append(f"✅ Yahoo v7：{len(quotes)} 筆")
        if quotes:
            q = quotes[0]
            lines.append(f"   台積電：{q.get('regularMarketPrice')}元 ({round(q.get('regularMarketChangePercent',0),2):+.2f}%)")
    except Exception as e:
        lines.append(f"❌ Yahoo v7：{str(e)[:40]}")

    if FUGLE_TOKEN:
        q = fugle_quote("2330")
        if q:
            lines.append(f"✅ Fugle 即時：正常")
            lines.append(f"   台積電：{q['current']}元 ({q['pct']:+.2f}%) 高{q['day_high']}")
        else:
            lines.append("⚠️ Fugle：無資料（非交易時段正常）")
    else:
        lines.append("❌ Fugle：未設定 Token")

    lines.append(f"LINE 推播：{'✅' if LINE_TOKEN and LINE_USER_ID else '❌ 未設定'}")
    lines.append(f"盤中監控名單：{len(_watchlist_today)} 支")
    lines.append(f"今日已提醒：{len(_alerted_today)} 支")

    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        for c in candidates[:3]:
            lines.append(f"• {c['code']} {c['name']} {c['close']}元 +{c['pct']}% {c['vol']:,}張")
    else:
        lines.append("（目前無符合條件標的）")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 排程
# ──────────────────────────────────────────────

def job_evening_push():
    """13:40 收盤後推播到 Telegram（完整版 + 今日結算）"""
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return
    logger.info("Evening push")
    # 先推播收盤篩選（明日觀察名單）
    candidates = screen()
    tg_only(CHAT_ID, format_report(candidates))
    # 等5秒再推播今日結算
    time.sleep(5)
    tg_only(CHAT_ID, format_daily_summary())


def job_morning_line():
    """08:50 早上推播到 LINE（簡潔版，開盤前提醒）
    優先使用前一天 13:40 存好的觀察名單
    若無（例如伺服器重啟）則重新抓昨日收盤資料
    """
    global _watchlist_today
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return
    logger.info("Morning LINE push")
    if _watchlist_today:
        # 使用前一天 13:40 已存好的名單（最準確）
        logger.info(f"Morning push using cached watchlist: {len(_watchlist_today)} stocks")
        line_send(format_line_morning(_watchlist_today))
    else:
        # 伺服器重啟後名單遺失，重新抓昨日資料
        logger.info("Morning push: no cached watchlist, fetching fresh data")
        candidates = screen()
        if candidates:
            line_send(format_line_morning(candidates))
        else:
            line_send("📊 今日無做空觀察標的")


scheduler = BackgroundScheduler(timezone=TW_TZ)
# 13:40 收盤推播（Telegram）
scheduler.add_job(job_evening_push, "cron", day_of_week="mon-fri", hour=13, minute=40)
# 08:50 早上提醒（LINE）
scheduler.add_job(job_morning_line, "cron", day_of_week="mon-fri", hour=8, minute=50)
# 盤中監控 09:00~13:30 每5分鐘（Telegram）
scheduler.add_job(intraday_monitor, "cron",
                  day_of_week="mon-fri",
                  hour="9-13", minute="0,5,10,15,20,25,30,35,40,45,50,55")
scheduler.start()


# ──────────────────────────────────────────────
# Telegram Polling
# ──────────────────────────────────────────────

last_update_id = 0


def handle_update(update):
    global last_update_id
    update_id = update.get("update_id", 0)
    if update_id <= last_update_id:
        return
    last_update_id = update_id
    msg = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    if not chat_id or not text:
        return
    logger.info(f"Message: {text}")

    if text in ["/start", "/scan", "掃描", "篩選", "今天", "標的", "做空"]:
        tg_send(chat_id, "⏳ 篩選中，請稍候 15~25 秒...")
        candidates = screen()
        tg_only(chat_id, format_report(candidates))

    elif text in ["/test", "測試"]:
        tg_send(chat_id, "🔧 測試中，請稍候...")
        tg_only(chat_id, format_test())

    elif text in ["/bt7", "回測7"]:
        tg_send(chat_id, "📈 回測近7天（1分K），請稍候約45秒...")
        results, trades = run_backtest(7)
        tg_only(chat_id, format_backtest(7, results, trades))

    elif text in ["/bt15", "回測15"]:
        tg_send(chat_id, "📈 回測近15天（1小時K），請稍候約60秒...")
        results, trades = run_backtest(15)
        tg_only(chat_id, format_backtest(15, results, trades))

    elif text in ["/bt30", "回測30"]:
        tg_send(chat_id, "📈 回測近30天（1小時K），請稍候約90秒...")
        results, trades = run_backtest(30)
        tg_only(chat_id, format_backtest(30, results, trades))

    elif text == "/id":
        tg_send(chat_id, f"你的 Chat ID：\n<code>{chat_id}</code>")

    elif text in ["/help", "說明"]:
        tg_send(chat_id, (
            "📋 <b>指令說明</b>\n\n"
            "/scan — 篩選做空標的\n"
            "/bt7 — 回測近7天（1分K）\n"
            "/bt15 — 回測近15天（1小時K）\n"
            "/bt30 — 回測近30天（1小時K）\n"
            "/test — 系統測試\n"
            "/id — 查詢 Chat ID\n\n"
            "📅 推播時程：\n"
            "  08:50 LINE 早上觀察名單提醒\n"
            "  09:00~13:30 Telegram 盤中即時提醒\n"
            "  13:40 Telegram 收盤完整推播\n\n"
            "📊 篩選：漲幅 3~9.5% + 量能 + 股價 ≤1000元"
        ))
    else:
        tg_send(chat_id, "傳 /scan 篩選，/bt30 回測，/help 查看指令。")


def polling_loop():
    global last_update_id
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=35
            )
            for update in r.json().get("result", []):
                handle_update(update)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)


threading.Thread(target=polling_loop, daemon=True).start()


@app.route("/")
def index():
    return "📈 短空機器人運行中（TG + LINE + Fugle）"


@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(TW_TZ).isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
