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

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["CHAT_ID"]
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL",   "60"))
VOL_MULTIPLIER  = float(os.environ.get("VOL_MULTIPLIER", "5"))
PRICE_PCT       = float(os.environ.get("PRICE_PCT",      "10"))
COOLDOWN_HOURS  = float(os.environ.get("COOLDOWN_HOURS",  "4"))
MIN_MCAP        = float(os.environ.get("MIN_MCAP", "1000000"))

BINANCE_API = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"

known_ids        = None
baselines        = {}
token_map        = {}
last_alerted_momentum = {}
last_alerted_volume   = {}
last_alerted_price    = {}
alerted_graduated     = set()
new_session_ids  = set()
graduated_ids    = set()
cnt_new = cnt_vol = cnt_price = cnt_momentum = cnt_grad = 0
bot: Bot = None

def fp(p):
    try:
        n = float(p)
        if n == 0:        return "—"
        if n < 0.000001:  return f"${n:.2e}"
        if n < 0.01:      return f"${n:.6f}"
        if n < 1:         return f"${n:.4f}"
        if n < 1000:      return f"${n:.2f}"
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

def is_cooled_down(alert_dict, tid):
    last = alert_dict.get(tid)
    if last is None:
        return True
    return datetime.utcnow() - last > timedelta(hours=COOLDOWN_HOURS)

def mark_alerted(alert_dict, tid):
    alert_dict[tid] = datetime.utcnow()

async def send(msg):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def fetch_tokens():
    resp = requests.get(BINANCE_API, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [t for t in data if not t.get("offline") and not t.get("offsell") and float(t.get("marketCap") or 0) >= MIN_MCAP]

async def detect_graduations(tokens):
    global cnt_grad
    for t in tokens:
        tid = t["tokenId"]
        sym = t["symbol"].upper()
        if tid not in baselines:
            continue
        was_cex = baselines[tid].get("cex_listed", False)
        now_cex = bool(t.get("listingCex", False))
        if now_cex and not was_cex and tid not in alerted_graduated:
            alerted_graduated.add(tid)
            graduated_ids.add(tid)
            cnt_grad += 1
            baselines[tid]["cex_listed"] = True
            link = trade_link(sym)
            await send(f"🎓 <b>GRADUATED TO BINANCE SPOT!</b>\n━━━━━━━━━━━━━━━\nToken: <b>{sym}</b> ({t.get('name','')})\nChain: {t.get('chainName','')}\n\nMoved from Alpha to Binance Spot.\n\nPrice: {fp(t.get('price'))}\nMarket Cap: {fv(t.get('marketCap'))}\n24h Volume: {fv(t.get('volume24h'))}\n\n📊 <a href='{link}'>Trade {sym} on Binance</a>\n🕐 {now_str()}")
            log.info(f"GRADUATED: {sym}")
        else:
            baselines[tid]["cex_listed"] = now_cex

async def detect_signals(tokens):
    global cnt_vol, cnt_price, cnt_momentum
    for t in tokens:
        tid   = t["tokenId"]
        sym   = t["symbol"].upper()
        cur_p = float(t.get("price", 0) or 0)
        cur_v = float(t.get("volume24h", 0) or 0)
        base  = baselines.get(tid)
        if not base:
            continue
        vol_ratio  = cur_v / base["volume"] if base["volume"] > 0 else 0
        price_gain = ((cur_p - base["price"]) / base["price"] * 100) if base["price"] > 0 else 0
        vol_spike  = vol_ratio  >= VOL_MULTIPLIER
        price_pump = price_gain >= PRICE_PCT
        link       = trade_link(sym)

        if vol_spike and price_pump and is_cooled_down(last_alerted_momentum, tid):
            was_fresh = tid not in last_alerted_momentum
            mark_alerted(last_alerted_momentum, tid)
            if was_fresh: cnt_momentum += 1
            await send(f"🚀 <b>MOMENTUM ALERT</b>\n━━━━━━━━━━━━━━━\nToken: <b>{sym}</b> ({t.get('name','')})\n\n📈 Volume: <b>{vol_ratio:.1f}×</b> above baseline\n   {fv(base['volume'])} → {fv(cur_v)}\n\n💰 Price: <b>{fpct(price_gain)}</b> from entry\n   {fp(base['price'])} → {fp(cur_p)}\n\n📊 <a href='{link}'>Trade {sym} on Binance</a>\n🕐 {now_str()}")
            log.info(f"MOMENTUM: {sym}")

        elif vol_spike and not price_pump and is_cooled_down(last_alerted_volume, tid):
            was_fresh = tid not in last_alerted_volume
            mark_alerted(last_alerted_volume, tid)
            if was_fresh: cnt_vol += 1
            await send(f"📈 <b>VOLUME SPIKE</b>\n━━━━━━━━━━━━━━━\nToken: <b>{sym}</b> ({t.get('name','')})\n\nVolume is <b>{vol_ratio:.1f}×</b> above baseline\n{fv(base['volume'])} → {fv(cur_v)}\nPrice: {fp(cur_p)} ({fpct(price_gain)} from entry)\n\n📊 <a href='{link}'>Trade {sym} on Binance</a>\n🕐 {now_str()}")
            log.info(f"VOLUME: {sym}")

        elif price_pump and not vol_spike and is_cooled_down(last_alerted_price, tid):
            was_fresh = tid not in last_alerted_price
            mark_alerted(last_alerted_price, tid)
            if was_fresh: cnt_price += 1
            await send(f"💰 <b>PRICE PUMP</b>\n━━━━━━━━━━━━━━━\nToken: <b>{sym}</b> ({t.get('name','')})\n\nPrice up <b>{fpct(price_gain)}</b> from listing entry\n{fp(base['price'])} → {fp(cur_p)}\nVolume: {fv(cur_v)}\n\n📊 <a href='{link}'>Trade {sym} on Binance</a>\n🕐 {now_str()}")
            log.info(f"PRICE: {sym}")

async def poll_job():
    global known_ids, cnt_new
    try:
        tokens = fetch_tokens()
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return

    for t in tokens:
        token_map[t["tokenId"]] = t

    if known_ids is None:
        known_ids = {t["tokenId"] for t in tokens}
        for t in tokens:
            baselines[t["tokenId"]] = {"price": float(t.get("price", 0) or 0), "volume": float(t.get("volume24h", 0) or 0), "first_seen": datetime.utcnow(), "symbol": t["symbol"].upper(), "name": t.get("name", ""), "cex_listed": bool(t.get("listingCex", False))}
        log.info(f"Baselines set for {len(tokens)} tokens.")
        await send(f"✅ <b>Alpha Watch Bot is live!</b>\n━━━━━━━━━━━━━━━\nMonitoring <b>{len(tokens)}</b> Binance Alpha tokens\nMin market cap: <b>${MIN_MCAP/1e6:.0f}M</b>\nPoll every <b>{POLL_INTERVAL}s</b>\nVolume alert: <b>{VOL_MULTIPLIER}×</b> baseline\nPrice alert: <b>+{PRICE_PCT}%</b> from entry\nCool-down: <b>{COOLDOWN_HOURS}h</b>\n\nSignals: ⚡ New · 📈 Vol · 💰 Price · 🚀 Momentum · 🎓 Graduation\n\nUse /status to check anytime 📡")
        return

    new_ones = [t for t in tokens if t["tokenId"] not in known_ids]
    for t in new_ones:
        tid = t["tokenId"]
        sym = t["symbol"].upper()
        known_ids.add(tid)
        new_session_ids.add(tid)
        cnt_new += 1
        baselines[tid] = {"price": float(t.get("price", 0) or 0), "volume": float(t.get("volume24h", 0) or 0), "first_seen": datetime.utcnow(), "symbol": sym, "name": t.get("name", ""), "cex_listed": bool(t.get("listingCex", False))}
        link = trade_link(sym)
        await send(f"⚡ <b>NEW ALPHA LISTING</b>\n━━━━━━━━━━━━━━━\nToken: <b>{sym}</b>\nName: {t.get('name','')}\nChain: {t.get('chainName','')}\nPrice: {fp(t.get('price'))}\nMarket Cap: {fv(t.get('marketCap'))}\n\n📡 Watching for momentum\n📊 <a href='{link}'>View {sym} on Binance</a>\n🕐 {now_str()}")
        log.info(f"NEW LISTING: {sym}")

    for t in tokens:
        tid = t["tokenId"]
        if tid not in baselines:
            baselines[tid] = {"price": float(t.get("price", 0) or 0), "volume": float(t.get("volume24h", 0) or 0), "first_seen": datetime.utcnow(), "symbol": t["symbol"].upper(), "name": t.get("name", ""), "cex_listed": bool(t.get("listingCex", False))}

    await detect_graduations(tokens)
    await detect_signals(tokens)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 <b>Alpha Watch Bot</b>\n\nMonitoring Binance Alpha 24/7:\n\n⚡ New token listings\n📈 Volume spikes\n💰 Price pumps\n🚀 Momentum alerts\n🎓 Graduation to Binance Spot\n\n<b>Commands:</b>\n/status — live stats\n/signals — active signals\n/top — top movers\n/settings — thresholds", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 <b>Alpha Watch Status</b>\n━━━━━━━━━━━━━━━\nTokens tracked: <b>{len(baselines)}</b>\nNew listings: <b>{cnt_new}</b>\nVol alerts: <b>{cnt_vol}</b>\nPrice alerts: <b>{cnt_price}</b>\nMomentum alerts: <b>{cnt_momentum}</b>\nGraduations: <b>{cnt_grad}</b> 🎓\n\n⏱ Poll: {POLL_INTERVAL}s\n📈 Vol: {VOL_MULTIPLIER}×\n💰 Price: +{PRICE_PCT}%\n🔄 Cool-down: {COOLDOWN_HOURS}h\n💰 Min mcap: ${MIN_MCAP/1e6:.0f}M\n🕐 {now_str()}", parse_mode="HTML")

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not token_map:
        await update.message.reply_text("Still loading… try again shortly.")
        return
    lines = ["📡 <b>Active Signals</b>\n━━━━━━━━━━━━━━━"]
    found = False
    for tid, t in token_map.items():
        sym  = t["symbol"].upper()
        is_m = tid in last_alerted_momentum
        is_v = tid in last_alerted_volume and not is_m
        is_p = tid in last_alerted_price  and not is_m
        is_g = tid in graduated_ids
        is_n = tid in new_session_ids and not is_m and not is_v and not is_p
        if not (is_m or is_v or is_p or is_g or is_n):
            continue
        found = True
        icon = "🎓" if is_g else "🚀" if is_m else "📈" if is_v else "💰" if is_p else "⚡"
        chg  = float(t.get("percentChange24h", 0) or 0)
        lines.append(f"{icon} <b>{sym}</b> {fpct(chg)} | Vol: {fv(t.get('volume24h'))} | <a href='{trade_link(sym)}'>Trade</a>")
    if not found:
        lines.append("\nNo signals yet this session.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not token_map:
        await update.message.reply_text("Still loading… try again shortly.")
        return
    sorted_tokens = sorted(token_map.values(), key=lambda t: float(t.get("percentChange24h", 0) or 0), reverse=True)
    lines = ["🔥 <b>Top Movers Right Now</b>\n━━━━━━━━━━━━━━━"]
    for t in sorted_tokens[:10]:
        sym = t["symbol"].upper()
        chg = float(t.get("percentChange24h", 0) or 0)
        lines.append(f"<b>{sym}</b> {fpct(chg)} | {fp(t.get('price'))} | Vol {fv(t.get('volume24h'))} | <a href='{trade_link(sym)}'>Trade</a>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚙️ <b>Current Settings</b>\n━━━━━━━━━━━━━━━\nMin market cap: <b>${MIN_MCAP/1e6:.0f}M</b>\nPoll interval: <b>{POLL_INTERVAL}s</b>\nVolume spike: <b>{VOL_MULTIPLIER}×</b>\nPrice pump: <b>+{PRICE_PCT}%</b>\nCool-down: <b>{COOLDOWN_HOURS}h</b>\n\nChange via Railway env vars:\n<code>MIN_MCAP · VOL_MULTIPLIER · PRICE_PCT · POLL_INTERVAL · COOLDOWN_HOURS</code>", parse_mode="HTML")

async def main():
    global bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot = app.bot
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("signals",  cmd_signals))
    app.add_handler(CommandHandler("top",      cmd_top))
    app.add_handler(CommandHandler("settings", cmd_settings))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_job, "interval", seconds=POLL_INTERVAL, id="poll")
    scheduler.start()
    log.info("Alpha Watch Bot starting…")
    await app.initialize()
    await app.start()
    await poll_job()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.updater.idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
