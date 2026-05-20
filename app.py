import os
import json
import requests
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz
import logging
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TW_TZ = pytz.timezone("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# 監控清單
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
    "2324.TW","2352.TW","2358.TW","2377.TW","2383.TW","2392.TW","2405.TW",
    "2498.TW","5871.TW","3481.TW","2393.TW","6147.TW","2363.TW","3045.TW",
    "6789.TWO","6269.TWO","3622.TWO","6265.TWO","3583.TWO","4961.TWO",
    "6191.TWO","8112.TWO","6756.TWO","6679.TWO","6477.TWO","6446.TWO",
    "3653.TWO","3189.TWO","4763.TWO","5269.TWO","6227.TWO","3311.TWO",
]


# ──────────────────────────────────────────────
# Telegram 訊息發送
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
# 資料抓取
# ──────────────────────────────────────────────

def fetch_quotes():
    results = []
    headers = {"User-Agent": UA, "Accept": "application/json"}
    batch_size = 100

    for i in range(0, len(SYMBOLS), batch_size):
        batch = SYMBOLS[i:i+batch_size]
        symbols_str = ",".join(batch)
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols_str}"
        try:
            r = requests.get(url, headers=headers, timeout=12)
            quotes = r.json().get("quoteResponse", {}).get("result", []) or []
            for q in quotes:
                try:
                    symbol = str(q.get("symbol", ""))
                    code = symbol.replace(".TW", "").replace(".TWO", "")
                    name = q.get("shortName", "") or code
                    name = name.replace(" Corp.", "").replace(" Co.,Ltd.", "").strip()
                    close = float(q.get("regularMarketPrice", 0) or 0)
                    pct = float(q.get("regularMarketChangePercent", 0) or 0)
                    vol = int(q.get("regularMarketVolume", 0) or 0) // 1000
                    if close <= 0 or vol <= 0:
                        continue
                    market = "上市" if symbol.endswith(".TW") else "上櫃"
                    if 3.0 <= pct <= 9.5 and vol >= 500:
                        results.append({
                            "market": market, "code": code, "name": name,
                            "close": round(close, 2), "pct": round(pct, 2),
                            "vol": vol, "signal": get_signal(pct, vol)
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Fetch batch error: {e}")

    logger.info(f"fetch_quotes: {len(results)} results")
    return results


def get_signal(pct, vol):
    score = (2 if 3 <= pct <= 6 else 1) + (2 if vol >= 10000 else 1 if vol >= 3000 else 0)
    return "🔴 高度關注" if score >= 3 else "🟡 值得觀察" if score >= 2 else "⚪ 備選"


def screen():
    candidates = fetch_quotes()
    seen = {}
    for c in candidates:
        code = c["code"]
        if code not in seen or c["vol"] > seen[code]["vol"]:
            seen[code] = c
    result = list(seen.values())
    result.sort(key=lambda x: x["vol"], reverse=True)
    return result[:10]


# ──────────────────────────────────────────────
# 訊息格式
# ──────────────────────────────────────────────

def format_report(candidates):
    now = datetime.now(TW_TZ).strftime("%m/%d")
    if not candidates:
        return (
            f"📊 <b>{now} 收盤篩選完畢</b>\n\n"
            "今日無符合條件的標的\n"
            "（量能不足或漲幅不符合策略門檻）"
        )

    lines = [f"📊 <b>{now} 明日做空觀察清單（{len(candidates)} 支）</b>\n"]
    for c in candidates:
        est_vol = max(1, int(c["vol"] * 0.02))
        lines.append(
            f"{c['signal']} <b>{c['code']} {c['name']}</b> [{c['market']}]\n"
            f"  收盤：<b>{c['close']}</b> 元  漲幅：<b>+{c['pct']}%</b>  量：<b>{c['vol']:,}</b> 張\n"
            f"  📌 試撮 &lt;{est_vol:,}張 | 空點 &lt;{round(c['close']*1.025,1)}元 | 停損過高"
        )

    lines.append("\n⚠️ 以上為明日觀察標的，需配合試撮量縮+趨勢確認再進場")
    return "\n".join(lines)


def format_test():
    now = datetime.now(TW_TZ).strftime("%m/%d %H:%M")
    lines = [f"🔧 <b>系統測試 {now}</b>\n"]

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

    candidates = screen()
    lines.append(f"\n📊 篩選結果：{len(candidates)} 支")
    if candidates:
        for c in candidates[:3]:
            lines.append(f"• {c['code']} {c['name']} +{c['pct']}% {c['vol']:,}張")
    else:
        lines.append("（目前無符合條件標的）")
        lines.append("※ 請在收盤後 13:30~15:00 測試")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 排程推播
# ──────────────────────────────────────────────

def push_report():
    if not CHAT_ID:
        logger.warning("CHAT_ID not set")
        return
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return
    logger.info(f"Auto push at {now}")
    candidates = screen()
    tg_send(CHAT_ID, format_report(candidates))


scheduler = BackgroundScheduler(timezone=TW_TZ)
scheduler.add_job(push_report, "cron", day_of_week="mon-fri", hour=13, minute=35)
scheduler.start()


# ──────────────────────────────────────────────
# Telegram Polling（背景執行）
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

    logger.info(f"Message from {chat_id}: {text}")

    if text in ["/start", "掃描", "篩選", "今天", "標的", "做空", "/scan"]:
        tg_send(chat_id, "⏳ 篩選中，請稍候 15~25 秒...")
        candidates = screen()
        tg_send(chat_id, format_report(candidates))

    elif text in ["測試", "/test"]:
        tg_send(chat_id, "🔧 測試中，請稍候...")
        tg_send(chat_id, format_test())

    elif text in ["說明", "/help"]:
        tg_send(chat_id, (
            "📋 <b>指令說明</b>\n\n"
            "【篩選標的】\n/scan 或 掃描 / 篩選 / 做空\n\n"
            "【系統測試】\n/test 或 測試\n\n"
            "【取得我的 Chat ID】\n/id\n\n"
            "【自動推播】\n每日 13:35 自動推播\n\n"
            "篩選條件：漲幅 3~9.5% + 量能足夠"
        ))

    elif text in ["/id"]:
        tg_send(chat_id, f"你的 Chat ID 是：\n<code>{chat_id}</code>")

    else:
        tg_send(chat_id, "傳 /scan 篩選標的，傳 /test 測試系統，傳 /help 查看說明。")


def polling_loop():
    global last_update_id
    logger.info("Telegram polling started")
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=35
            )
            updates = r.json().get("result", [])
            for update in updates:
                handle_update(update)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            import time
            time.sleep(5)


# 在背景執行 polling
polling_thread = threading.Thread(target=polling_loop, daemon=True)
polling_thread.start()


# ──────────────────────────────────────────────
# Flask（讓 Render 保持運行）
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return "📈 短空 Telegram 機器人運行中"


@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(TW_TZ).isoformat()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
