import os
import time
import json
import html
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import websocket

BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = str(os.getenv("TELEGRAM_CHAT_ID"))

MIN_PEAK_MC = float(os.getenv("MIN_PEAK_MC", "30000"))
SOFT_ALERT_MC = float(os.getenv("SOFT_ALERT_MC", "20000"))
HARD_ALERT_MC = float(os.getenv("HARD_ALERT_MC", "12000"))
BOUNCE_PERCENT = float(os.getenv("BOUNCE_PERCENT", "20"))
RESET_MC = float(os.getenv("RESET_MC", "25000"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

DIGEST_HOUR_CT = int(os.getenv("DIGEST_HOUR_CT", "8"))
OVERNIGHT_START_HOUR_CT = int(os.getenv("OVERNIGHT_START_HOUR_CT", "22"))
TRACK_UNQUALIFIED_HOURS = int(os.getenv("TRACK_UNQUALIFIED_HOURS", "24"))

STATE_FILE = "/data/resurrection_state.json"
OFFSET_FILE = "/data/telegram_offset.json"

CENTRAL = ZoneInfo("America/Chicago")

start_time = time.time()
ws_connected = False
tokens_seen = 0
alerts_sent = 0
last_coin = "None yet"
last_alert = "None yet"
seen_mints = set()

state = {
    "coins": {},
    "overnight": False,
    "digest_queue": [],
    "last_digest_date": ""
}


def n():
    return "\n"


def esc(x):
    return html.escape(str(x or ""))


def now_ct():
    return datetime.now(CENTRAL)


def now_str():
    return now_ct().strftime("%Y-%m-%d %I:%M:%S %p CT")


def uptime():
    s = int(time.time() - start_time)
    return f"{s//3600} hr {(s%3600)//60} min"


def money(x):
    try:
        return "$" + format(float(x), ",.0f")
    except:
        return "Unknown"


def load_state():
    global state
    try:
        with open(STATE_FILE, "r") as f:
            loaded = json.load(f)
            state.update(loaded)
            state.setdefault("coins", {})
            state.setdefault("overnight", False)
            state.setdefault("digest_queue", [])
            state.setdefault("last_digest_date", "")
    except:
        pass


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("save state error", e, flush=True)


def load_offset():
    try:
        with open(OFFSET_FILE, "r") as f:
            return int(json.load(f).get("offset", 0))
    except:
        return 0


def save_offset(offset):
    try:
        with open(OFFSET_FILE, "w") as f:
            json.dump({"offset": offset}, f)
    except:
        pass


def drain_updates():
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT}/getUpdates",
            params={"timeout": 0},
            timeout=10
        )
        updates = r.json().get("result", [])
        if updates:
            newest = updates[-1]["update_id"] + 1
            save_offset(newest)
            return newest
    except:
        pass
    return load_offset()


load_state()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"running")


threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 3002), HealthHandler).serve_forever(),
    daemon=True
).start()


def tg(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json={
                "chat_id": CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            },
            timeout=10
        )
    except Exception as e:
        print("telegram msg error", e, flush=True)


def tg_photo(img, caption):
    global alerts_sent
    try:
        if img:
            requests.post(
                f"https://api.telegram.org/bot{BOT}/sendPhoto",
                json={
                    "chat_id": CHAT,
                    "photo": img,
                    "caption": caption,
                    "parse_mode": "HTML"
                },
                timeout=10
            )
        else:
            tg(caption)
        alerts_sent += 1
    except Exception as e:
        print("telegram photo error", e, flush=True)


def fetch_coin(mint):
    try:
        r = requests.get(
            f"https://frontend-api-v3.pump.fun/coins/{mint}?sync=true",
            timeout=10
        )
        d = r.json()
        return d.get("data", d)
    except Exception as e:
        print("fetch error", e, flush=True)
        return None


def has_x_link(coin):
    blob = json.dumps(coin).lower()
    return "twitter.com" in blob or "x.com" in blob


def has_emoji(text):
    try:
        return any(ord(c) > 10000 for c in str(text or ""))
    except:
        return False


def should_buffer(record):
    if record.get("manual"):
        return False

    if not state.get("overnight", False):
        return False

    hour = now_ct().hour
    return hour >= OVERNIGHT_START_HOUR_CT or hour < DIGEST_HOUR_CT


def queue_digest(kind, mint, record, current_mc, extra=""):
    state["digest_queue"].append({
        "kind": kind,
        "mint": mint,
        "name": record.get("name", "Unknown"),
        "symbol": record.get("symbol", "Unknown"),
        "current_mc": current_mc,
        "peak": record.get("peak", 0),
        "low": record.get("low", 0),
        "extra": extra,
        "time": now_str()
    })
    save_state()


def format_soft(mint, record, current_mc):
    return (
        "👀 <b>Revival Watch</b>" + n() + n()
        + "🪙 <b>Name:</b> " + esc(record["name"]) + n()
        + "🏷 <b>Ticker:</b> " + esc(record["symbol"]) + n()
        + "💰 <b>Current MC:</b> " + money(current_mc) + n()
        + "📈 <b>Peak Seen:</b> " + money(record["peak"]) + n() + n()
        + "🚀 https://pump.fun/coin/" + mint + n() + n()
        + "🧬 <code>" + mint + "</code>"
    )


def format_hard(mint, record, current_mc, bounce):
    return (
        "🧟 <b>Revival Alert</b>" + n() + n()
        + "🪙 <b>Name:</b> " + esc(record["name"]) + n()
        + "🏷 <b>Ticker:</b> " + esc(record["symbol"]) + n()
        + "💰 <b>Current MC:</b> " + money(current_mc) + n()
        + "📉 <b>Local Low:</b> " + money(record["low"]) + n()
        + "📈 <b>Bounce:</b> " + str(round(bounce, 1)) + "%" + n() + n()
        + "🚀 https://pump.fun/coin/" + mint + n() + n()
        + "🧬 <code>" + mint + "</code>"
    )


def send_soft_alert(mint, record, current_mc):
    global last_alert

    if should_buffer(record):
        queue_digest("soft", mint, record, current_mc)
    else:
        tg_photo(record.get("img", ""), format_soft(mint, record, current_mc))

    record["soft_sent"] = True
    last_alert = f"SOFT {record['name']}"
    save_state()


def send_hard_alert(mint, record, current_mc, bounce):
    global last_alert

    if should_buffer(record):
        queue_digest("hard", mint, record, current_mc, f"Bounce: {round(bounce, 1)}%")
    else:
        tg_photo(record.get("img", ""), format_hard(mint, record, current_mc, bounce))

    record["hard_sent"] = True
    last_alert = f"HARD {record['name']}"
    save_state()


def create_coin_record(mint, coin, manual=False):
    mc = float(coin.get("usd_market_cap") or 0)

    state["coins"][mint] = {
        "name": coin.get("name", "Unknown"),
        "symbol": coin.get("symbol", "Unknown"),
        "peak": mc,
        "low": mc if mc > 0 else 999999999,
        "soft_sent": False,
        "hard_sent": False,
        "manual": manual,
        "qualified": manual or mc >= MIN_PEAK_MC,
        "created_at": time.time(),
        "img": coin.get("image_uri") or coin.get("image") or ""
    }

    save_state()


def prune_old_unqualified():
    cutoff = time.time() - (TRACK_UNQUALIFIED_HOURS * 3600)
    removed = []

    for mint, rec in list(state["coins"].items()):
        if rec.get("manual"):
            continue
        if rec.get("qualified"):
            continue
        if rec.get("created_at", 0) < cutoff:
            removed.append(mint)
            del state["coins"][mint]

    if removed:
        save_state()


def process_coin(mint, coin):
    mc = float(coin.get("usd_market_cap") or 0)

    if mint not in state["coins"]:
        if not has_x_link(coin):
            return

        name = coin.get("name", "")
        symbol = coin.get("symbol", "")

        if has_emoji(name) or has_emoji(symbol):
            return

        create_coin_record(mint, coin, manual=False)
        return

    record = state["coins"][mint]

    if mc > record["peak"]:
        record["peak"] = mc

    if mc < record["low"]:
        record["low"] = mc

    if not record.get("qualified") and record["peak"] >= MIN_PEAK_MC:
        record["qualified"] = True

    if not record.get("qualified") and not record.get("manual"):
        save_state()
        return

    if mc >= RESET_MC:
        record["soft_sent"] = False
        record["hard_sent"] = False
        record["low"] = mc

    if mc <= SOFT_ALERT_MC and not record["soft_sent"]:
        send_soft_alert(mint, record, mc)

    if mc <= HARD_ALERT_MC and record["low"] > 0:
        bounce = ((mc - record["low"]) / record["low"]) * 100
        if bounce >= BOUNCE_PERCENT and not record["hard_sent"]:
            send_hard_alert(mint, record, mc, bounce)

    save_state()


def digest_text(clear=True):
    queue = state.get("digest_queue", [])

    if not queue:
        return "🌅 <b>Overnight Meme Digest</b>" + n() + n() + "No buffered revival alerts."

    lines = [
        "🌅 <b>Overnight Meme Digest</b>",
        "",
        "Found " + str(len(queue)) + " buffered setup(s):",
        ""
    ]

    for i, item in enumerate(queue[:15], 1):
        icon = "🧟" if item["kind"] == "hard" else "👀"
        lines.append(
            f"{i}) {icon} <b>{esc(item['name'])}</b> / {esc(item['symbol'])}"
        )
        lines.append("MC: " + money(item["current_mc"]))
        lines.append("Peak: " + money(item["peak"]))
        if item.get("low"):
            lines.append("Low: " + money(item["low"]))
        if item.get("extra"):
            lines.append(esc(item["extra"]))
        lines.append("Pump: https://pump.fun/coin/" + item["mint"])
        lines.append("CA: <code>" + esc(item["mint"]) + "</code>")
        lines.append("")

    if len(queue) > 15:
        lines.append("+" + str(len(queue) - 15) + " more buffered alerts not shown.")

    if clear:
        state["digest_queue"] = []
        save_state()

    return n().join(lines)


def maybe_send_daily_digest():
    today = now_ct().strftime("%Y-%m-%d")
    if now_ct().hour == DIGEST_HOUR_CT and state.get("last_digest_date") != today:
        tg(digest_text(clear=True))
        state["last_digest_date"] = today
        save_state()


def poll_loop():
    while True:
        try:
            prune_old_unqualified()
            maybe_send_daily_digest()

            for mint in list(state["coins"].keys()):
                coin = fetch_coin(mint)
                if coin:
                    process_coin(mint, coin)

        except Exception as e:
            print("poll error", e, flush=True)

        time.sleep(POLL_SECONDS)


def websocket_new_token(ws, message):
    global tokens_seen, last_coin

    try:
        ev = json.loads(message)
        mint = ev.get("mint") or ev.get("mintAddress") or ev.get("ca")

        if not mint or mint in seen_mints:
            return

        seen_mints.add(mint)
        tokens_seen += 1
        last_coin = mint

        coin = fetch_coin(mint)
        if coin:
            process_coin(mint, coin)

    except Exception as e:
        print("ws msg error", e, flush=True)


def on_open(ws):
    global ws_connected
    ws_connected = True
    ws.send(json.dumps({"method": "subscribeNewToken"}))


def on_close(ws, *args):
    global ws_connected
    ws_connected = False


def status():
    return (
        "✅ <b>Resurrection Hunter</b>" + n() + n()
        + "🔌 Websocket: " + ("connected" if ws_connected else "disconnected") + n()
        + "⏱ Uptime: " + uptime() + n()
        + "👀 Tokens seen: " + str(tokens_seen) + n()
        + "🚨 Alerts sent: " + str(alerts_sent) + n()
        + "📚 Tracking: " + str(len(state["coins"])) + n()
        + "🌙 Overnight: " + ("ON" if state.get("overnight") else "OFF") + n()
        + "📥 Buffered alerts: " + str(len(state.get("digest_queue", []))) + n()
        + "⏰ Digest: 8:00 AM Central" + n()
        + "📣 Last alert: " + esc(last_alert) + n()
        + "🕒 " + now_str()
    )


def command_loop():
    offset = drain_updates()

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=30
            )

            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                save_offset(offset)

                msg = update.get("message", {})
                chat = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                if chat != CHAT:
                    continue

                lower = text.lower()

                if lower in ["/status", "status"]:
                    tg(status())

                elif lower in ["/restart", "restart"]:
                    tg("♻️ Restarting resurrection hunter...")
                    time.sleep(1)
                    os._exit(0)

                elif lower == "/overnight on":
                    state["overnight"] = True
                    save_state()
                    tg("🌙 Overnight mode is now ON.")

                elif lower == "/overnight off":
                    state["overnight"] = False
                    save_state()
                    tg("☀️ Overnight mode is now OFF.")

                elif lower in ["/overnight", "overnight"]:
                    tg(
                        "🌙 <b>Overnight Mode</b>" + n() + n()
                        + "Status: " + ("ON" if state.get("overnight") else "OFF") + n()
                        + "Digest time: 8:00 AM Central" + n()
                        + "Buffered alerts: " + str(len(state.get("digest_queue", [])))
                    )

                elif lower in ["/digest", "digest"]:
                    tg(digest_text(clear=True))

                elif lower.startswith("/track "):
                    mint = text.split(" ", 1)[1].strip()
                    coin = fetch_coin(mint)

                    if not coin:
                        tg("Could not fetch that coin.")
                        continue

                    create_coin_record(mint, coin, manual=True)
                    tg("👀 Tracking manually: " + esc(coin.get("name", mint)))

                elif lower.startswith("/untrack "):
                    mint = text.split(" ", 1)[1].strip()
                    if mint in state["coins"]:
                        del state["coins"][mint]
                        save_state()
                        tg("🗑 Untracked.")
                    else:
                        tg("Not tracked.")

                elif lower in ["/tracked", "tracked"]:
                    if not state["coins"]:
                        tg("No tracked coins.")
                    else:
                        lines = ["📚 <b>Tracked Coins</b>"]
                        for mint, rec in list(state["coins"].items())[:25]:
                            tag = "manual" if rec.get("manual") else ("qualified" if rec.get("qualified") else "watching")
                            lines.append(f"• {esc(rec['name'])} — {tag} — <code>{mint}</code>")
                        tg(n().join(lines))

                elif lower in ["/help", "help"]:
                    tg(
                        "🤖 <b>Commands</b>" + n() + n()
                        + "/status" + n()
                        + "/track CA" + n()
                        + "/untrack CA" + n()
                        + "/tracked" + n()
                        + "/overnight on" + n()
                        + "/overnight off" + n()
                        + "/overnight" + n()
                        + "/digest" + n()
                        + "/restart"
                    )

        except Exception as e:
            print("command error", e, flush=True)
            time.sleep(5)


def heartbeat():
    while True:
        time.sleep(HEARTBEAT_MINUTES * 60)
        tg("🟢 Scheduled Checkup" + n() + n() + status())


threading.Thread(target=poll_loop, daemon=True).start()
threading.Thread(target=command_loop, daemon=True).start()
threading.Thread(target=heartbeat, daemon=True).start()

while True:
    try:
        websocket.WebSocketApp(
            "wss://pumpportal.fun/api/data",
            on_open=on_open,
            on_message=websocket_new_token,
            on_close=on_close
        ).run_forever()
    except Exception as e:
        print("websocket crash", e, flush=True)

    time.sleep(5)
