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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
USER_ID = os.environ.get("LINE_USER_ID", "")
TW_TZ = pytz.timezone("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ──────────────────────────────────────────────
# 台股監控清單（成交量大、當沖率高的熱門股）
# 涵蓋上市上櫃共約 300 支
# ──────────────────────────────────────────────
WATCH_LIST_TW = [
    # 半導體/電子
    "2330","2303","2317","2454","2382","3711","2308","2379","2301","2344",
    "2337","2360","3034","3036","2388","2376","6669","2347","2357","2395",
    "3008","3045","2449","2408","2353","2385","2323","2340","3041","2393",
    "2351","2369","6533","3529","6271","3231","2368","2409","6147","2363",
    # 金融
    "2882","2881","2886","2884","2891","2883","2880","2885","2887","2892",
    "2890","2889","2888","5880","2823","2836",
    # 傳產/其他大型
    "1301","1303","1326","2002","1402","2207","2201","1216","2912","9904",
    "1101","1102","2105","2603","2609","2615","2618","2610",
    # 中小型熱門/當沖
    "3023","6679","6477","3037","6446","5274","3705","4966","6515","3714",
    "8069","6770","6472","3596","6488","4919","3545","6510","3086","6531",
    "6278","5347","3035","6257","2421","4736","3443","4958","6670","6548",
    "3047","6EL8","6491","3661","3714","6257","4763","5269","3189","2399",
    "6116","6488","3374","2433","3680","6227","6592","4977","3653","6533",
    "8046","6415","3016","2429","6285","3694","2441","6456","6456","3311",
    "2913","3702","6239","2492","3013","4904","2474","6443","3533","6409",
    # 上櫃熱門
    "6789","6269","3622","6265","3583","4961","6191","6116","8112","6756",
    "6278","3680","6488","4977","3714","6257","6515","6770","3545","6679",
]

# 去重
WATCH_LIST_TW = list(dict.fromkeys(WATCH_LIST_TW))


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
# Yahoo Finance 批次報價 API
# ──────────────────────────────────────────────

def fetch_yahoo_quotes(symbols):
    """
    用 Yahoo Finance v7 quote API 批次查詢
    這個 endpoint 不受地區限制，海外 IP 完全可用
    """
    results = []
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com"
    }

    # 每次最多查 50 支，分批處理
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        # 加上 .TW 後綴
        tickers = ",".join([f"{s}.TW" for s in batch])
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={tickers}&fields=regularMarketPrice,regularMarketChangePercent,regularMarketVolume,shortName,regularMarketChange,regularMarketPreviousClose"

        try:
            r = requests.get(url, headers=headers, timeout=15)
            data = r.json()
            quotes = data.get("quoteResponse", {}).get("result", [])

            for q in quotes:
                try:
                    symbol = q.get("symbol", "")
                    code = symbol.replace(".TW", "").replace(".TWO", "")
                    name = q.get("shortName", "") or code
                    close = float(q.get("regularMarketPrice", 0))
                    pct = float(q.get("regularMarketChangePercent", 0))
                    vol = int(q.get("regularMarketVolume", 0)) // 1000

                    if close <= 0 or vol <= 0:
                        continue
                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        market = "上市" if symbol.endswith(".TW") else "上櫃"
                        results.append({
                            "market": market,
                            "code": code,
                            "name": name,
                            "close": round(close, 2),
                            "pct": round(pct, 2),
                            "vol": vol,
                            "signal": get_signal(pct, vol)
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Yahoo quote batch error: {e}")

    # 也查一批上櫃（.TWO）
    for i in range(0, min(len(symbols), 100), batch_size):
        batch = symbols[i:i+batch_size]
        tickers = ",".join([f"{s}.TWO" for s in batch])
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={tickers}&fields=regularMarketPrice,regularMarketChangePercent,regularMarketVolume,shortName"

        try:
            r = requests.get(url, headers=headers, timeout=15)
            data = r.json()
            quotes = data.get("quoteResponse", {}).get("result", [])
            for q in quotes:
                try:
                    symbol = q.get("symbol", "")
                    if not symbol:
                        continue
                    code = symbol.replace(".TWO", "").replace(".TW", "")
                    name = q.get("shortName", "") or code
                    close = float(q.get("regularMarketPrice", 0))
                    pct = float(q.get("regularMarketChangePercent", 0))
                    vol = int(q.get("regularMarketVolume", 0)) // 1000
                    if close <= 0 or vol <= 0:
                        continue
                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        results.append({
                            "market": "上櫃",
                            "code": code,
                            "name": name,
                            "close": round(close, 2),
                            "pct": round(pct, 2),
                            "vol": vol,
                            "signal": get_signal(pct, vol)
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Yahoo TWO batch error: {e}")

    return results


# ──────────────────────────────────────────────
# 篩選
# ──────────────────────────────────────────────

def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    candidates = fetch_yahoo_quotes(WATCH_LIST_TW)
    # 去重（同代號可能上市上櫃都查到）
    seen = set()
    unique = []
    for c in candidates:
        if c["code"] not in seen:
            seen.add(c["code"])
            unique.append(c)
    unique.sort(key=lambda x: x["vol"], reverse=True)
    logger.info(f"Screen results: {len(unique)}")
    return unique[:10]


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
            "傳「測試」可查看 API 狀態"
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

    # 測試 Yahoo v7 quote API
    try:
        test_symbols = "2330.TW,2317.TW,2454.TW"
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={test_symbols}"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        lines.append(f"✅ Yahoo v7 Quote API 正常（{len(quotes)} 筆）")
        if quotes:
            q = quotes[0]
            pct = round(q.get("regularMarketChangePercent", 0), 2)
            price = q.get("regularMarketPrice", 0)
            lines.append(f"   {q.get('symbol')}: {price} ({pct:+.2f}%)")
    except Exception as e:
        lines.append(f"❌ Yahoo v7 失敗：{str(e)[:50]}")

    # 跑一次篩選
    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        for c in candidates[:3]:
            lines.append(f"• {c['code']} {c['name']} +{c['pct']}% {c['vol']:,}張 {c['signal']}")
    else:
        lines.append("（目前無符合條件標的）")
        lines.append("\n監控清單共 " + str(len(WATCH_LIST_TW)) + " 支")

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
            reply(rt, [{"type": "text", "text": "⏳ 篩選中，請稍候 15~30 秒..."}])
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
                f"監控清單：{len(WATCH_LIST_TW)} 支熱門股"
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
