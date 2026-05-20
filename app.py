import os
import json
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
TW_TZ = pytz.timezone("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

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
    "2492": "華新科", "3013": "映泰", "2474": "可成", "6443": "元晶",
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
# Telegram 發送
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


# ──────────────────────────────────────────────
# 進場參考計算
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
# 資料抓取
# ──────────────────────────────────────────────

def get_last_trading_day():
    now = datetime.now(TW_TZ)
    d = now.date()
    if now.hour < 14:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


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
        closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        volumes = chart.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
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
        return {"close": round(last_close, 2), "pct": round(pct, 2),
                "vol": vol, "all": valid}
    except Exception:
        return None


def fetch_all():
    results = []
    headers = {"User-Agent": UA, "Accept": "application/json"}
    batch_size = 100

    for i in range(0, len(SYMBOLS), batch_size):
        batch = SYMBOLS[i:i+batch_size]
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
                    if close <= 0 or prev <= 0:
                        continue
                    pct = chg / prev * 100
                    market = "上市" if symbol.endswith(".TW") else "上櫃"
                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        trade = calc_trade(close)
                        results.append({
                            "market": market, "code": code, "name": name,
                            "close": round(close, 2), "pct": round(pct, 2),
                            "vol": vol, "signal": get_signal(pct, vol), **trade
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"v7 error: {e}")

    if not results:
        logger.info("v7 empty, using v8 history...")
        for symbol in SYMBOLS[:100]:
            data = fetch_stock_history(symbol)
            if data and 3.0 <= data["pct"] <= 9.5 and data["vol"] >= 500:
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


def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


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
# 回測功能
# ──────────────────────────────────────────────

def backtest_symbol(symbol, period_days):
    """
    回測單支股票：
    找出 period_days 內符合篩選條件的日子（當日漲 3~9.5%）
    檢查隔日收盤是否比當日收盤低（做空有獲利）
    計算平均報酬率
    """
    range_map = {7: "1mo", 15: "1mo", 30: "3mo"}
    range_str = range_map.get(period_days, "1mo")

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": range_str, "includePrePost": "false"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        chart = result[0]
        closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        volumes = chart.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
        timestamps = chart.get("timestamp", [])

        valid = [(datetime.fromtimestamp(t, TW_TZ).date(), c, v)
                 for t, c, v in zip(timestamps, closes, volumes)
                 if c is not None and v is not None]

        if len(valid) < 3:
            return []

        # 只看 period_days 內的資料
        cutoff = (datetime.now(TW_TZ) - timedelta(days=period_days)).date()
        valid = [x for x in valid if x[0] >= cutoff]

        trades = []
        for i in range(1, len(valid) - 1):
            date, close, vol = valid[i]
            prev_close = valid[i-1][1]
            next_close = valid[i+1][1]

            if prev_close <= 0 or close <= 0:
                continue

            pct = (close - prev_close) / prev_close * 100
            vol_k = int(vol) // 1000

            # 符合做空條件：當日漲 3~9.5%，量足夠
            if 3.0 <= pct <= 9.5 and vol_k >= 500:
                # 隔日收盤報酬（做空：收跌才賺）
                next_pct = (next_close - close) / close * 100
                profit = -next_pct  # 做空時，跌 = 賺
                trades.append({
                    "date": date.strftime("%m/%d"),
                    "close": round(close, 2),
                    "pct": round(pct, 2),
                    "next_close": round(next_close, 2),
                    "profit": round(profit, 2),
                    "win": profit > 0
                })

        return trades
    except Exception as e:
        logger.error(f"Backtest error {symbol}: {e}")
        return []


def run_backtest(period_days):
    """對監控清單中的熱門股做回測"""
    tg_send(CHAT_ID or "", f"🔄 回測中（近{period_days}天），請稍候約30秒...")

    # 只回測前30支（避免太慢）
    test_symbols = SYMBOLS[:30]
    all_trades = []
    symbol_results = {}

    for symbol in test_symbols:
        trades = backtest_symbol(symbol, period_days)
        if trades:
            code = symbol.replace(".TW", "").replace(".TWO", "")
            name = STOCK_NAMES.get(code, code)
            wins = sum(1 for t in trades if t["win"])
            total = len(trades)
            avg_profit = sum(t["profit"] for t in trades) / total
            symbol_results[code] = {
                "name": name, "total": total, "wins": wins,
                "win_rate": round(wins/total*100, 1),
                "avg_profit": round(avg_profit, 2),
                "trades": trades
            }
            all_trades.extend(trades)
        time.sleep(0.1)

    return symbol_results, all_trades


def format_backtest(period_days, symbol_results, all_trades):
    lines = [f"📈 <b>回測報告（近 {period_days} 天）</b>\n"]

    if not all_trades:
        lines.append("此期間無符合條件的交易機會")
        return "\n".join(lines)

    # 整體統計
    total = len(all_trades)
    wins = sum(1 for t in all_trades if t["win"])
    win_rate = round(wins / total * 100, 1)
    avg_profit = round(sum(t["profit"] for t in all_trades) / total, 2)
    total_profit = round(sum(t["profit"] for t in all_trades), 2)

    lines.append(
        f"📊 <b>整體統計</b>\n"
        f"  交易次數：{total} 次\n"
        f"  勝率：<b>{win_rate}%</b>（{wins}勝{total-wins}敗）\n"
        f"  平均報酬：<b>{avg_profit:+.2f}%</b>\n"
        f"  累積報酬：<b>{total_profit:+.2f}%</b>\n"
    )

    # 各股表現（只顯示有超過2次交易的）
    good = [(code, r) for code, r in symbol_results.items()
            if r["total"] >= 2 and r["win_rate"] >= 50]
    good.sort(key=lambda x: x[1]["win_rate"], reverse=True)

    if good:
        lines.append("🏆 <b>勝率較高的標的：</b>")
        for code, r in good[:5]:
            lines.append(
                f"  {code} {r['name']}：{r['win_rate']}% "
                f"（{r['wins']}/{r['total']}）平均 {r['avg_profit']:+.2f}%"
            )

    # 最近幾筆交易
    recent = sorted(all_trades, key=lambda x: x["date"], reverse=True)[:5]
    lines.append("\n📅 <b>最近交易記錄：</b>")
    for t in recent:
        icon = "✅" if t["win"] else "❌"
        lines.append(
            f"  {icon} {t['date']} 收{t['close']} +{t['pct']}% "
            f"→ 隔日{t['next_close']} ({t['profit']:+.2f}%)"
        )

    lines.append(
        "\n⚠️ 回測為理想條件下的隔日收盤計算\n"
        "實際操作須配合試撮量縮+趨勢確認"
    )
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 訊息格式
# ──────────────────────────────────────────────

def format_report(candidates):
    last_day = get_last_trading_day()
    date_str = last_day.strftime("%m/%d")

    if not candidates:
        return (
            f"📊 <b>{date_str} 收盤篩選完畢</b>\n\n"
            "今日無符合條件的標的\n"
            "（量能不足或漲幅不符合策略門檻）"
        )

    lines = [f"📊 <b>{date_str} 做空觀察清單（{len(candidates)} 支）</b>\n"]
    for c in candidates:
        est_vol = max(1, int(c["vol"] * 0.02))
        lines.append(
            f"{c['signal']} <b>{c['code']} {c['name']}</b> [{c['market']}]\n"
            f"  昨收：<b>{c['close']}</b>元  漲幅：<b>+{c['pct']}%</b>  量：<b>{c['vol']:,}</b>張\n"
            f"  ━━━━━━━━━━━━\n"
            f"  👁 觀察空點：漲不過 <b>{c['watch_line']}</b> 元（+2.5%）才考慮空\n"
            f"  🛑 停損參考：<b>{c['stop_ref']}</b> 元（+{c['stop_pct']}%，實際以早盤高點為準）\n"
            f"  💰 目標參考：<b>{c['target_ref']}</b> 元（-{c['target_pct']}%，賺賠比 2:1）\n"
            f"  📌 試撮量需低於 {est_vol:,} 張"
        )

    lines.append(
        "\n⚠️ <b>進場前需確認：</b>\n"
        "① 試撮量縮（低於上方門檻）\n"
        "② 漲不過 2.5% 門檻\n"
        "③ 趨勢線形成後再空"
    )
    return "\n".join(lines)


def format_test():
    now = datetime.now(TW_TZ).strftime("%m/%d %H:%M")
    last_day = get_last_trading_day()
    lines = [f"🔧 <b>系統測試 {now}</b>", f"最近交易日：{last_day.strftime('%m/%d')}\n"]

    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=2330.TW,2317.TW"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=8)
        quotes = r.json().get("quoteResponse", {}).get("result", []) or []
        lines.append(f"✅ Yahoo v7：{len(quotes)} 筆")
        if quotes:
            q = quotes[0]
            lines.append(f"   台積電：{q.get('regularMarketPrice')}元 ({round(q.get('regularMarketChangePercent',0),2):+.2f}%)")
    except Exception as e:
        lines.append(f"❌ v7 失敗：{str(e)[:40]}")

    try:
        data = fetch_stock_history("2330.TW")
        if data:
            lines.append(f"✅ Yahoo v8 歷史：正常")
            lines.append(f"   台積電最後收盤：{data['close']} ({data['pct']:+.2f}%) {data['vol']:,}張")
        else:
            lines.append("⚠️ v8 歷史：無資料")
    except Exception as e:
        lines.append(f"❌ v8 失敗：{str(e)[:40]}")

    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        for c in candidates[:3]:
            lines.append(f"• {c['code']} {c['name']} +{c['pct']}% {c['vol']:,}張")
            lines.append(f"  觀察:{c['watch_line']} 停損參考:{c['stop_ref']} 目標參考:{c['target_ref']}")
    else:
        lines.append("（目前無符合條件標的）")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 排程
# ──────────────────────────────────────────────

def push_report():
    if not CHAT_ID:
        return
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return
    logger.info(f"Auto push at {now}")
    tg_send(CHAT_ID, format_report(screen()))


scheduler = BackgroundScheduler(timezone=TW_TZ)
scheduler.add_job(push_report, "cron", day_of_week="mon-fri", hour=13, minute=35)
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
        tg_send(chat_id, format_report(screen()))

    elif text in ["/test", "測試"]:
        tg_send(chat_id, "🔧 測試中，請稍候...")
        tg_send(chat_id, format_test())

    elif text in ["/bt7", "回測7", "回測一週"]:
        tg_send(chat_id, "📈 回測近7天，請稍候...")
        results, trades = run_backtest(7)
        tg_send(chat_id, format_backtest(7, results, trades))

    elif text in ["/bt15", "回測15", "回測半個月"]:
        tg_send(chat_id, "📈 回測近15天，請稍候...")
        results, trades = run_backtest(15)
        tg_send(chat_id, format_backtest(15, results, trades))

    elif text in ["/bt30", "回測30", "回測一個月"]:
        tg_send(chat_id, "📈 回測近30天，請稍候...")
        results, trades = run_backtest(30)
        tg_send(chat_id, format_backtest(30, results, trades))

    elif text in ["/id"]:
        tg_send(chat_id, f"你的 Chat ID：\n<code>{chat_id}</code>")

    elif text in ["/help", "說明"]:
        tg_send(chat_id, (
            "📋 <b>指令說明</b>\n\n"
            "/scan — 篩選今日做空標的\n"
            "/bt7 — 回測近7天績效\n"
            "/bt15 — 回測近15天績效\n"
            "/bt30 — 回測近30天績效\n"
            "/test — 系統測試\n"
            "/id — 查詢 Chat ID\n"
            "/help — 說明\n\n"
            "⏰ 每日 13:35 自動推播\n"
            "💡 非交易時段自動顯示昨日資料"
        ))

    else:
        tg_send(chat_id, "傳 /scan 篩選標的，/bt30 回測績效，/help 查看全部指令。")


def polling_loop():
    global last_update_id
    logger.info("Polling started")
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
    return "📈 短空 Telegram 機器人運行中"


@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(TW_TZ).isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
