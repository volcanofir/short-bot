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
logger = logging.getLogger(**name**)

app = Flask(**name**)

TOKEN = os.environ.get(“LINE_CHANNEL_ACCESS_TOKEN”, “”)
SECRET = os.environ.get(“LINE_CHANNEL_SECRET”, “”)
USER_ID = os.environ.get(“LINE_USER_ID”, “”)
TW_TZ = pytz.timezone(“Asia/Taipei”)

def get_headers():
return {
“Content-Type”: “application/json”,
“Authorization”: f”Bearer {TOKEN}”
}

def reply(reply_token, messages):
requests.post(
“https://api.line.me/v2/bot/message/reply”,
headers=get_headers(),
json={“replyToken”: reply_token, “messages”: messages},
timeout=10
)

def push(to, messages):
requests.post(
“https://api.line.me/v2/bot/message/push”,
headers=get_headers(),
json={“to”: to, “messages”: messages},
timeout=10
)

def verify_signature(body, sig):
digest = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
return base64.b64encode(digest).decode() == sig

# ──────────────────────────────────────────────

# 資料抓取：使用穩定的 openapi 格式

# ──────────────────────────────────────────────

def fetch_twse():
“”“抓取上市股票（使用 openapi，欄位穩定）”””
url = “https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL”
try:
r = requests.get(url, timeout=15, headers={“User-Agent”: “Mozilla/5.0”})
data = r.json()
results = []
for item in data:
try:
code = str(item.get(“Code”, “”)).strip()
name = str(item.get(“Name”, “”)).strip()
vol_str = str(item.get(“TradeVolume”, “0”)).replace(”,”, “”)
close_str = str(item.get(“ClosingPrice”, “0”)).replace(”,”, “”)
change_str = str(item.get(“Change”, “0”)).replace(”,”, “”).replace(”+”, “”)

```
            if not vol_str or not close_str or not change_str:
                continue
            if close_str in ["--", "", "0"] or change_str in ["--", ""]:
                continue

            vol = int(float(vol_str)) // 1000
            close = float(close_str)
            chg = float(change_str)
            prev = close - chg
            if prev <= 0:
                continue
            pct = chg / prev * 100

            if 3.0 <= pct <= 9.5 and vol >= 3000:
                results.append({
                    "market": "上市", "code": code, "name": name,
                    "close": close, "pct": round(pct, 2), "vol": vol,
                    "signal": get_signal(pct, vol)
                })
        except Exception:
            continue
    return results
except Exception as e:
    logger.error(f"TWSE openapi error: {e}")
return []
```

def fetch_tpex():
“”“抓取上櫃股票”””
today = datetime.now(TW_TZ)
roc = f”{today.year-1911}/{today.month:02d}/{today.day:02d}”
url = f”https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc}&se=AL”
try:
r = requests.get(url, timeout=15, headers={“User-Agent”: “Mozilla/5.0”})
rows = r.json().get(“aaData”, [])
results = []
for row in rows:
try:
code = str(row[0]).strip()
name = str(row[1]).strip()
close_str = str(row[2]).replace(”,”, “”)
change_str = str(row[3]).replace(”,”, “”).replace(”+”, “”)
vol_str = str(row[7]).replace(”,”, “”)

```
            if close_str in ["--", ""] or change_str in ["--", ""]:
                continue

            close = float(close_str)
            chg = float(change_str)
            vol = int(float(vol_str)) // 1000
            prev = close - chg
            if prev <= 0:
                continue
            pct = chg / prev * 100

            if 3.0 <= pct <= 9.5 and vol >= 1000:
                results.append({
                    "market": "上櫃", "code": code, "name": name,
                    "close": close, "pct": round(pct, 2), "vol": vol,
                    "signal": get_signal(pct, vol)
                })
        except Exception:
            continue
    return results
except Exception as e:
    logger.error(f"TPEX error: {e}")
return []
```

# ──────────────────────────────────────────────

# 篩選邏輯

# ──────────────────────────────────────────────

def get_signal(pct, vol):
score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 20000 else 1 if vol >= 10000 else 0)
return “🔴 高度關注” if score >= 3 else “🟡 值得觀察” if score >= 2 else “⚪ 備選”

def screen():
candidates = fetch_twse() + fetch_tpex()
candidates.sort(key=lambda x: x[“vol”], reverse=True)
return candidates[:10]

# ──────────────────────────────────────────────

# 訊息組裝

# ──────────────────────────────────────────────

def build_messages(candidates):
now = datetime.now(TW_TZ).strftime(”%m/%d”)
if not candidates:
return [{“type”: “text”, “text”: f”📊 {now} 收盤篩選完畢\n\n今日無符合條件的標的\n（量能不足或漲幅不符合策略門檻）”}]

```
bubbles = []
for c in candidates:
    color = "#ff3b3b" if "高度" in c["signal"] else "#ffb800" if "值得" in c["signal"] else "#888888"
    watch = f"• 試撮量低於 {max(1, int(c['vol']*0.02)):,} 張時注意\n• 漲不過 {round(c['close']*1.025, 1)} 元可考慮空\n• 停損設過早盤高點（控制 1% 內）"
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
```

# ──────────────────────────────────────────────

# 排程推播

# ──────────────────────────────────────────────

def push_report():
if not USER_ID:
logger.warning(“USER_ID not set”)
return
now = datetime.now(TW_TZ)
if now.weekday() >= 5:
return
logger.info(f”Pushing report at {now}”)
push(USER_ID, build_messages(screen()))

scheduler = BackgroundScheduler(timezone=TW_TZ)
scheduler.add_job(push_report, “cron”, day_of_week=“mon-fri”, hour=15, minute=35)
scheduler.start()

# ──────────────────────────────────────────────

# Webhook

# ──────────────────────────────────────────────

@app.route(”/callback”, methods=[“POST”])
def callback():
sig = request.headers.get(“X-Line-Signature”, “”)
body = request.get_data(as_text=True)
if not verify_signature(body, sig):
abort(400)

```
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
    elif text in ["說明", "help", "?"]:
        reply(rt, [{"type": "text", "text": (
            "📋 指令說明\n\n傳送以下任一字觸發篩選：\n"
            "「掃描」「篩選」「今天」「標的」「做空」\n\n"
            "每日 15:35 自動推播收盤篩選結果\n\n"
            "篩選條件（大叔策略）：\n"
            "• 今日漲幅 3~9.5%\n"
            "• 上市成交量 3000 張以上\n"
            "• 明日試撮留意量縮+漲不過 2.5%"
        )}])
    else:
        reply(rt, [{"type": "text", "text": "傳「掃描」開始篩選，或傳「說明」查看指令。"}])

return "OK"
```

@app.route(”/”)
def index():
return “📈 短空機器人運行中”

@app.route(”/health”)
def health():
return {“status”: “ok”, “time”: datetime.now(TW_TZ).isoformat()}

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, 5000))
app.run(host=“0.0.0.0”, port=port)