import os
import json
import hmac
import hashlib
import base64
import requests
import yfinance as yf
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
USER_ID = os.environ.get("LINE_USER_ID", "")
TW_TZ = pytz.timezone("Asia/Taipei")

# 台股熱門監控清單（當沖率高、成交量大）
WATCH_LIST = [
    "2330","2303","2317","2454","2382","3711","2308","2379","2301","2344",
    "2337","2360","3034","3036","2388","2376","6669","2347","2357","2395",
    "3008","3045","2449","2408","2353","2385","2323","2340","3041","2393",
    "2351","2369","6533","3529","6271","3231","2368","2409","6147","2363",
    "2882","2881","2886","2884","2891","2883","2880","2885","2887","2892",
    "1301","1303","1326","2002","1402","2207","2201","1216","2912","9904",
    "1101","1102","2105","2603","2609","2615","2618","2610",
    "3023","6679","6477","3037","6446","5274","3705","4966","6515","3714",
    "8069","6770","6472","3596","6488","4919","3545","6510","3086","6531",
    "6278","5347","3035","6257","2421","4736","3443","4958","6670","6548",
    "3047","6491","3661","4763","5269","3189","2399","6116","3374","2433",
    "3680","6227","6592","4977","3653","8046","6415","3016","2429","6285",
    "3694","2441","6456","3311","2913","3702","6239","2492","3013","4904",
    "2474","6443","3533","6409","6789","6269","3622","6265","3583","4961",
    "6191","8112","6756","2330","6271","3481","5871","2412","4938","3006",
    "2327","2356","2324","2352","2358","2377","2383","2392","2405","2415",
    "2498","2520","2542","2548","2610","2634","2637","2641","2642","2645",
]
WATCH_LIST = list(dict.fromkeys(WATCH_LIST))  # 去重


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
# 資料抓取：yfinance（最穩定）
# ──────────────────────────────────────────────

def fetch_tw_stocks():
    """
    用 yfinance 批次抓取台股當日收盤資料
    收盤後幾分鐘即可取得，海外 IP 完全支援
    """
    results = []
    now = datetime.now(TW_TZ)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=3)).strftime("%Y-%m-%d")  # 往前3天確保有資料

    # 分批查詢，每批 50 支
    batch_size = 50
    for i in range(0, len(WATCH_LIST), batch_size):
        batch = WATCH_LIST[i:i+batch_size]
        # yfinance 台股代號格式：XXXX.TW 或 XXXX.TWO
        tickers_tw = [f"{c}.TW" for c in batch]
        tickers_two = [f"{c}.TWO" for c in batch]

        for tickers, market in [(tickers_tw, "上市"), (tickers_two, "上櫃")]:
            try:
                symbols_str = " ".join(tickers)
                data = yf.download(
                    tickers=symbols_str,
                    start=yesterday,
                    end=(now + timedelta(days=1)).strftime("%Y-%m-%d"),
                    progress=False,
                    auto_adjust=True,
                    threads=True
                )

                if data.empty:
                    continue

                # 取最後兩天資料計算漲跌
                closes = data["Close"]
                volumes = data["Volume"]

                if closes.empty:
                    continue

                # 確保是 DataFrame
                if hasattr(closes, "iloc"):
                    for ticker in tickers:
                        try:
                            if ticker not in closes.columns:
                                continue
                            col = closes[ticker].dropna()
                            vol_col = volumes[ticker].dropna()
                            if len(col) < 2:
                                continue

                            close_today = float(col.iloc[-1])
                            close_prev = float(col.iloc[-2])
                            vol_today = int(vol_col.iloc[-1]) // 1000

                            if close_prev <= 0 or close_today <= 0:
                                continue

                            pct = (close_today - close_prev) / close_prev * 100

                            code = ticker.replace(".TW", "").replace(".TWO", "")

                            if 3.0 <= pct <= 9.5 and vol_today >= 500:
                                results.append({
                                    "market": market,
                                    "code": code,
                                    "name": code,  # yfinance 不一定有中文名
                                    "close": round(close_today, 2),
                                    "pct": round(pct, 2),
                                    "vol": vol_today,
                                    "signal": get_signal(pct, vol_today)
                                })
                        except Exception:
                            continue

            except Exception as e:
                logger.error(f"yfinance batch error ({market}): {e}")
                continue

    logger.info(f"yfinance results: {len(results)}")
    return results


def get_stock_name_yahoo(code):
    """用 Yahoo v7 API 查股票中文名"""
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={code}.TW"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        q = r.json().get("quoteResponse", {}).get("result", [])
        if q:
            return q[0].get("shortName", code)
    except Exception:
        pass
    return code


# ──────────────────────────────────────────────
# 篩選
# ──────────────────────────────────────────────

def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    candidates = fetch_tw_stocks()

    # 去重（同代號取成交量較大的）
    seen = {}
    for c in candidates:
        code = c["code"]
        if code not in seen or c["vol"] > seen[code]["vol"]:
            seen[code] = c

    result = list(seen.values())
    result.sort(key=lambda x: x["vol"], reverse=True)

    # 補查前10支的中文名稱
    top = result[:10]
    for item in top:
        if item["name"] == item["code"]:
            item["name"] = get_stock_name_yahoo(item["code"])

    return top


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

    # 測試 yfinance 單支股票
    try:
        ticker = yf.Ticker("2330.TW")
        hist = ticker.history(period="2d")
        if not hist.empty:
            close = round(float(hist["Close"].iloc[-1]), 2)
            lines.append(f"✅ yfinance 正常")
            lines.append(f"   台積電(2330) 最近收盤：{close}")
        else:
            lines.append("⚠️ yfinance 連線正常但無資料（非交易日？）")
    except Exception as e:
        lines.append(f"❌ yfinance 失敗：{str(e)[:50]}")

    # 跑篩選
    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        for c in candidates[:3]:
            lines.append(f"• {c['code']} {c['name']} +{c['pct']}% {c['vol']:,}張")
    else:
        lines.append("（目前無符合條件標的）")
        lines.append(f"監控清單：{len(WATCH_LIST)} 支")

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
            reply(rt, [{"type": "text", "text": "⏳ 篩選中，請稍候 20~40 秒..."}])
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
                f"監控清單：{len(WATCH_LIST)} 支熱門股"
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
