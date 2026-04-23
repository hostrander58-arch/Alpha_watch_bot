import os
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ──
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Config ──
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["CHAT_ID"]
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL",   "60"))   # seconds between polls
VOL_MULTIPLIER  = float(os.environ.get("VOL_MULTIPLIER", "5"))   # volume spike multiplier
PRICE_PCT       = float(os.environ.get("PRICE_PCT",      "10"))  # % gain from entry
COOLDOWN_HOURS  = float(os.environ.get("COOLDOWN_HOURS",  "4"))  # hours before re-arming alerts
MIN_MCAP        = float(os.environ.get("MIN_MCAP", "1000000"))   # minimum market cap to monitor ($1M)

BINANCE_API      = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
BINANCE_TRADE    = "https://www.binance.com/en/trade/alpha"   # base for Alpha trade links
BINANCE_ALPHA_MK = "https://www.binance.com/en/markets/alpha-all"

# ── State ──
known_ids        = None   # set of tokenIds seen on first load
baselines        = {}     # tokenId → {price, volume, first_seen, symbol, name, cex_listed}
token_map        = {}     # tokenId → latest token dict (kept fresh each poll)

# Alert tracking: tokenId → datetime last alerted (allows cool-down re-arm)
last_alerted_momentum = {}
last_alerted_volume   = {}
last_alerted_price    = {}
alerted_graduated     = set()   # graduation is one-shot, no cool-down needed

new_session_ids  = set()
graduated_ids    = set()

# Counters for /status
cnt_new = cnt_vol = cnt_price = cnt_momentum = cnt_grad = 0

bot: Bot = None

# ── Formatters ──
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

def trade_link(sym: str) -> str:
    """Generate a direct Binance Alpha trade link for a symbol."""
    return f"https://www.binance.com/en/trade/{sym.upper()}_USDT?type=alpha"

# ── Cool-down helper ──
def is_cooled_down(alert_dict: dict, tid: str) -> bool:
    """Return True if enough time has passed since the last alert for this token."""
    last = alert_dict.get(tid)
    if last is None:
        return True
    return datetime.utcnow() - last > timedelta(hours=COOLDOWN_HOURS)

def mark_alerted(alert_dict: dict, tid: str):
    alert_dict[tid] = datetime.utcnow()

# ── Send ──
async def send(msg: str):
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ── Fetch ──
def fetch_tokens():
    resp = requests.get(BINANCE_API, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [
        t for t in data
        if not t.get("offline")
        and not t.get("offsell")
        and float(t.get("marketCap") or 0) >= MIN_MCAP
    ]

# ── Graduation detection ──
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
            msg = (
                f"🎓 <b>GRADUATED TO BINANCE SPOT!</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Token: <b>{sym}</b> ({t.get('name','')})\n"
                f"Chain: {t.get('chainName','')}\n"
                f"\n"
                f"This token just moved from Alpha → Binance Spot listing.\n"
                f"This is the biggest signal — now tradeable by all Binance users.\n"
                f"\n"
                f"Current Price: {fp(t.get('price'))}\n"
                f"Market Cap: {fv(t.get('marketCap'))}\n"
                f"24h Volume: {fv(t.get('volume24h'))}\n"
                f"\n"
                f"📊 <a href='{link}'>Trade {sym} on Binance</a>\n"
                f"🕐 {now_str()}"
            )
            await send(msg)
            log.info(f"GRADUATED: {sym}")
        else:
            # Keep cex_listed flag current
            baselines[tid]["cex_listed"] = now_cex

# ── Signal detection ──
async def detect_signals(tokens):
    global cnt_vol, cnt_price, cnt_momentum

    for t in tokens:
        tid   = t["tokenId"]
        sym   = t["symbol"].upper()
        cur_p = float(t.get("price",     0) or 0)
        cur_v = float(t.get("volume24h", 0) or 0)
        base  = baselines.get(tid)
        if not base:
            continue

        vol_ratio  = cur_v / base["volume"] if base["volume"] > 0 else 0
        price_gain = ((cur_p - base["price"]) / base["price"] * 100) if base["price"] > 0 else 0

        vol_spike  = vol_ratio  >= VOL_MULTIPLIER
        price_pump = price_gain >= PRICE_PCT
        link       = trade_link(sym)

        # ── 🚀 MOMENTUM — both together ──
        if vol_spike and price_pump and is_cooled_down(last_alerted_momentum, tid):
            # If upgrading from a solo alert, that's fine — momentum overrides
            was_fresh = tid not in last_alerted_momentum
            mark_alerted(last_alerted_momentum, tid)
            if was_fresh:
                cnt_momentum += 1

            msg = (
                f"🚀 <b>MOMENTUM ALERT</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Token: <b>{sym}</b> ({t.get('name','')})\n"
                f"\n"
                f"📈 Volume: <b>{vol_ratio:.1f}×</b> above baseline\n"
                f"   {fv(base['volume'])} → {fv(cur_v)}\n"
                f"\n"
                f"💰 Price: <b>{fpct(price_gain)}</b> from entry\n"
                f"   {fp(base['price'])} → {fp(cur_p)}\n"
                f"\n"
                f"📊 <a href='{link}'>Trade {sym} on Binance</a>\n"
                f"🕐 {now_str()}"
            )
            await send(msg)
            log.info(f"MOMENTUM: {sym} vol={vol_ratio:.1f}x price={fpct(price_gain)}")

        # ── 📈 VOLUME only ──
        elif vol_spike and not price_pump and is_cooled_down(last_alerted_volume, tid):
            was_fresh = tid not in last_alerted_volume
            mark_alerted(last_alerted_volume, tid)
            if was_fresh:
                cnt_vol += 1

            msg = (
                f"📈 <b>VOLUME SPIKE</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Token: <b>{sym}</b> ({t.get('name','')})\n"
                f"\n"
                f"Volume is <b>{vol_ratio:.1f}×</b> above baseline\n"
                f"{fv(base['volume'])} → {fv(cur_v)}\n"
                f"Price: {fp(cur_p)} ({fpct(price_gain)} from entry)\n"
                f"\n"
                f"📊 <a href='{link}'>Trade {sym} on Binance</a>\n"
                f"🕐 {now_str()}"
            )
            await send(msg)
            log.info(f"VOLUME: {sym} {vol_ratio:.1f}x")

        # ── 💰 PRICE only ──
        elif price_pump and not vol_spike and is_cooled_down(last_alerted_price, tid):
            was_fresh = tid not in last_alerted_price
            mark_alerted(last_alerted_price, tid)
            if was_fresh:
                cnt_price += 1

            msg = (
                f"💰 <b>PRICE PUMP</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"Token: <b>{sym}</b> ({t.get('name','')})\n"
                f"\n"
                f"Price up <b>{fpct(price_gain)}</b> from listing entry\n"
                f"{fp(base['price'])} → {fp(cur_p)}\n"
                f"Volume: {fv(cur_v)}\n"
                f"\n"
                f"📊 <a href='{link}'>Trade {sym} on Binance</a>\n"
                f"🕐 {now_str()}"
            )
            await send(msg)
            log.info(f"PRICE: {sym} {fpct(price_gain)}")

# ── Main poll ──
async def poll_job():
    global known_ids, cnt_new

    try:
        tokens = fetch_tokens()
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return

    # Keep token_map fresh for commands
    for t in tokens:
        token_map[t["tokenId"]] = t

    if known_ids is None:
        # First run — set baselines silently
        known_ids = {t["tokenId"] for t in tokens}
        for t in tokens:
            baselines[t["tokenId"]] = {
                "price":      float(t.get("price",     0) or 0),
                "volume":     float(t.get("volume24h", 0) or 0),
                "first_seen": datetime.utcnow(),
                "symbol":     t["symbol"].upper(),
                "name":       t.get("name", ""),
                "cex_listed": bool(t.get("listingCex", False)),
            }
        log.info(f"Baselines set for {len(tokens)} tokens.")
        await send(
            f"✅ <b>Alpha Watch Bot is live!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Monitoring <b>{len(tokens)}</b> Binance Alpha tokens\n"
            f"Min market cap: <b>${MIN_MCAP/1e6:.0f}M</b> (filtering sub-cap tokens)\n"
            f"Poll every <b>{POLL_INTERVAL}s</b>\n"
            f"Volume alert: <b>{VOL_MULTIPLIER}×</b> baseline\n"
            f"Price alert: <b>+{PRICE_PCT}%</b> from entry\n"
            f"Cool-down: <b>{COOLDOWN_HOURS}h</b> before re-arming\n"
            f"\n"
            f"Signals: ⚡ New listing · 📈 Vol spike · 💰 Price pump · 🚀 Momentum · 🎓 Graduation\n"
            f"\n"
            f"Use /status to check anytime 📡"
        )
        return

    # ── New listings ──
    new_ones = [t for t in tokens if t["tokenId"] not in known_ids]
    for t in new_ones:
        tid = t["tokenId"]
        sym = t["symbol"].upper()
        known_ids.add(tid)
        new_session_ids.add(tid)
        cnt_new += 1
        baselines[tid] = {
            "price":      float(t.get("price",     0) or 0),
            "volume":     float(t.get("volume24h", 0) or 0),
            "first_seen": datetime.utcnow(),
            "symbol":     sym,
            "name":       t.get("name", ""),
            "cex_listed": bool(t.get("listingCex", False)),
        }
        link = trade_link(sym)
        msg = (
            f"⚡ <b>NEW ALPHA LISTING</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Token: <b>{sym}</b>\n"
            f"Name: {t.get('name','')}\n"
            f"Chain: {t.get('chainName','')}\n"
            f"Price: {fp(t.get('price'))}\n"
            f"Market Cap: {fv(t.get('marketCap'))}\n"
            f"\n"
            f"📡 Baseline recorded — watching for momentum\n"
            f"📊 <a href='{link}'>View {sym} on Binance</a>\n"
            f"🕐 {now_str()}"
        )
        await send(msg)
        log.info(f"NEW LISTING: {sym}")

    # ── Ensure baselines exist for any tokens added between runs ──
    for t in tokens:
        tid = t["tokenId"]
        if tid not in baselines:
            baselines[tid] = {
                "price":      float(t.get("price",     0) or 0),
                "volume":     float(t.get("volume24h", 0) or 0),
                "first_seen": datetime.utcnow(),
                "symbol":     t["symbol"].upper(),
                "name":       t.get("name", ""),
                "cex_listed": bool(t.get("listingCex", False)),
            }

    # ── Graduation + signal detection ──
    await detect_graduations(tokens)
    await detect_signals(tokens)

# ── Commands ──
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Alpha Watch Bot</b>\n\n"
        "I monitor Binance Alpha 24/7 and alert you when:\n\n"
        "⚡ New token lists on Binance Alpha\n"
        "📈 Volume spikes above baseline\n"
        "💰 Price pumps from listing entry\n"
        "🚀 Both vol AND price fire together\n"
        "🎓 Token graduates to Binance Spot\n\n"
        "Every alert includes a tap-to-trade link.\n"
        "Alerts re-arm after a cool-down period so you catch second waves.\n\n"
        "<b>Commands:</b>\n"
        "/status — live stats\n"
        "/signals — tokens with active signals\n"
        "/settings — current thresholds\n"
        "/top — top movers right now",
        parse_mode="HTML"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cooldown_remaining = {}
    now = datetime.utcnow()
    cd = timedelta(hours=COOLDOWN_HOURS)

    await update.message.reply_text(
        f"📊 <b>Alpha Watch Status</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Tokens tracked: <b>{len(baselines)}</b>\n"
        f"New listings: <b>{cnt_new}</b>\n"
        f"Vol spike alerts: <b>{cnt_vol}</b>\n"
        f"Price pump alerts: <b>{cnt_price}</b>\n"
        f"Momentum alerts: <b>{cnt_momentum}</b>\n"
        f"Graduations: <b>{cnt_grad}</b> 🎓\n"
        f"\n"
        f"⏱ Poll interval: {POLL_INTERVAL}s\n"
        f"📈 Vol threshold: {VOL_MULTIPLIER}×\n"
        f"💰 Price threshold: +{PRICE_PCT}%\n"
        f"🔄 Cool-down: {COOLDOWN_HOURS}h\n"
        f"🕐 {now_str()}",
        parse_mode="HTML"
    )

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not token_map:
        await update.message.reply_text("Still loading… try again in a moment.")
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
        icon  = "🎓" if is_g else "🚀" if is_m else "📈" if is_v else "💰" if is_p else "⚡"
        chg   = float(t.get("percentChange24h", 0) or 0)
        lines.append(f"{icon} <b>{sym}</b> — {fpct(chg)} 24h | Vol: {fv(t.get('volume24h'))} | <a href='{trade_link(sym)}'>Trade</a>")

    if not found:
        lines.append("\nNo signals fired yet this session.\nKeep the bot running — alerts appear here as they fire.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not token_map:
        await update.message.reply_text("Still loading… try again in a moment.")
        return

    # Sort by 24h % change descending
    sorted_tokens = sorted(
        token_map.values(),
        key=lambda t: float(t.get("percentChange24h", 0) or 0),
        reverse=True
    )

    lines = ["🔥 <b>Top Movers Right Now</b>\n━━━━━━━━━━━━━━━"]
    for t in sorted_tokens[:10]:
        sym  = t["symbol"].upper()
        chg  = float(t.get("percentChange24h", 0) or 0)
        vol  = fv(t.get("volume24h"))
        prc  = fp(t.get("price"))
        flag = "🎓" if t.get("listingCex") else "🚀" if sym.lower() in {baselines.get(tid,{}).get("symbol","").lower() for tid in last_alerted_momentum} else ""
        lines.append(f"{flag}<b>{sym}</b> {fpct(chg)} | {prc} | Vol {vol} | <a href='{trade_link(sym)}'>Trade</a>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ <b>Current Settings</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Min market cap: <b>${MIN_MCAP/1e6:.0f}M</b>\n"
        f"Poll interval: <b>{POLL_INTERVAL}s</b>\n"
        f"Volume spike: <b>{VOL_MULTIPLIER}×</b> baseline\n"
        f"Price pump: <b>+{PRICE_PCT}%</b> from entry\n"
        f"Cool-down: <b>{COOLDOWN_HOURS}h</b> before re-arming\n"
        f"\n"
        f"To change, update env vars in Render dashboard:\n"
        f"<code>MIN_MCAP</code> · <code>VOL_MULTIPLIER</code> · <code>PRICE_PCT</code> · <code>POLL_INTERVAL</code> · <code>COOLDOWN_HOURS</code>",
        parse_mode="HTML"
    )

# ── Entry point ──
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
    await poll_job()  # immediate first run
    await app.updater.start_polling(drop_pending_updates=True)
    await app.updater.idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
