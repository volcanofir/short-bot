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
# 資料抓取：證交所 openapi（每日更新）
# ──────────────────────────────────────────────

def fetch_twse():
    """上市股票 - 使用證交所 openapi"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    results = []
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        })
        if r.status_code != 200:
            logger.error(f"TWSE status: {r.status_code}")
            return results
        data = r.json()
        logger.info(f"TWSE data count: {len(data)}")
        if data:
            logger.info(f"TWSE sample keys: {list(data[0].keys()) if data else 'empty'}")

        for item in data:
            try:
                code = str(item.get("Code", "") or item.get("股票代號", "")).strip()
                name = str(item.get("Name", "") or item.get("股票名稱", "")).strip()
                vol_str = str(item.get("TradeVolume", "") or item.get("成交股數", "0")).replace(",", "")
                close_str = str(item.get("ClosingPrice", "") or item.get("收盤價", "0")).replace(",", "")
                change_str = str(item.get("Change", "") or item.get("漲跌價差", "0")).replace(",", "").replace("+", "").strip()

                if not code or not close_str or close_str in ["--", "", "0", "nan"]:
                    continue
                if not change_str or change_str in ["--", "", "nan", "X0.00"]:
                    continue

                # 處理負號格式
                if change_str.startswith("X"):
                    change_str = "-" + change_str[1:]

                vol = int(float(vol_str)) // 1000 if vol_str else 0
                close = float(close_str)
                chg = float(change_str)
                prev = close - chg
                if prev <= 0 or close <= 0:
                    continue
                pct = chg / prev * 100

                if 3.0 <= pct <= 9.5 and vol >= 1000:
                    results.append({
                        "market": "上市", "code": code, "name": name,
                        "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                        "signal": get_signal(pct, vol)
                    })
            except Exception as ex:
                continue

    except Exception as e:
        logger.error(f"TWSE fetch error: {e}")
    return results


def fetch_tpex():
    """上櫃股票 - 使用櫃買中心 api"""
    today = datetime.now(TW_TZ)
    roc = f"{today.year-1911}/{today.month:02d}/{today.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc}&se=AL"
    results = []
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        rows = r.json().get("aaData", [])
        logger.info(f"TPEX data count: {len(rows)}")
        for row in rows:
            try:
                code = str(row[0]).strip()
                name = str(row[1]).strip()
                close_str = str(row[2]).replace(",", "")
                change_str = str(row[3]).replace(",", "").replace("+", "").strip()
                vol_str = str(row[7]).replace(",", "")

                if close_str in ["--", ""] or change_str in ["--", ""]:
                    continue

                close = float(close_str)
                chg = float(change_str)
                vol = int(float(vol_str)) // 1000 if vol_str else 0
                prev = close - chg
                if prev <= 0:
                    continue
                pct = chg / prev * 100

                if 3.0 <= pct <= 9.5 and vol >= 500:
                    results.append({
                        "market": "上櫃", "code": code, "name": name,
                        "close": round(close, 2), "pct": round(pct, 2), "vol": vol,
                        "signal": get_signal(pct, vol)
                    })
            except Exception:
                continue
    except Exception as e:
        logger.error(f"TPEX error: {e}")
    return results


# ──────────────────────────────────────────────
# 篩選邏輯
# ──────────────────────────────────────────────

def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    twse = fetch_twse()
    tpex = fetch_tpex()
    candidates = twse + tpex
    logger.info(f"Total candidates before sort: TWSE={len(twse)}, TPEX={len(tpex)}")
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
            "（可能原因：今日量能普遍不足、或資料尚未更新）\n\n"
            "傳「測試」可查看 API 連線狀態"
        )}]

    bubbles = []
    for c in candidates:
        color = "#ff3b3b" if "高度" in c["signal"] else "#ffb800" if "值得" in c["signal"] else "#888888"
        est_vol = max(1, int(c["vol"] * 0.02))
        watch = f"• 明日試撮量低於 {est_vol:,} 張時注意\n• 漲不過 {round(c['close']*1.025, 1)} 元可考慮空\n• 停損設過早盤高點（控制 1% 內）"
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
    """測試 API 連線狀態"""
    now = datetime.now(TW_TZ).strftime("%m/%d %H:%M")
    lines = [f"🔧 系統測試 {now}\n"]

    # 測試 TWSE
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        lines.append(f"✅ 上市 API 正常（{len(data)} 筆）")
        if data:
            sample = data[0]
            lines.append(f"   欄位：{', '.join(list(sample.keys())[:4])}")
    except Exception as e:
        lines.append(f"❌ 上市 API 失敗：{str(e)[:50]}")

    # 測試 TPEX
    try:
        today = datetime.now(TW_TZ)
        roc = f"{today.year-1911}/{today.month:02d}/{today.day:02d}"
        r = requests.get(
            f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc}&se=AL",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"}
        )
        rows = r.json().get("aaData", [])
        lines.append(f"✅ 上櫃 API 正常（{len(rows)} 筆）")
    except Exception as e:
        lines.append(f"❌ 上櫃 API 失敗：{str(e)[:50]}")

    # 執行一次篩選
    try:
        twse = fetch_twse()
        tpex = fetch_tpex()
        lines.append(f"\n📊 篩選結果：")
        lines.append(f"上市符合條件：{len(twse)} 支")
        lines.append(f"上櫃符合條件：{len(tpex)} 支")
        if twse:
            top = twse[0]
            lines.append(f"最大量範例：{top['code']} {top['name']} +{top['pct']}% {top['vol']:,}張")
    except Exception as e:
        lines.append(f"篩選錯誤：{str(e)[:50]}")

    return [{"type": "text", "text": "\n".join(lines)}]


# ──────────────────────────────────────────────
# 排程推播
# ──────────────────────────────────────────────

def push_report():
    if not USER_ID:
        logger.warning("USER_ID not set")
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
                "【篩選標的】\n"
                "傳：掃描 / 篩選 / 今天 / 標的 / 做空\n\n"
                "【系統測試】\n"
                "傳：測試\n"
                "→ 顯示 API 連線狀態與資料筆數\n\n"
                "【自動推播】\n"
                "每日 15:35 自動推播收盤篩選結果\n\n"
                "篩選條件（大叔策略）：\n"
                "• 今日漲幅 3~9.5%\n"
                "• 成交量上市 1000 張以上\n"
                "• 明日試撮留意量縮+漲不過 2.5%"
            )}])

        else:
            reply(rt, [{"type": "text", "text": "傳「掃描」篩選標的，傳「測試」檢查系統，傳「說明」查看所有指令。"}])

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
