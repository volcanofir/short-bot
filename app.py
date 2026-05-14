import os
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz
import logging
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
TW_TZ = pytz.timezone("Asia/Taipei")


# ──────────────────────────────────────────────
# 資料抓取
# ──────────────────────────────────────────────

def fetch_twse_data():
    today = datetime.now(TW_TZ).strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={today}&type=ALLBUT0999"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        for table in data.get("tables", []):
            fields = table.get("fields", [])
            if "漲跌價差" in fields and "成交股數" in fields:
                return table.get("data", []), fields
    except Exception as e:
        logger.error(f"TWSE error: {e}")
    return [], []


def fetch_tpex_data():
    today = datetime.now(TW_TZ)
    roc_date = f"{today.year - 1911}/{today.month:02d}/{today.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=AL"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        return r.json().get("aaData", [])
    except Exception as e:
        logger.error(f"TPEX error: {e}")
    return []


# ──────────────────────────────────────────────
# 篩選邏輯
# ──────────────────────────────────────────────

def screen_candidates():
    candidates = []

    rows, fields = fetch_twse_data()
    if rows and fields:
        fi = {f: i for i, f in enumerate(fields)}
        for row in rows:
            try:
                code = row[fi.get("證券代號", 0)].strip()
                name = row[fi.get("證券名稱", 1)].strip()
                vol = int(row[fi.get("成交股數", 2)].replace(",", "")) // 1000
                close = float(row[fi.get("收盤價", -1)].replace(",", ""))
                change = float(row[fi.get("漲跌價差", -1)].replace(",", "").replace("+", ""))
                prev = close - change
                if prev <= 0:
                    continue
                pct = change / prev * 100
                if 3.0 <= pct <= 9.5 and vol >= 5000:
                    candidates.append({
                        "market": "上市", "code": code, "name": name,
                        "close": close, "pct": round(pct, 2), "vol": vol,
                        "signal": get_signal(pct, vol)
                    })
            except Exception:
                continue

    for row in fetch_tpex_data():
        try:
            code = str(row[0]).strip()
            name = str(row[1]).strip()
            close = float(str(row[2]).replace(",", ""))
            change = float(str(row[3]).replace(",", "").replace("+", ""))
            vol = int(str(row[7]).replace(",", "")) // 1000
            prev = close - change
            if prev <= 0:
                continue
            pct = change / prev * 100
            if 3.0 <= pct <= 9.5 and vol >= 2000:
                candidates.append({
                    "market": "上櫃", "code": code, "name": name,
                    "close": close, "pct": round(pct, 2), "vol": vol,
                    "signal": get_signal(pct, vol)
                })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["vol"], reverse=True)
    return candidates[:10]


def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 20000 else 1 if vol >= 10000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


# ──────────────────────────────────────────────
# 訊息組裝
# ──────────────────────────────────────────────

def build_message(candidates):
    now = datetime.now(TW_TZ).strftime("%m/%d")

    if not candidates:
        return TextMessage(text=f"📊 {now} 收盤篩選完畢\n\n今日無符合條件的標的\n（量能不足或漲幅不符合策略門檻）")

    bubbles = []
    for c in candidates:
        color = "#ff3b3b" if "高度" in c["signal"] else "#ffb800" if "值得" in c["signal"] else "#888888"
        watch = f"• 試撮量低於 {int(c['vol']*0.02):,} 張時注意\n• 漲不過 {round(c['close']*1.025,1)} 元可考慮空\n• 停損設過早盤高點（控制 1% 內）"

        bubble = {
            "type": "bubble", "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#111318",
                "contents": [
                    {"type": "text", "text": f"{c['code']} {c['name']}", "color": "#ffffff", "size": "md", "weight": "bold"},
                    {"type": "text", "text": c["signal"], "color": color, "size": "xs", "margin": "xs"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#1a1d24",
                "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "市場", "color": "#777777", "size": "sm", "flex": 2},
                        {"type": "text", "text": c["market"], "color": "#e8eaf0", "size": "sm", "flex": 3, "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "收盤價", "color": "#777777", "size": "sm", "flex": 2},
                        {"type": "text", "text": str(c["close"]), "color": "#ffea00", "size": "sm", "flex": 3, "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "漲幅", "color": "#777777", "size": "sm", "flex": 2},
                        {"type": "text", "text": f"+{c['pct']}%", "color": "#ff3b3b", "size": "sm", "flex": 3, "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                        {"type": "text", "text": "成交量", "color": "#777777", "size": "sm", "flex": 2},
                        {"type": "text", "text": f"{c['vol']:,} 張", "color": "#e8eaf0", "size": "sm", "flex": 3}
                    ]},
                    {"type": "separator", "margin": "md", "color": "#252830"},
                    {"type": "text", "text": "明日觀察重點", "color": "#777777", "size": "xs", "margin": "md"},
                    {"type": "text", "text": watch, "color": "#aaaaaa", "size": "xs", "wrap": True, "margin": "xs"}
                ]
            }
        }
        bubbles.append(bubble)

    flex_body = {"type": "carousel", "contents": bubbles}
    return FlexMessage(
        alt_text=f"📊 {now} 明日做空觀察清單（{len(candidates)} 支）",
        contents=FlexContainer.from_dict(flex_body)
    )


# ──────────────────────────────────────────────
# 推播
# ──────────────────────────────────────────────

def push_report():
    if not LINE_USER_ID:
        return
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return
    logger.info(f"Pushing report at {now}")
    candidates = screen_candidates()
    msg = build_message(candidates)
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message(PushMessageRequest(to=LINE_USER_ID, messages=[msg]))


# ──────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        if text in ["掃描", "篩選", "今天", "標的", "做空"]:
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="⏳ 篩選中，請稍候...")]
            ))
            candidates = screen_candidates()
            msg = build_message(candidates)
            api.push_message(PushMessageRequest(to=event.source.user_id, messages=[msg]))
        elif text in ["說明", "help", "?"]:
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=(
                    "📋 指令說明\n\n"
                    "傳送以下任一字觸發篩選：\n"
                    "「掃描」「篩選」「今天」「標的」「做空」\n\n"
                    "每日 15:35 自動推播收盤篩選結果\n\n"
                    "篩選條件（大叔策略）：\n"
                    "• 今日漲幅 3~9.5%\n"
                    "• 成交量上市 5000 張以上\n"
                    "• 明日試撮留意量縮+漲不過 2.5%"
                ))]
            ))
        else:
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="傳「掃描」開始篩選，或傳「說明」查看指令。")]
            ))


# ──────────────────────────────────────────────
# 排程 & 啟動
# ──────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TW_TZ)
scheduler.add_job(push_report, "cron", day_of_week="mon-fri", hour=15, minute=35)
scheduler.start()


@app.route("/")
def index():
    return "📈 短空機器人運行中"


@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(TW_TZ).isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
