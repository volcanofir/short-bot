import os
import json
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz
import logging
import csv
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
USER_ID = os.environ.get("LINE_USER_ID", "")
TW_TZ = pytz.timezone("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def get_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}"
    }


def reply(reply_token, messages):
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=get_headers(),
            json={"replyToken": reply_token, "messages": messages},
            timeout=10
        )
    except Exception as e:
        logger.error(f"Reply error: {e}")


def push(to, messages):
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=get_headers(),
            json={"to": to, "messages": messages},
            timeout=10
        )
    except Exception as e:
        logger.error(f"Push error: {e}")


def verify_signature(body, sig):
    digest = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == sig


# ──────────────────────────────────────────────
# 資料來源：Stooq（波蘭金融資料站，提供全球股市，無地區限制）
# 台股格式：代號.TW（上市）或 代號.TWO（上櫃）
# ──────────────────────────────────────────────

def fetch_stooq_index():
    """
    從 Stooq 抓取台灣上市股票清單當日行情
    使用 Taiwan 市場指數成分股 CSV 下載
    """
    results = []

    # Stooq 提供台灣市場所有股票的每日資料
    # 格式：https://stooq.com/db/l/?b=d&t=d&e=csv&i=m (台灣市場)
    urls = [
        "https://stooq.com/db/l/?b=d&t=d&e=csv&i=d",  # 所有市場當日資料
    ]

    # 改用直接查詢漲幅排行的方式
    # 用 stooq 的 top gainers 頁面
    try:
        # 抓台灣市場當日漲幅
        url = "https://stooq.com/t/?i=524"  # Taiwan stocks top gainers
        r = requests.get(url, timeout=15, headers={"User-Agent": UA})
        logger.info(f"Stooq status: {r.status_code}, len: {len(r.text)}")
    except Exception as e:
        logger.error(f"Stooq error: {e}")

    return results


def fetch_tw_market():
    """
    主要方案：用 Yahoo Finance 的市場摘要 API
    這個不需要 Screener，直接查台灣市場漲幅前排
    """
    results = []

    # 用 Yahoo Finance v8 chart API 查多支股票（最快）
    # 一次最多 1500 支
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com"
    }

    # 方案A：用 Yahoo Finance market summary（台灣）
    try:
        url = "https://query1.finance.yahoo.com/v6/finance/quote/marketSummary"
        params = {"lang": "zh-TW", "region": "TW", "corsDomain": "tw.finance.yahoo.com"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        logger.info(f"Yahoo market summary: {r.status_code}")
    except Exception as e:
        logger.error(f"Yahoo market summary: {e}")

    # 方案B：直接用 Yahoo Finance 的 spark API 批次查報價
    # 這是最快的批次查詢方式，一次可以查 1500 支
    SYMBOLS = [
        # 熱門上市（加 .TW）
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
        "2324.TW","2352.TW","2358.TW","2377.TW","2383.TW","2392.TW","2405.TW",
        "2498.TW","5871.TW","3481.TW","2393.TW","6147.TW","2363.TW","3045.TW",
        # 熱門上櫃（加 .TWO）
        "6789.TWO","6269.TWO","3622.TWO","6265.TWO","3583.TWO","4961.TWO",
        "6191.TWO","8112.TWO","6756.TWO","6679.TWO","6477.TWO","6446.TWO",
        "3653.TWO","3189.TWO","4763.TWO","5269.TWO","6227.TWO","3311.TWO",
    ]

    # 用 Yahoo v7 批次查（每批 100 支，夠快）
    batch_size = 100
    for i in range(0, len(SYMBOLS), batch_size):
        batch = SYMBOLS[i:i+batch_size]
        symbols_str = ",".join(batch)
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}"
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            quotes = r.json().get("quoteResponse", {}).get("result", []) or []
            logger.info(f"Yahoo v7 batch {i//batch_size+1}: {len(quotes)} quotes")

            for q in quotes:
                try:
                    symbol = str(q.get("symbol", ""))
                    code = symbol.replace(".TW", "").replace(".TWO", "")
                    name = q.get("shortName", "") or code
                    # 清理名稱（去掉多餘字）
                    name = name.replace(" Corp.", "").replace(" Co.,Ltd.", "").strip()
                    close = float(q.get("regularMarketPrice", 0) or 0)
                    pct = float(q.get("regularMarketChangePercent", 0) or 0)
                    vol = int(q.get("regularMarketVolume", 0) or 0) // 1000
                    mkt_state = q.get("marketState", "")

                    if close <= 0:
                        continue

                    market = "上市" if symbol.endswith(".TW") else "上櫃"

                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        results.append({
                            "market": market,
                            "code": code,
                            "name": name,
                            "close": round(close, 2),
                            "pct": round(pct, 2),
                            "vol": vol,
                            "signal": get_signal(pct, vol),
                            "state": mkt_state
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Yahoo v7 batch error: {e}")

    logger.info(f"fetch_tw_market results: {len(results)}")
    return results


# ──────────────────────────────────────────────
# 篩選
# ──────────────────────────────────────────────

def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    candidates = fetch_tw_market()
    seen = {}
    for c in candidates:
        code = c["code"]
        if code not in seen or c["vol"] > seen[code]["vol"]:
            seen[code] = c
    result = list(seen.values())
    result.sort(key=lambda x: x["vol"], reverse=True)
    return result[:10]


# ──────────────────────────────────────────────
# 訊息
# ──────────────────────────────────────────────

def build_messages(candidates):
    now = datetime.now(TW_TZ).strftime("%m/%d")
    if not candidates:
        return [{"type": "text", "text": (
            f"📊 {now} 收盤篩選完畢\n\n"
            "今日無符合條件的標的\n"
            "（量能不足或漲幅不符合策略門檻）\n\n"
            "傳「測試」可查看系統狀態"
        )}]

    bubbles = []
    for c in candidates:
        color = "#ff3b3b" if "高度" in c["signal"] else "#ffb800" if "值得" in c["signal"] else "#888888"
        est_vol = max(1, int(c["vol"] * 0.02))
        watch = f"• 試撮量低於 {est_vol:,} 張時注意\n• 漲不過 {round(c['close']*1.025, 1)} 元可考慮空\n• 停損設過早盤高點（1% 內）"
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#111318",
                "contents": [
                    {"type": "text", "text": f"{c['code']} {c['name']}", "color": "#ffffff", "size": "md", "weight": "bold"},
                    {"type": "text", "text": c["signal"], "color": color, "size": "xs", "margin": "xs"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "backgroundColor": "#1a1d24",
                "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "市場", "color": "#777", "size": "sm", "flex": 2},
                        {"type": "text", "text": c["market"], "color": "#e8eaf0", "size": "sm", "flex": 3, "weight": "bold"}]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "收盤價", "color": "#777", "size": "sm", "flex": 2},
                        {"type": "text", "text": str(c["close"]), "color": "#ffea00", "size": "sm", "flex": 3, "weight": "bold"}]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "漲幅", "color": "#777", "size": "sm", "flex": 2},
                        {"type": "text", "text": f"+{c['pct']}%", "color": "#ff3b3b", "size": "sm", "flex": 3, "weight": "bold"}]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "成交量", "color": "#777", "size": "sm", "flex": 2},
                        {"type": "text", "text": f"{c['vol']:,} 張", "color": "#e8eaf0", "size": "sm", "flex": 3}]},
                    {"type": "separator", "margin": "md", "color": "#252830"},
                    {"type": "text", "text": "明日觀察重點", "color": "#777", "size": "xs", "margin": "md"},
                    {"type": "text", "text": watch, "color": "#aaa", "size": "xs", "wrap": True, "margin": "xs"}
                ]
            }
        })

    return [{
        "type": "flex",
        "altText": f"📊 {now} 明日做空觀察清單（{len(candidates)} 支）",
        "contents": {"type": "carousel", "contents": bubbles}
    }]


def build_test_message():
    now = datetime.now(TW_TZ).strftime("%m/%d %H:%M")
    lines = [f"🔧 系統測試 {now}\n"]

    # 測試單支股票（2330台積電）
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=2330.TW,2317.TW,2454.TW"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=8)
        quotes = r.json().get("quoteResponse", {}).get("result", []) or []
        lines.append(f"✅ Yahoo v7 API 正常（{len(quotes)} 筆）")
        if quotes:
            q = quotes[0]
            price = q.get("regularMarketPrice", "?")
            pct = round(q.get("regularMarketChangePercent", 0), 2)
            state = q.get("marketState", "")
            lines.append(f"   台積電：{price}元 ({pct:+.2f}%) [{state}]")
    except Exception as e:
        lines.append(f"❌ Yahoo v7 失敗：{str(e)[:50]}")

    # 跑篩選
    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        for c in candidates[:3]:
            lines.append(f"• {c['code']} {c['name']} +{c['pct']}% {c['vol']:,}張")
    else:
        lines.append("（目前無符合條件標的）")
        lines.append("※ 非交易時段 Yahoo 不提供漲跌幅資料")
        lines.append("※ 請在收盤後 13:30~16:00 測試")

    return [{"type": "text", "text": "\n".join(lines)}]


# ──────────────────────────────────────────────
# 排程
# ──────────────────────────────────────────────

def push_report():
    if not USER_ID:
        return
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return
    logger.info(f"Pushing report at {now}")
    push(USER_ID, build_messages(screen()))


scheduler = BackgroundScheduler(timezone=TW_TZ)
scheduler.add_job(push_report, "cron", day_of_week="mon-fri", hour=15, minute=35)
scheduler.start()


# ──────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_signature(body, sig):
        abort(400)

    for event in json.loads(body).get("events", []):
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue
        text = event["message"]["text"].strip()
        rt = event["replyToken"]
        uid = event["source"]["userId"]

        if text in ["掃描", "篩選", "今天", "標的", "做空"]:
            reply(rt, [{"type": "text", "text": "⏳ 篩選中，請稍候 15~25 秒..."}])
            push(uid, build_messages(screen()))
        elif text == "測試":
            reply(rt, [{"type": "text", "text": "🔧 測試中，請稍候..."}])
            push(uid, build_test_message())
        elif text in ["說明", "help", "?"]:
            reply(rt, [{"type": "text", "text": (
                "📋 指令說明\n\n"
                "【篩選標的】掃描 / 篩選 / 今天 / 標的 / 做空\n"
                "【系統測試】測試\n"
                "【自動推播】每日 15:35\n\n"
                "篩選條件：漲幅 3~9.5% + 量能足夠\n"
                "最佳測試時間：收盤後 13:30~16:00"
            )}])
        else:
            reply(rt, [{"type": "text", "text": "傳「掃描」篩選標的，傳「測試」檢查系統，傳「說明」查看指令。"}])

    return "OK"


@app.route("/")
def index():
    return "📈 短空機器人運行中"


@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(TW_TZ).isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
