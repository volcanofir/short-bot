import os
import json
import requests
import pandas as pd
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")  # 你的 LINE User ID

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

TW_TZ = pytz.timezone("Asia/Taipei")


# ──────────────────────────────────────────────
# 資料抓取：台灣證交所
# ──────────────────────────────────────────────

def fetch_twse_day_trades():
    """抓取當日上市股票成交資料 (證交所)"""
    today = datetime.now(TW_TZ).strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={today}&type=ALLBUT0999"
    try:
        r = requests.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        # 找到個股資料的表格 (table9 是全部股票)
        for table in data.get("tables", []):
            if table.get("title", "").startswith("各類指數"):
                continue
            fields = table.get("fields", [])
            if "漲跌價差" in fields and "成交股數" in fields:
                return table.get("data", []), fields
    except Exception as e:
        logger.error(f"TWSE fetch error: {e}")
    return [], []


def fetch_tpex_day_trades():
    """抓取當日上櫃股票成交資料 (櫃買中心)"""
    today = datetime.now(TW_TZ)
    # 民國年
    roc_date = f"{today.year - 1911}/{today.month:02d}/{today.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=AL"
    try:
        r = requests.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        rows = data.get("aaData", [])
        return rows
    except Exception as e:
        logger.error(f"TPEX fetch error: {e}")
    return []


# ──────────────────────────────────────────────
# 大叔策略：篩選明日觀察標的
# ──────────────────────────────────────────────

def screen_candidates():
    """
    篩選條件（大叔策略）：
    1. 今日爆量大漲（成交量前排 + 漲幅 3~9%）
    2. 當沖率高（代替指標：成交量異常大）
    3. 漲幅不超過 9.5%（避免鎖漲停留倉風險）
    4. 排除處置股
    回傳：明日值得觀察做空的標的列表
    """
    candidates = []

    # ── 上市 ──
    rows, fields = fetch_twse_day_trades()
    if rows and fields:
        try:
            fi = {f: i for i, f in enumerate(fields)}
            for row in rows:
                try:
                    code = row[fi.get("證券代號", 0)].strip()
                    name = row[fi.get("證券名稱", 1)].strip()
                    vol_str = row[fi.get("成交股數", 2)].replace(",", "")
                    close_str = row[fi.get("收盤價", -1)].replace(",", "")
                    change_str = row[fi.get("漲跌價差", -1)].replace(",", "").replace("+", "")
                    vol = int(vol_str) // 1000  # 轉成張
                    close = float(close_str)
                    change_price = float(change_str)
                    prev_close = close - change_price
                    if prev_close <= 0:
                        continue
                    change_pct = change_price / prev_close * 100

                    # 篩選條件
                    if (3.0 <= change_pct <= 9.5  # 漲幅 3~9.5%
                            and vol >= 5000        # 成交量 5000 張以上
                            and not any(x in name for x in ["DR", "KY"])):  # 排除特殊股
                        candidates.append({
                            "market": "上市",
                            "code": code,
                            "name": name,
                            "close": close,
                            "change_pct": round(change_pct, 2),
                            "vol": vol,
                            "signal": classify_signal(change_pct, vol)
                        })
                except (ValueError, IndexError, KeyError):
                    continue
        except Exception as e:
            logger.error(f"TWSE screen error: {e}")

    # ── 上櫃 ──
    tpex_rows = fetch_tpex_day_trades()
    for row in tpex_rows:
        try:
            code = str(row[0]).strip()
            name = str(row[1]).strip()
            close = float(str(row[2]).replace(",", ""))
            change_str = str(row[3]).replace(",", "").replace("+", "")
            vol = int(str(row[7]).replace(",", "")) // 1000  # 成交張數
            change_price = float(change_str)
            prev_close = close - change_price
            if prev_close <= 0:
                continue
            change_pct = change_price / prev_close * 100

            if (3.0 <= change_pct <= 9.5
                    and vol >= 2000):
                candidates.append({
                    "market": "上櫃",
                    "code": code,
                    "name": name,
                    "close": close,
                    "change_pct": round(change_pct, 2),
                    "vol": vol,
                    "signal": classify_signal(change_pct, vol)
                })
        except (ValueError, IndexError):
            continue

    # 依成交量排序，取前 10
    candidates.sort(key=lambda x: x["vol"], reverse=True)
    return candidates[:10]


def classify_signal(change_pct, vol):
    """給予訊號強度"""
    score = 0
    if 3 <= change_pct <= 6:
        score += 2  # 漲幅甜蜜點
    elif 6 < change_pct <= 9:
        score += 1
    if vol >= 20000:
        score += 2
    elif vol >= 10000:
        score += 1
    if score >= 3:
        return "🔴 高度關注"
    elif score >= 2:
        return "🟡 值得觀察"
    else:
        return "⚪ 備選"


# ──────────────────────────────────────────────
# LINE 訊息組裝
# ──────────────────────────────────────────────

def build_flex_message(candidates):
    """組成 Flex Message 推播"""
    now = datetime.now(TW_TZ).strftime("%m/%d")

    if not candidates:
        return TextSendMessage(
            text=f"📊 {now} 收盤篩選完畢\n\n今日無符合做空觀察條件的標的\n（量能不足或漲幅不符合策略門檻）"
        )

    # Flex bubble list
    bubbles = []
    for c in candidates:
        color = "#ff3b3b" if "高度" in c["signal"] else "#ffb800" if "值得" in c["signal"] else "#888888"
        bubble = {
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#111318",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{c['code']} {c['name']}",
                        "color": "#ffffff",
                        "size": "md",
                        "weight": "bold"
                    },
                    {
                        "type": "text",
                        "text": c["signal"],
                        "color": color,
                        "size": "xs",
                        "margin": "xs"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#1a1d24",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "市場", "color": "#777777", "size": "sm", "flex": 2},
                            {"type": "text", "text": c["market"], "color": "#e8eaf0", "size": "sm", "flex": 3, "weight": "bold"}
                        ]
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "margin": "sm",
                        "contents": [
                            {"type": "text", "text": "收盤價", "color": "#777777", "size": "sm", "flex": 2},
                            {"type": "text", "text": f"{c['close']}", "color": "#ffea00", "size": "sm", "flex": 3, "weight": "bold"}
                        ]
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "margin": "sm",
                        "contents": [
                            {"type": "text", "text": "今日漲幅", "color": "#777777", "size": "sm", "flex": 2},
                            {"type": "text", "text": f"+{c['change_pct']}%", "color": "#ff3b3b", "size": "sm", "flex": 3, "weight": "bold"}
                        ]
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "margin": "sm",
                        "contents": [
                            {"type": "text", "text": "成交量", "color": "#777777", "size": "sm", "flex": 2},
                            {"type": "text", "text": f"{c['vol']:,} 張", "color": "#e8eaf0", "size": "sm", "flex": 3}
                        ]
                    },
                    {
                        "type": "separator",
                        "margin": "md",
                        "color": "#252830"
                    },
                    {
                        "type": "text",
                        "text": "明日觀察重點",
                        "color": "#777777",
                        "size": "xs",
                        "margin": "md"
                    },
                    {
                        "type": "text",
                        "text": build_watch_note(c),
                        "color": "#aaaaaa",
                        "size": "xs",
                        "wrap": True,
                        "margin": "xs"
                    }
                ]
            }
        }
        bubbles.append(bubble)

    flex = FlexSendMessage(
        alt_text=f"📊 {now} 明日做空觀察清單（{len(candidates)} 支）",
        contents={
            "type": "carousel",
            "contents": bubbles
        }
    )
    return flex


def build_watch_note(c):
    notes = []
    notes.append(f"• 試撮留意量縮（低於 {int(c['vol'] * 0.02):,} 張）")
    notes.append(f"• 漲不過 {round(c['close'] * 1.025, 1)} 元（+2.5%）可空")
    if c['change_pct'] >= 6:
        notes.append(f"• 漲幅偏高，等破開盤價再空較安全")
    notes.append(f"• 停損設過早盤高點，控制在 1% 內")
    return "\n".join(notes)


# ──────────────────────────────────────────────
# 推播主函數
# ──────────────────────────────────────────────

def push_daily_report():
    """每日收盤後推播"""
    if not LINE_USER_ID:
        logger.warning("LINE_USER_ID not set, skipping push")
        return

    now = datetime.now(TW_TZ)
    # 只在週一到週五推播
    if now.weekday() >= 5:
        logger.info("Weekend, skip push")
        return

    logger.info(f"Running daily screen at {now}")
    candidates = screen_candidates()
    msg = build_flex_message(candidates)

    try:
        line_bot_api.push_message(LINE_USER_ID, msg)
        logger.info(f"Pushed {len(candidates)} candidates")
    except Exception as e:
        logger.error(f"Push error: {e}")


# ──────────────────────────────────────────────
# LINE Webhook（讓使用者手動觸發）
# ──────────────────────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    if text in ["掃描", "篩選", "今天", "標的", "做空"]:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="⏳ 正在篩選中，請稍候...")
        )
        candidates = screen_candidates()
        msg = build_flex_message(candidates)
        line_bot_api.push_message(event.source.user_id, msg)

    elif text in ["說明", "help", "Help", "?"]:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=(
                "📋 指令說明\n\n"
                "傳送以下任一字即可觸發篩選：\n"
                "「掃描」「篩選」「今天」「標的」「做空」\n\n"
                "每日 15:30 會自動推播收盤篩選結果\n\n"
                "篩選條件（大叔策略）：\n"
                "• 今日漲幅 3~9.5%\n"
                "• 成交量上市 5000 張以上\n"
                "• 明日試撮留意量縮+漲不過 2.5%\n"
                "• 籌碼雜亂（隔日沖風險）"
            ))
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="傳「掃描」開始篩選做空標的，或傳「說明」查看指令。")
        )


# ──────────────────────────────────────────────
# 排程
# ──────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TW_TZ)
# 每天 15:35 推播（收盤後 5 分鐘，等資料更新）
scheduler.add_job(push_daily_report, "cron",
                  day_of_week="mon-fri", hour=15, minute=35)
scheduler.start()


@app.route("/")
def index():
    return "📈 短空機器人運行中 | 傳送任意文字至 LINE 觸發篩選"


@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(TW_TZ).isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
