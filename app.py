import os
import json
import hmac
import hashlib
import base64
import requests
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
# 資料抓取：FinMind（台灣開源金融平台，海外可存取）
# ──────────────────────────────────────────────

def get_today_str():
    now = datetime.now(TW_TZ)
    return now.strftime("%Y-%m-%d")


def fetch_finmind():
    """
    使用 FinMind 抓取台股當日收盤資料
    API 文件：https://finmindtrade.com/analysis/#/data/document
    不需要帳號即可使用（有頻率限制）
    """
    today = get_today_str()
    url = "https://api.finmindtrade.com/api/v4/data"
    results = []

    # 抓上市
    try:
        params = {
            "dataset": "TaiwanStockPrice",
            "start_date": today,
            "token": ""  # 免費版不需要 token
        }
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": UA})
        data = r.json()
        rows = data.get("data", [])
        logger.info(f"FinMind TaiwanStockPrice: {len(rows)} rows")

        for row in rows:
            try:
                code = str(row.get("stock_id", "")).strip()
                close = float(row.get("close", 0))
                open_p = float(row.get("open", 0))
                prev_close = float(row.get("close", 0)) - float(row.get("spread", 0))
                spread = float(row.get("spread", 0))
                vol = int(row.get("Trading_Volume", 0)) // 1000

                if prev_close <= 0 or close <= 0:
                    continue
                pct = spread / prev_close * 100

                # 排除ETF（代號長度>4或含英文字母）
                if len(code) > 4 or not code.isdigit():
                    continue

                if 3.0 <= pct <= 9.5 and vol >= 1000:
                    results.append({
                        "market": "上市", "code": code, "name": code,
                        "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                        "signal": get_signal(pct, vol)
                    })
            except Exception:
                continue

    except Exception as e:
        logger.error(f"FinMind fetch error: {e}")

    return results


def fetch_yahoo_movers():
    """
    使用 Yahoo Finance Taiwan 漲幅排行
    這個 API 在海外完全可存取
    """
    results = []
    today = get_today_str()

    # Yahoo Finance Taiwan 漲幅排行 API
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    params = {
        "formatted": "false",
        "lang": "zh-TW",
        "region": "TW",
        "scrIds": "day_gainers",
        "count": 100,
        "start": 0
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://tw.stock.yahoo.com/"
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        logger.info(f"Yahoo movers: {len(quotes)} stocks")

        for q in quotes:
            try:
                symbol = q.get("symbol", "")
                # 只要台灣股票（.TW 或 .TWO）
                if not symbol.endswith(".TW") and not symbol.endswith(".TWO"):
                    continue

                code = symbol.replace(".TW", "").replace(".TWO", "")
                name = q.get("shortName", "") or q.get("longName", code)
                close = float(q.get("regularMarketPrice", 0))
                pct = float(q.get("regularMarketChangePercent", 0))
                vol = int(q.get("regularMarketVolume", 0)) // 1000
                market = "上市" if symbol.endswith(".TW") else "上櫃"

                if 3.0 <= pct <= 9.5 and vol >= 500:
                    results.append({
                        "market": market, "code": code, "name": name,
                        "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                        "signal": get_signal(pct, vol)
                    })
            except Exception:
                continue

    except Exception as e:
        logger.error(f"Yahoo movers error: {e}")

    # 備用：Yahoo Finance 台灣漲幅排行（另一個端點）
    if not results:
        try:
            url2 = "https://query2.finance.yahoo.com/v1/finance/screener"
            payload = {
                "offset": 0,
                "size": 100,
                "sortField": "percentchange",
                "sortType": "DESC",
                "quoteType": "EQUITY",
                "topOperator": "AND",
                "query": {
                    "operator": "AND",
                    "operands": [
                        {"operator": "EQ", "operands": ["region", "tw"]},
                        {"operator": "GT", "operands": ["percentchange", 3]},
                        {"operator": "LT", "operands": ["percentchange", 10]},
                        {"operator": "GT", "operands": ["dayvolume", 500000]}
                    ]
                }
            }
            r = requests.post(url2, json=payload, headers=headers, timeout=15)
            data = r.json()
            quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
            logger.info(f"Yahoo screener: {len(quotes)} stocks")

            for q in quotes:
                try:
                    symbol = q.get("symbol", "")
                    code = symbol.replace(".TW", "").replace(".TWO", "")
                    name = q.get("shortName", code)
                    close = float(q.get("regularMarketPrice", 0))
                    pct = float(q.get("regularMarketChangePercent", 0))
                    vol = int(q.get("regularMarketVolume", 0)) // 1000
                    market = "上市" if ".TW" in symbol else "上櫃"

                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        results.append({
                            "market": market, "code": code, "name": name,
                            "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                            "signal": get_signal(pct, vol)
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Yahoo screener error: {e}")

    return results


# ──────────────────────────────────────────────
# 主篩選
# ──────────────────────────────────────────────

def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    # 優先用 Yahoo Finance（最穩定）
    candidates = fetch_yahoo_movers()

    # 如果 Yahoo 沒資料，試 FinMind
    if not candidates:
        logger.info("Yahoo empty, trying FinMind...")
        candidates = fetch_finmind()

    candidates.sort(key=lambda x: x["vol"], reverse=True)
    return candidates[:10]


# ──────────────────────────────────────────────
# 訊息組裝
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

    # 測試 Yahoo Finance
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        params = {"formatted": "false", "lang": "zh-TW", "region": "TW",
                  "scrIds": "day_gainers", "count": 10}
        r = requests.get(url, params=params, timeout=10,
                         headers={"User-Agent": UA})
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        lines.append(f"✅ Yahoo Finance 正常（{len(quotes)} 筆）")
    except Exception as e:
        lines.append(f"❌ Yahoo Finance 失敗：{str(e)[:50]}")

    # 測試 FinMind
    try:
        today = get_today_str()
        r = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockPrice", "start_date": today},
            timeout=10, headers={"User-Agent": UA}
        )
        rows = r.json().get("data", [])
        lines.append(f"✅ FinMind 正常（{len(rows)} 筆）")
    except Exception as e:
        lines.append(f"❌ FinMind 失敗：{str(e)[:50]}")

    # 執行篩選
    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        t = candidates[0]
        lines.append(f"最大量：{t['code']} {t['name']}")
        lines.append(f"+{t['pct']}% | {t['vol']:,} 張 | {t['signal']}")
    else:
        lines.append("（目前無符合條件標的）")

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
            reply(rt, [{"type": "text", "text": "⏳ 篩選中，請稍候 10~20 秒..."}])
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
                "篩選條件：漲幅 3~9.5% + 量能足夠"
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
