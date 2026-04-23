import os
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
VOL_MULTIPLIER = float(os.environ.get("VOL_MULTIPLIER", "5"))
PRICE_PCT = float(os.environ.get("PRICE_PCT", "10"))
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "4"))
MIN_MCAP = float(os.environ.get("MIN_MCAP", "1000000"))

BINANCE_API = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"

known_ids = None
baselines = {}
token_map = {}
last_alerted_momentum = {}
last_alerted_volume = {}
last_alerted_price = {}
alerted_graduated = set()
new_session_ids = set()
graduated_ids = set()
cnt_new = cnt_vol = cnt_price = cnt_momentum = cnt_grad = 0
app = None

def fp(p):
    try:
        n = float(p)
        if n == 0: return "—"
        if n < 0.000001: return f"${n:.2e}"
        if n < 0.01: return f"${n:.6f}"
        if n < 1: return f"${n:.4f}"
        if n < 1000: return f"${n:.2f}"
        return f"${n:,.0f}"
    except: return "—"

def fv(v):
    try:
        n = float(v)
        if n >= 1e9: return f"${n/1e9:.1f}B"
        if n >= 1e6: return f"${n/1e6:.1f}M"
        if n >= 1e3: return f"${n/1e3:.0f}K"
        return f"${n:.0f}"
    except: return "—"

def fpct(n):
    return f"{'+'if n>=0 else ''}{n:.1f}%"

def now_str():
    return datetime.utcnow().strftime("%H:%M:%S UTC")

def trade_link(sym):
    return f"https://www.binance.com/en/trade/{sym.upper()}_USDT?type=alpha"

def is_cooled_down(d, tid):
    last = d.get(tid)
    return last is None or datetime.utcnow() - last > timedelta(hours=COOLDOWN_HOURS)

def mark_alerted(d, tid):
    d[tid] = datetime.utcnow()

async def send(msg):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Send error: {e}")

def fetch_tokens():
    r = requests.get(BINANCE_API, timeout=15)
    r.raise_for_status()
    return [t for t in r.json().get("data", []) if not t.get("offline") and not t.get("offsell") and float(t.get("marketCap") or 0) >= MIN_MCAP]

async def detect_graduations(tokens):
    global cnt_grad
    for t in tokens:
        tid, sym = t["tokenId"], t["symbol"].upper()
        if tid not in baselines: continue
        was, now = baselines[tid].get("cex_listed", False), bool(t.get("listingCex", False))
        if now and not was and tid not in alerted_graduated:
            alerted_graduated.add(tid); graduated_ids.add(tid); cnt_grad += 1
            baselines[tid]["cex_listed"] = True
            await send(f"🎓 <b>GRADUATED TO BINANCE SPOT!</b>\n━━━━━━━━━━━━━━━\n<b>{sym}</b> ({t.get('name','')})\n\nPrice: {fp(t.get('price'))} | MCap: {fv(t.get('marketCap'))}\n\n📊 <a href='{trade_link(sym)}'>Trade {sym}</a>\n🕐 {now_str()}")
        else:
            baselines[tid]["cex_listed"] = now

async def detect_signals(tokens):
    global cnt_vol, cnt_price, cnt_momentum
    for t in tokens:
        tid, sym = t["tokenId"], t["symbol"].upper()
        cur_p, cur_v = float(t.get("price", 0) or 0), float(t.get("volume24h", 0) or 0)
        base = baselines.get(tid)
        if not base: continue
        vr = cur_v / base["volume"] if base["volume"] > 0 else 0
        pg = ((cur_p - base["price"]) / base["price"] * 100) if base["price"] > 0 else 0
        vs, pp, lk = vr >= VOL_MULTIPLIER, pg >= PRICE_PCT, trade_link(sym)

        if vs and pp and is_cooled_down(last_alerted_momentum, tid):
            if tid not in last_alerted_momentum: cnt_momentum += 1
            mark_alerted(last_alerted_momentum, tid)
            await send(f"🚀 <b>MOMENTUM ALERT</b>\n━━━━━━━━━━━━━━━\n<b>{sym}</b>\n\n📈 Vol: <b>{vr:.1f}×</b> | {fv(base['volume'])} → {fv(cur_v)}\n💰 Price: <b>{fpct(pg)}</b> | {fp(base['price'])} → {fp(cur_p)}\n\n📊 <a href='{lk}'>Trade {sym}</a>\n🕐 {now_str()}")
        elif vs and not pp and is_cooled_down(last_alerted_volume, tid):
            if tid not in last_alerted_volume: cnt_vol += 1
            mark_alerted(last_alerted_volume, tid)
            await send(f"📈 <b>VOLUME SPIKE</b>\n━━━━━━━━━━━━━━━\n<b>{sym}</b>\n\nVol: <b>{vr:.1f}×</b> above baseline\n{fv(base['volume'])} → {fv(cur_v)}\nPrice: {fp(cur_p)} ({fpct(pg)} from entry)\n\n📊 <a href='{lk}'>Trade {sym}</a>\n🕐 {now_str()}")
        elif pp and not vs and is_cooled_down(last_alerted_price, tid):
            if tid not in last_alerted_price: cnt_price += 1
            mark_alerted(last_alerted_price, tid)
            await send(f"💰 <b>PRICE PUMP</b>\n━━━━━━━━━━━━━━━\n<b>{sym}</b>\n\nUp <b>{fpct(pg)}</b> from entry\n{fp(base['price'])} → {fp(cur_p)}\nVol: {fv(cur_v)}\n\n📊 <a href='{lk}'>Trade {sym}</a>\n🕐 {now_str()}")

async def poll_job():
    global known_ids, cnt_new
    try:
        tokens = fetch_tokens()
    except Exception as e:
        log.error(f"Fetch failed: {e}"); return

    for t in tokens:
        token_map[t["tokenId"]] = t

    def make_base(t):
        return {"price": float(t.get("price", 0) or 0), "volume": float(t.get("volume24h", 0) or 0), "first_seen": datetime.utcnow(), "symbol": t["symbol"].upper(), "name": t.get("name", ""), "cex_listed": bool(t.get("listingCex", False))}

    if known_ids is None:
        known_ids = {t["tokenId"] for t in tokens}
        for t in tokens: baselines[t["tokenId"]] = make_base(t)
        log.info(f"Baselines set for {len(tokens)} tokens.")
        await send(f"✅ <b>Alpha Watch Bot is live!</b>\n━━━━━━━━━━━━━━━\nTracking <b>{len(tokens)}</b> tokens (min ${MIN_MCAP/1e6:.0f}M mcap)\nPoll: <b>{POLL_INTERVAL}s</b> | Vol: <b>{VOL_MULTIPLIER}×</b> | Price: <b>+{PRICE_PCT}%</b>\n\n/status /signals /top /settings 📡")
        return

    for t in tokens:
        if t["tokenId"] not in known_ids:
            tid, sym = t["tokenId"], t["symbol"].upper()
            known_ids.add(tid); new_session_ids.add(tid); cnt_new += 1
            baselines[tid] = make_base(t)
            await send(f"⚡ <b>NEW ALPHA LISTING</b>\n━━━━━━━━━━━━━━━\n<b>{sym}</b> — {t.get('name','')}\nChain: {t.get('chainName','')} | MCap: {fv(t.get('marketCap'))}\nPrice: {fp(t.get('price'))}\n\n📊 <a href='{trade_link(sym)}'>View {sym}</a>\n🕐 {now_str()}")
        elif t["tokenId"] not in baselines:
            baselines[t["tokenId"]] = make_base(t)

    await detect_graduations(tokens)
    await detect_signals(tokens)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("👋 <b>Alpha Watch Bot</b>\n\n⚡ New listings\n📈 Volume spikes\n💰 Price pumps\n🚀 Momentum\n🎓 Graduations\n\n/status /signals /top /settings", parse_mode="HTML")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"📊 <b>Status</b>\n━━━━━━━━━━━━━━━\nTracking: <b>{len(baselines)}</b> tokens\nNew: <b>{cnt_new}</b> | Vol: <b>{cnt_vol}</b> | Price: <b>{cnt_price}</b>\nMomentum: <b>{cnt_momentum}</b> | Grad: <b>{cnt_grad}</b>\n\nVol: {VOL_MULTIPLIER}× | Price: +{PRICE_PCT}% | Poll: {POLL_INTERVAL}s\n🕐 {now_str()}", parse_mode="HTML")

async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    lines = ["📡 <b>Active Signals</b>\n━━━━━━━━━━━━━━━"]
    found = False
    for tid, t in token_map.items():
        sym = t["symbol"].upper()
        is_m = tid in last_alerted_momentum
        is_v = tid in last_alerted_volume and not is_m
        is_p = tid in last_alerted_price and not is_m
        is_g = tid in graduated_ids
        is_n = tid in new_session_ids and not any([is_m,is_v,is_p])
        if not any([is_m,is_v,is_p,is_g,is_n]): continue
        found = True
        icon = "🎓" if is_g else "🚀" if is_m else "📈" if is_v else "💰" if is_p else "⚡"
        chg = float(t.get("percentChange24h", 0) or 0)
        lines.append(f"{icon} <b>{sym}</b> {fpct(chg)} | <a href='{trade_link(sym)}'>Trade</a>")
    if not found: lines.append("No signals yet.")
    await u.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_top(u: Update, c: ContextTypes.DEFAULT_TYPE):
    tokens = sorted(token_map.values(), key=lambda t: float(t.get("percentChange24h", 0) or 0), reverse=True)
    lines = ["🔥 <b>Top Movers</b>\n━━━━━━━━━━━━━━━"]
    for t in tokens[:10]:
        sym = t["symbol"].upper()
        lines.append(f"<b>{sym}</b> {fpct(float(t.get('percentChange24h',0) or 0))} | {fp(t.get('price'))} | <a href='{trade_link(sym)}'>Trade</a>")
    await u.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_settings(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"⚙️ <b>Settings</b>\n━━━━━━━━━━━━━━━\nMcap: ${MIN_MCAP/1e6:.0f}M | Poll: {POLL_INTERVAL}s\nVol: {VOL_MULTIPLIER}× | Price: +{PRICE_PCT}%\nCool-down: {COOLDOWN_HOURS}h", parse_mode="HTML")

async def main():
    global app
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("settings", cmd_settings))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_job, "interval", seconds=POLL_INTERVAL)
    scheduler.start()

    async with app:
        await app.start()
        await poll_job()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
