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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


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
# 資料抓取：Yahoo Finance Screener
# ──────────────────────────────────────────────

def fetch_yahoo_tw():
    """
    用 Yahoo Finance Screener 抓台灣股票漲幅 3~9.5% 的標的
    直接 POST 查詢，不受地區限制
    """
    results = []
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://finance.yahoo.com/screener/",
        "Origin": "https://finance.yahoo.com"
    }

    # Yahoo Finance Screener API
    url = "https://query1.finance.yahoo.com/v1/finance/screener"
    params = {
        "formatted": "false",
        "useRecords": "false",
        "lang": "en-US",
        "region": "TW"
    }
    payload = {
        "offset": 0,
        "size": 250,
        "sortField": "percentchange",
        "sortType": "DESC",
        "quoteType": "EQUITY",
        "topOperator": "AND",
        "query": {
            "operator": "AND",
            "operands": [
                {
                    "operator": "or",
                    "operands": [
                        {"operator": "EQ", "operands": ["region", "tw"]}
                    ]
                },
                {"operator": "GT", "operands": ["percentchange", 2.9]},
                {"operator": "LT", "operands": ["percentchange", 9.6]},
                {"operator": "GT", "operands": ["intradaymarketcap", 500000000]}
            ]
        }
    }

    try:
        r = requests.post(url, params=params, json=payload, headers=headers, timeout=20)
        logger.info(f"Yahoo screener status: {r.status_code}")
        data = r.json()
        quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        logger.info(f"Yahoo screener quotes: {len(quotes)}")

        for q in quotes:
            try:
                symbol = str(q.get("symbol", ""))
                code = symbol.replace(".TW", "").replace(".TWO", "")
                name = q.get("shortName", "") or q.get("longName", "") or code
                close = float(q.get("regularMarketPrice", 0))
                pct = float(q.get("regularMarketChangePercent", 0))
                vol_shares = int(q.get("regularMarketVolume", 0))
                vol = vol_shares // 1000  # 張
                market = "上市" if symbol.endswith(".TW") else "上櫃"

                # 過濾：只要普通股（4碼數字）
                if not code.isdigit() or len(code) != 4:
                    continue
                if close <= 0 or vol <= 0:
                    continue
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

    # 備用：用 predefined screener 抓多個 batch
    if not results:
        logger.info("Trying Yahoo predefined screener...")
        results = fetch_yahoo_predefined()

    return results


def fetch_yahoo_predefined():
    """備用：Yahoo 預設台股漲幅排行"""
    results = []
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://tw.stock.yahoo.com/"
    }
    # 台灣漲幅排行
    for scrId in ["tw_market_day_gainers"]:
        try:
            url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
            params = {
                "count": 100,
                "scrIds": scrId,
                "formatted": "false",
                "lang": "zh-TW",
                "region": "TW"
            }
            r = requests.get(url, params=params, headers=headers, timeout=15)
            quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
            logger.info(f"Yahoo predefined {scrId}: {len(quotes)}")
            for q in quotes:
                try:
                    symbol = str(q.get("symbol", ""))
                    code = symbol.replace(".TW", "").replace(".TWO", "")
                    if not code.isdigit() or len(code) != 4:
                        continue
                    name = q.get("shortName", code)
                    close = float(q.get("regularMarketPrice", 0))
                    pct = float(q.get("regularMarketChangePercent", 0))
                    vol = int(q.get("regularMarketVolume", 0)) // 1000
                    market = "上市" if ".TW" in symbol and not ".TWO" in symbol else "上櫃"
                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        results.append({
                            "market": market, "code": code, "name": name,
                            "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                            "signal": get_signal(pct, vol)
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Yahoo predefined error: {e}")
    return results


def fetch_finmind():
    """FinMind 備用（收盤後才有資料）"""
    today = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    results = []
    try:
        r = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockPrice", "start_date": today},
            timeout=20, headers={"User-Agent": UA}
        )
        rows = r.json().get("data", [])
        logger.info(f"FinMind rows: {len(rows)}")
        for row in rows:
            try:
                code = str(row.get("stock_id", "")).strip()
                if not code.isdigit() or len(code) != 4:
                    continue
                close = float(row.get("close", 0))
                spread = float(row.get("spread", 0))
                vol = int(row.get("Trading_Volume", 0)) // 1000
                prev = close - spread
                if prev <= 0 or close <= 0:
                    continue
                pct = spread / prev * 100
                if 3.0 <= pct <= 9.5 and vol >= 500:
                    results.append({
                        "market": "台股", "code": code, "name": code,
                        "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                        "signal": get_signal(pct, vol)
                    })
            except Exception:
                continue
    except Exception as e:
        logger.error(f"FinMind error: {e}")
    return results


# ──────────────────────────────────────────────
# 篩選
# ──────────────────────────────────────────────

def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    candidates = fetch_yahoo_tw()
    if not candidates:
        logger.info("Yahoo empty, trying FinMind...")
        candidates = fetch_finmind()
    candidates.sort(key=lambda x: x["vol"], reverse=True)
    return candidates[:10]


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

    # Yahoo screener
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener"
        params = {"formatted": "false", "lang": "en-US", "region": "TW"}
        payload = {
            "offset": 0, "size": 10,
            "sortField": "percentchange", "sortType": "DESC",
            "quoteType": "EQUITY", "topOperator": "AND",
            "query": {
                "operator": "AND",
                "operands": [
                    {"operator": "or", "operands": [{"operator": "EQ", "operands": ["region", "tw"]}]},
                    {"operator": "GT", "operands": ["percentchange", 2.9]}
                ]
            }
        }
        r = requests.post(url, params=params, json=payload,
                          headers={"User-Agent": UA, "Content-Type": "application/json"},
                          timeout=10)
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        lines.append(f"✅ Yahoo Screener 正常（{len(quotes)} 筆）")
        if quotes:
            q = quotes[0]
            lines.append(f"   範例：{q.get('symbol')} +{round(q.get('regularMarketChangePercent',0),1)}%")
    except Exception as e:
        lines.append(f"❌ Yahoo Screener 失敗：{str(e)[:50]}")

    # FinMind
    try:
        today = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        r = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockPrice", "start_date": today},
            timeout=10, headers={"User-Agent": UA}
        )
        rows = r.json().get("data", [])
        lines.append(f"✅ FinMind 正常（{len(rows)} 筆）")
    except Exception as e:
        lines.append(f"❌ FinMind 失敗：{str(e)[:50]}")

    # 篩選結果
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
