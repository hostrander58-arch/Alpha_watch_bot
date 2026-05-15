import os
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import MessageHandler, filters
import json
import websockets

logging.basicConfig(format=”%(asctime)s [%(levelname)s] %(message)s”, level=logging.INFO)
log = logging.getLogger(**name**)

# – Config –

TELEGRAM_TOKEN  = os.environ[“TELEGRAM_TOKEN”]
CHAT_ID         = os.environ[“CHAT_ID”]
POLL_INTERVAL   = int(os.environ.get(“POLL_INTERVAL”,    “60”))
VOL_MULTIPLIER  = float(os.environ.get(“VOL_MULTIPLIER”,  “5”))
PRICE_PCT       = float(os.environ.get(“PRICE_PCT”,       “10”))
COOLDOWN_HOURS  = float(os.environ.get(“COOLDOWN_HOURS”,   “4”))
MIN_MCAP        = float(os.environ.get(“MIN_MCAP”,  “1000000”))
MIN_VOL_USD     = float(os.environ.get(“MIN_VOL_USD”,  “50000”))  # 1. min volume filter
VOL_MCAP_RATIO  = float(os.environ.get(“VOL_MCAP_RATIO”, “20”))   # vol/mcap ratio threshold %
SWEET_SPOT_MIN  = float(os.environ.get(“SWEET_SPOT_MIN”,  “2”))   # sweet spot window start (days)
SWEET_SPOT_MAX  = float(os.environ.get(“SWEET_SPOT_MAX”,  “7”))   # sweet spot window end (days)
WATCH_HOURS     = float(os.environ.get(“WATCH_HOURS”,      “2”))  # 2. watching period before price alerts
CONFIRM_POLLS   = int(os.environ.get(“CONFIRM_POLLS”,      “2”))  # 3. consecutive polls to confirm
HOLDERS_PCT     = float(os.environ.get(“HOLDERS_PCT”,      “20”))  # 4. holders growth %
VOL_ACCEL_PCT   = float(os.environ.get(“VOL_ACCEL_PCT”,    “50”))  # 5. vol acceleration % per poll
NEW_TOKEN_HOURS = float(os.environ.get(“NEW_TOKEN_HOURS”,   “6”))  # 6. ignore price alerts < X hours old

BINANCE_API = “https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list”

# Hyperliquid whale monitoring

HL_WS_URL       = “wss://api.hyperliquid.xyz/ws”
HL_REST_URL     = “https://api.hyperliquid.xyz/info”
MIN_WHALE_USD   = float(os.environ.get(“MIN_WHALE_USD”, “50000”))
HL_ENABLED      = os.environ.get(“HL_ENABLED”, “true”).lower() == “true”

# Binance official Telegram channels to monitor

WATCH_CHANNELS = [
“binance_announcements”,   # official Binance announcements
“BinanceExchange”,         # Binance main channel
]

# Keywords that trigger an alert

ALERT_KEYWORDS = [
“alpha”, “new listing”, “spot listing”, “lists”, “listing”,
“will list”, “is now available”, “spot trading”
]

# – State –

known_ids             = None
baselines             = {}
token_map             = {}
prev_volumes          = {}   # tokenId  volume from last poll (for acceleration)
pending_signals       = {}   # tokenId  {type: count} for confirmation
last_alerted_momentum = {}
last_alerted_volume   = {}
last_alerted_price    = {}
last_alerted_holders  = {}
alerted_graduated     = set()
last_alerted_volmcap  = {}
last_alerted_sweetspot = {}
new_session_ids       = set()
graduated_ids         = set()
cnt_new = cnt_vol = cnt_price = cnt_momentum = cnt_grad = cnt_holders = cnt_volmcap = cnt_sweetspot = cnt_announce = 0
seen_announcement_ids = set()   # track message IDs so we don’t double-alert
whale_alert_count = 0
wallet_history = {}   # wallet -> list of recent trades for accumulation detection
app = None

# – Formatters –

def fp(p):
try:
n = float(p)
if n == 0:       return “–”
if n < 0.000001: return f”${n:.2e}”
if n < 0.01:     return f”${n:.6f}”
if n < 1:        return f”${n:.4f}”
if n < 1000:     return f”${n:.2f}”
return f”${n:,.0f}”
except: return “–”

def fv(v):
try:
n = float(v)
if n >= 1e9: return f”${n/1e9:.1f}B”
if n >= 1e6: return f”${n/1e6:.1f}M”
if n >= 1e3: return f”${n/1e3:.0f}K”
return f”${n:.0f}”
except: return “–”

def fpct(n):
return f”{’+’if n>=0 else ‘’}{n:.1f}%”

def now_str():
return datetime.utcnow().strftime(”%H:%M:%S UTC”)

def date_str(dt):
return dt.strftime(”%b %d, %Y %H:%M UTC”) if dt else “Unknown”

def days_ago(dt):
if not dt: return “”
delta = datetime.utcnow() - dt
if delta.days == 0:
hours = int(delta.seconds / 3600)
return f”{hours}h ago” if hours > 0 else “just now”
if delta.days == 1: return “1 day ago”
return f”{delta.days} days ago”

def trade_link(sym):
return f”https://www.binance.com/en/trade/{sym.upper()}_USDT?type=alpha”

def is_cooled_down(d, tid):
last = d.get(tid)
return last is None or datetime.utcnow() - last > timedelta(hours=COOLDOWN_HOURS)

def mark_alerted(d, tid):
d[tid] = datetime.utcnow()

def hours_watched(tid):
base = baselines.get(tid, {})
fs = base.get(“first_seen”)
if not fs: return 0
return (datetime.utcnow() - fs).total_seconds() / 3600

async def send(msg):
try:
await app.bot.send_message(
chat_id=CHAT_ID, text=msg,
parse_mode=“HTML”, disable_web_page_preview=True
)
except Exception as e:
log.error(f”Send error: {e}”)

def fetch_tokens():
r = requests.get(BINANCE_API, timeout=15)
r.raise_for_status()
return [t for t in r.json().get(“data”, [])
if not t.get(“offline”)
and not t.get(“offsell”)
and float(t.get(“marketCap”) or 0) >= MIN_MCAP]

def make_base(t, listed_date=None):
now = datetime.utcnow()
return {
“price”:          float(t.get(“price”, 0) or 0),
“volume”:         float(t.get(“volume24h”, 0) or 0),
“volume_updated”: now,
“holders”:        int(t.get(“holders”, 0) or 0),
“first_seen”:     now,
“listed_date”:    listed_date or now,
“symbol”:         t[“symbol”].upper(),
“name”:           t.get(“name”, “”),
“cex_listed”:     bool(t.get(“listingCex”, False)),
}

# – Confirmation helper –

def confirm_signal(tid, sig_type):
“”“Returns True when signal has been seen CONFIRM_POLLS times in a row.”””
key = f”{tid}:{sig_type}”
pending_signals[key] = pending_signals.get(key, 0) + 1
return pending_signals[key] >= CONFIRM_POLLS

def reset_signal(tid, sig_type):
pending_signals.pop(f”{tid}:{sig_type}”, None)

# – 24h volume refresh –

async def refresh_volume_baselines():
count = 0
try:
tokens = fetch_tokens()
for t in tokens:
tid = t[“tokenId”]
if tid in baselines:
baselines[tid][“volume”] = float(t.get(“volume24h”, 0) or 0)
baselines[tid][“volume_updated”] = datetime.utcnow()
count += 1
log.info(f”Volume baselines refreshed for {count} tokens.”)
await send(
f” <b>Volume Baselines Refreshed</b>\n”
f”—————\n”
f”Updated {count} token baselines to current 24h volume.\n”
f” {now_str()}”
)
except Exception as e:
log.error(f”Volume refresh failed: {e}”)

# – Daily 9am briefing – (8. daily briefing)

async def daily_briefing():
lines = [f” <b>Daily Alpha Briefing</b>\n—————\n {now_str()}\n”]

```
# New listings in last 24h
recent_new = [
    b for b in baselines.values()
    if b.get("listed_date") and (datetime.utcnow() - b["listed_date"]).total_seconds() < 86400
    and b["symbol"] in {t["symbol"].upper() for t in token_map.values() if t["tokenId"] in new_session_ids}
]
lines.append(f" <b>New listings (24h):</b> {len(recent_new)}")

# Top 5 movers
sorted_tokens = sorted(
    token_map.values(),
    key=lambda t: float(t.get("percentChange24h", 0) or 0),
    reverse=True
)
lines.append(f"\n <b>Top 5 movers:</b>")
for t in sorted_tokens[:5]:
    sym = t["symbol"].upper()
    chg = float(t.get("percentChange24h", 0) or 0)
    lines.append(f"   <b>{sym}</b> {fpct(chg)} | {fp(t.get('price'))}")

# Signals summary
lines.append(f"\n <b>Session totals:</b>")
lines.append(f"  New: {cnt_new} | Vol: {cnt_vol} | Price: {cnt_price}")
lines.append(f"  Momentum: {cnt_momentum} | Grad: {cnt_grad} | Holders: {cnt_holders}")

# Graduations
if graduated_ids:
    grad_syms = [baselines.get(tid, {}).get("symbol", tid) for tid in graduated_ids]
    lines.append(f"\n <b>Graduated:</b> {', '.join(grad_syms)}")

await send("\n".join(lines))
log.info("Daily briefing sent.")
```

# – Graduation detection –

async def detect_graduations(tokens):
global cnt_grad
for t in tokens:
tid, sym = t[“tokenId”], t[“symbol”].upper()
if tid not in baselines: continue
was = baselines[tid].get(“cex_listed”, False)
now_cex = bool(t.get(“listingCex”, False))
if now_cex and not was and tid not in alerted_graduated:
alerted_graduated.add(tid)
graduated_ids.add(tid)
cnt_grad += 1
baselines[tid][“cex_listed”] = True
listed = baselines[tid].get(“listed_date”)
days_on = (datetime.utcnow() - listed).days if listed else “?”
await send(
f” <b>GRADUATED TO BINANCE SPOT!</b>\n”
f”—————\n”
f”Token: <b>{sym}</b> ({t.get(‘name’,’’)})\n”
f”Chain: {t.get(‘chainName’,’’)}\n\n”
f”Listed on Alpha: {date_str(listed)}\n”
f”Days on Alpha: <b>{days_on}</b>\n\n”
f”Price: {fp(t.get(‘price’))}\n”
f”Market Cap: {fv(t.get(‘marketCap’))}\n”
f”24h Volume: {fv(t.get(‘volume24h’))}\n\n”
f” <a href='{trade_link(sym)}'>Trade {sym} on Binance</a>\n”
f” {now_str()}”
)
log.info(f”GRADUATED: {sym}”)
else:
baselines[tid][“cex_listed”] = now_cex

# – Signal detection –

async def detect_signals(tokens):
global cnt_vol, cnt_price, cnt_momentum, cnt_holders

```
for t in tokens:
    tid  = t["tokenId"]
    sym  = t["symbol"].upper()
    cur_p = float(t.get("price",     0) or 0)
    cur_v = float(t.get("volume24h", 0) or 0)
    cur_h = int(t.get("holders",     0) or 0)
    base  = baselines.get(tid)
    if not base: continue

    lk      = trade_link(sym)
    listed  = base.get("listed_date")
    watched = hours_watched(tid)

    # -- 1. Minimum volume filter --
    if cur_v < MIN_VOL_USD:
        reset_signal(tid, "vol")
        reset_signal(tid, "momentum")
        continue

    vr = cur_v / base["volume"] if base["volume"] > 0 else 0
    pg = ((cur_p - base["price"]) / base["price"] * 100) if base["price"] > 0 else 0
    vs = vr >= VOL_MULTIPLIER
    pp = pg >= PRICE_PCT and watched >= WATCH_HOURS  # 2. watching period
    lk = trade_link(sym)

    # -- 5. Volume acceleration --
    prev_v = prev_volumes.get(tid, cur_v)
    vol_accel = ((cur_v - prev_v) / prev_v * 100) if prev_v > 0 else 0
    vol_accelerating = vol_accel >= VOL_ACCEL_PCT and vs

    # -- 6. Token age filter -- skip price alerts for very new tokens --
    if watched < NEW_TOKEN_HOURS:
        pp = False

    # -- 9. Deduplication -- if momentum fires, suppress solo alerts --
    momentum_fired = tid in last_alerted_momentum and not is_cooled_down(last_alerted_momentum, tid)

    #  MOMENTUM
    if vs and pp and is_cooled_down(last_alerted_momentum, tid):
        if confirm_signal(tid, "momentum"):
            reset_signal(tid, "momentum")
            reset_signal(tid, "vol")
            reset_signal(tid, "price")
            if tid not in last_alerted_momentum: cnt_momentum += 1
            mark_alerted(last_alerted_momentum, tid)
            # 9. suppress solo alerts
            last_alerted_volume[tid] = datetime.utcnow()
            last_alerted_price[tid]  = datetime.utcnow()
            accel_note = f"\n Vol accelerating +{vol_accel:.0f}% this poll!" if vol_accelerating else ""
            await send(
                f" <b>MOMENTUM ALERT</b>\n"
                f"---------------\n"
                f"Token: <b>{sym}</b> ({base.get('name','')})\n"
                f"Listed on Alpha: {date_str(listed)} ({days_ago(listed)})\n"
                f"Watched for: {watched:.1f}h\n"
                f"{accel_note}\n"
                f" Vol: <b>{vr:.1f}</b> baseline | {fv(base['volume'])}  {fv(cur_v)}\n"
                f" Price: <b>{fpct(pg)}</b> from entry | {fp(base['price'])}  {fp(cur_p)}\n\n"
                f" <a href='{lk}'>Trade {sym} on Binance</a>\n"
                f" {now_str()}"
            )
            log.info(f"MOMENTUM: {sym}")
    else:
        if not (vs and pp): reset_signal(tid, "momentum")

    #  VOLUME (skip if momentum recently fired)
    if vs and not momentum_fired and is_cooled_down(last_alerted_volume, tid):
        if confirm_signal(tid, "vol"):
            reset_signal(tid, "vol")
            if tid not in last_alerted_volume: cnt_vol += 1
            mark_alerted(last_alerted_volume, tid)
            accel_note = f"\n Accelerating +{vol_accel:.0f}% this poll!" if vol_accelerating else ""
            await send(
                f" <b>VOLUME SPIKE</b>\n"
                f"---------------\n"
                f"Token: <b>{sym}</b> ({base.get('name','')})\n"
                f"Listed on Alpha: {date_str(listed)} ({days_ago(listed)})\n"
                f"{accel_note}\n"
                f"Vol: <b>{vr:.1f}</b> baseline\n"
                f"{fv(base['volume'])}  {fv(cur_v)}\n"
                f"Price: {fp(cur_p)} ({fpct(pg)} from entry)\n\n"
                f" <a href='{lk}'>Trade {sym} on Binance</a>\n"
                f" {now_str()}"
            )
            log.info(f"VOLUME: {sym}")
    else:
        if not vs: reset_signal(tid, "vol")

    #  PRICE (skip if momentum recently fired)
    if pp and not momentum_fired and is_cooled_down(last_alerted_price, tid):
        if confirm_signal(tid, "price"):
            reset_signal(tid, "price")
            if tid not in last_alerted_price: cnt_price += 1
            mark_alerted(last_alerted_price, tid)
            await send(
                f" <b>PRICE PUMP</b>\n"
                f"---------------\n"
                f"Token: <b>{sym}</b> ({base.get('name','')})\n"
                f"Listed on Alpha: {date_str(listed)} ({days_ago(listed)})\n"
                f"Watched for: {watched:.1f}h\n\n"
                f"Up <b>{fpct(pg)}</b> from entry\n"
                f"{fp(base['price'])}  {fp(cur_p)}\n"
                f"Volume: {fv(cur_v)}\n\n"
                f" <a href='{lk}'>Trade {sym} on Binance</a>\n"
                f" {now_str()}"
            )
            log.info(f"PRICE: {sym}")
    else:
        if not pp: reset_signal(tid, "price")

    #  HOLDERS GROWTH (4.)
    base_h = base.get("holders", 0)
    if base_h > 0 and cur_h > 0:
        h_growth = ((cur_h - base_h) / base_h) * 100
        if h_growth >= HOLDERS_PCT and is_cooled_down(last_alerted_holders, tid):
            mark_alerted(last_alerted_holders, tid)
            cnt_holders += 1
            await send(
                f" <b>HOLDERS SURGE</b>\n"
                f"---------------\n"
                f"Token: <b>{sym}</b> ({base.get('name','')})\n"
                f"Listed on Alpha: {date_str(listed)} ({days_ago(listed)})\n\n"
                f"Holders up <b>{fpct(h_growth)}</b>\n"
                f"{base_h:,}  {cur_h:,} holders\n"
                f"Price: {fp(cur_p)} | Vol: {fv(cur_v)}\n\n"
                f" Early accumulation signal\n"
                f" <a href='{lk}'>View {sym} on Binance</a>\n"
                f" {now_str()}"
            )
            log.info(f"HOLDERS: {sym} +{h_growth:.1f}%")

    # -- Vol/Mcap ratio alert --
    mcap = float(t.get("marketCap", 0) or 0)
    if mcap > 0 and cur_v > 0:
        vm_ratio = (cur_v / mcap) * 100
        if vm_ratio >= VOL_MCAP_RATIO and is_cooled_down(last_alerted_volmcap, tid):
            mark_alerted(last_alerted_volmcap, tid)
            cnt_volmcap += 1
            await send(
                f"fire <b>VOL/MCAP SURGE</b>\n"
                f"---\n"
                f"Token: <b>{sym}</b> ({base.get('name','')})\n"
                f"Listed on Alpha: {date_str(listed)} ({days_ago(listed)})\n\n"
                f"Volume is <b>{vm_ratio:.0f}%</b> of market cap\n"
                f"Vol: {fv(cur_v)} | MCap: {fv(mcap)}\n"
                f"Price: {fp(cur_p)}\n\n"
                f"High vol/mcap = intense buying relative to size\n"
                f"chart <a href='{lk}'>Trade {sym} on Binance</a>\n"
                f"clock {now_str()}"
            )
            log.info(f"VOL/MCAP: {sym} {vm_ratio:.0f}%")

    # -- Sweet spot alert (2-7 days since listing) --
    if listed:
        age_days = (datetime.utcnow() - listed).total_seconds() / 86400
        in_sweet_spot = SWEET_SPOT_MIN <= age_days <= SWEET_SPOT_MAX
        if in_sweet_spot and vs and is_cooled_down(last_alerted_sweetspot, tid):
            mark_alerted(last_alerted_sweetspot, tid)
            cnt_sweetspot += 1
            await send(
                f"star <b>SWEET SPOT ALERT</b>\n"
                f"---\n"
                f"Token: <b>{sym}</b> ({base.get('name','')})\n"
                f"Listed: {date_str(listed)}\n"
                f"Age: <b>{age_days:.1f} days</b> (prime 2-7 day window)\n\n"
                f"Volume spike during sweet spot window\n"
                f"Vol: {fv(cur_v)} ({vr:.1f}x baseline)\n"
                f"Price: {fp(cur_p)} ({fpct(pg)} from entry)\n\n"
                f"chart <a href='{lk}'>Trade {sym} on Binance</a>\n"
                f"clock {now_str()}"
            )
            log.info(f"SWEET SPOT: {sym} age={age_days:.1f}d")

    # Update holders baseline and prev volume
    baselines[tid]["holders"] = cur_h
    prev_volumes[tid] = cur_v
```

# – Main poll –

async def poll_job():
global known_ids, cnt_new
try:
tokens = fetch_tokens()
except Exception as e:
log.error(f”Fetch failed: {e}”); return

```
for t in tokens:
    token_map[t["tokenId"]] = t

if known_ids is None:
    known_ids = {t["tokenId"] for t in tokens}
    for t in tokens:
        baselines[t["tokenId"]] = make_base(t)
        prev_volumes[t["tokenId"]] = float(t.get("volume24h", 0) or 0)
        # Pre-mark already-graduated tokens -- never alert on these
        if bool(t.get("listingCex", False)):
            alerted_graduated.add(t["tokenId"])
            graduated_ids.add(t["tokenId"])
    already_grad = len(alerted_graduated)
    log.info(f"Baselines set for {len(tokens)} tokens. Suppressed {already_grad} already-graduated tokens.")
    await send(
        f" <b>Alpha Watch Bot is live!</b>\n"
        f"---------------\n"
        f"Tracking <b>{len(tokens)}</b> tokens (min ${MIN_MCAP/1e6:.0f}M mcap)\n"
        f"Poll: <b>{POLL_INTERVAL}s</b> | Vol: <b>{VOL_MULTIPLIER}</b> | Price: <b>+{PRICE_PCT}%</b>\n"
        f"Min volume: <b>{fv(MIN_VOL_USD)}</b> | Watch period: <b>{WATCH_HOURS}h</b>\n"
        f"Confirmation: <b>{CONFIRM_POLLS} polls</b> | Age filter: <b>{NEW_TOKEN_HOURS}h</b>\n"
        f"Vol refresh: every <b>24h</b> | Briefing: <b>9am UTC daily</b>\n\n"
        f"Signals: New  Vol  Price  Momentum  Holders  Grad\n\n"
        f"/status /signals /top /price /settings "
    )
    return

# New listings
for t in tokens:
    tid = t["tokenId"]
    if tid not in known_ids:
        sym = t["symbol"].upper()
        known_ids.add(tid)
        new_session_ids.add(tid)
        cnt_new += 1
        listed_date = datetime.utcnow()
        baselines[tid] = make_base(t, listed_date=listed_date)
        prev_volumes[tid] = float(t.get("volume24h", 0) or 0)
        await send(
            f" <b>NEW BINANCE ALPHA LISTING</b>\n"
            f"---------------\n"
            f"Token: <b>{sym}</b>\n"
            f"Name: {t.get('name','')}\n"
            f"Chain: {t.get('chainName','')}\n\n"
            f"Listed: <b>{date_str(listed_date)}</b>\n\n"
            f"Price: {fp(t.get('price'))}\n"
            f"Market Cap: {fv(t.get('marketCap'))}\n"
            f"24h Volume: {fv(t.get('volume24h'))}\n"
            f"Holders: {int(t.get('holders',0) or 0):,}\n\n"
            f" Baseline set -- watching for momentum\n"
            f" <a href='{trade_link(sym)}'>View {sym} on Binance Alpha</a>\n"
            f" {now_str()}"
        )
        log.info(f"NEW LISTING: {sym}")
    elif tid not in baselines:
        baselines[tid] = make_base(t)
        prev_volumes[tid] = float(t.get("volume24h", 0) or 0)

await detect_graduations(tokens)
await detect_signals(tokens)
```

# – Commands –

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
await u.message.reply_text(
“ <b>Alpha Watch Bot</b>\n\n”
“Monitoring Binance Alpha 24/7:\n\n”
“ New Alpha listings\n”
“ Volume spikes (confirmed  2 polls)\n”
“ Price pumps (after 2h watch period)\n”
“ Momentum (vol + price together)\n”
“ Holders surge (early accumulation)\n”
“ Graduation to Binance Spot\n”
“ Daily 9am UTC briefing\n\n”
“<b>Commands:</b>\n”
“/status – live stats\n”
“/signals – active signals\n”
“/top – top movers now\n”
“/price SYM – instant price lookup\n”
“/settings – thresholds”,
parse_mode=“HTML”
)

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
vol_ages = [b.get(“volume_updated”) for b in baselines.values() if b.get(“volume_updated”)]
last_refresh = min(vol_ages) if vol_ages else None
next_refresh = (last_refresh + timedelta(hours=24)).strftime(”%H:%M UTC”) if last_refresh else “–”
await u.message.reply_text(
f” <b>Alpha Watch Status</b>\n”
f”—————\n”
f”Tracking: <b>{len(baselines)}</b> tokens\n”
f”New: <b>{cnt_new}</b> | Vol: <b>{cnt_vol}</b> | Price: <b>{cnt_price}</b>\n”
f”Momentum: <b>{cnt_momentum}</b> | Holders: <b>{cnt_holders}</b>\n”
f”Vol/MCap: <b>{cnt_volmcap}</b> | Sweet spot: <b>{cnt_sweetspot}</b>\n”
f”Grad: <b>{cnt_grad}</b> | Announcements: <b>{cnt_announce}</b>\n”
f”Whale alerts: <b>{whale_alert_count}</b>\n\n”
f” Poll: {POLL_INTERVAL}s | Confirm: {CONFIRM_POLLS} polls\n”
f” Vol: {VOL_MULTIPLIER} | Min vol: {fv(MIN_VOL_USD)}\n”
f” Price: +{PRICE_PCT}% | Watch: {WATCH_HOURS}h | Age: {NEW_TOKEN_HOURS}h\n”
f” Next vol refresh: {next_refresh}\n”
f” {now_str()}”,
parse_mode=“HTML”
)

async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
lines = [” <b>Active Signals</b>\n—————”]
found = False
for tid, t in token_map.items():
sym  = t[“symbol”].upper()
is_m = tid in last_alerted_momentum
is_v = tid in last_alerted_volume and not is_m
is_p = tid in last_alerted_price  and not is_m
is_g = tid in graduated_ids
is_h = tid in last_alerted_holders
is_n = tid in new_session_ids and not any([is_m, is_v, is_p])
if not any([is_m, is_v, is_p, is_g, is_h, is_n]): continue
found = True
icon = “” if is_g else “” if is_m else “” if is_v else “” if is_p else “” if is_h else “”
chg  = float(t.get(“percentChange24h”, 0) or 0)
listed = baselines.get(tid, {}).get(“listed_date”)
lines.append(f”{icon} <b>{sym}</b> {fpct(chg)} | {days_ago(listed)} | <a href='{trade_link(sym)}'>Trade</a>”)
if not found:
lines.append(”\nNo signals yet this session.”)
await u.message.reply_text(”\n”.join(lines), parse_mode=“HTML”, disable_web_page_preview=True)

async def cmd_top(u: Update, c: ContextTypes.DEFAULT_TYPE):
if not token_map:
await u.message.reply_text(“Still loading”); return
tokens = sorted(token_map.values(), key=lambda t: float(t.get(“percentChange24h”, 0) or 0), reverse=True)
lines = [” <b>Top Movers Right Now</b>\n—————”]
for t in tokens[:10]:
sym  = t[“symbol”].upper()
chg  = float(t.get(“percentChange24h”, 0) or 0)
listed = baselines.get(t[“tokenId”], {}).get(“listed_date”)
lines.append(f”<b>{sym}</b> {fpct(chg)} | {fp(t.get(‘price’))} | {days_ago(listed)} | <a href='{trade_link(sym)}'>Trade</a>”)
await u.message.reply_text(”\n”.join(lines), parse_mode=“HTML”, disable_web_page_preview=True)

async def cmd_price(u: Update, c: ContextTypes.DEFAULT_TYPE):
“”“7. /price SYM – instant price lookup”””
args = c.args
if not args:
await u.message.reply_text(“Usage: /price SYMBOL\nExample: /price TRIA”)
return
sym = args[0].upper()
match = next((t for t in token_map.values() if t[“symbol”].upper() == sym), None)
if not match:
await u.message.reply_text(f”Token {sym} not found in Alpha list.”)
return
tid    = match[“tokenId”]
listed = baselines.get(tid, {}).get(“listed_date”)
base   = baselines.get(tid, {})
cur_p  = float(match.get(“price”, 0) or 0)
entry  = base.get(“price”, 0)
since_entry = ((cur_p - entry) / entry * 100) if entry > 0 else 0
await u.message.reply_text(
f” <b>{sym}</b> – {match.get(‘name’,’’)}\n”
f”—————\n”
f”Price: <b>{fp(cur_p)}</b>\n”
f”24h Change: {fpct(float(match.get(‘percentChange24h’, 0) or 0))}\n”
f”Since listing entry: {fpct(since_entry)}\n\n”
f”Market Cap: {fv(match.get(‘marketCap’))}\n”
f”24h Volume: {fv(match.get(‘volume24h’))}\n”
f”Holders: {int(match.get(‘holders’, 0) or 0):,}\n”
f”Chain: {match.get(‘chainName’,’’)}\n\n”
f”Listed on Alpha: {date_str(listed)} ({days_ago(listed)})\n\n”
f” <a href='{trade_link(sym)}'>Trade {sym} on Binance</a>”,
parse_mode=“HTML”, disable_web_page_preview=True
)

async def cmd_settings(u: Update, c: ContextTypes.DEFAULT_TYPE):
await u.message.reply_text(
f” <b>Current Settings</b>\n”
f”—————\n”
f”Min market cap: ${MIN_MCAP/1e6:.0f}M\n”
f”Min volume: {fv(MIN_VOL_USD)}\n”
f”Poll interval: {POLL_INTERVAL}s\n”
f”Confirmation: {CONFIRM_POLLS} polls\n”
f”Vol spike: {VOL_MULTIPLIER}\n”
f”Vol acceleration: +{VOL_ACCEL_PCT}% per poll\n”
f”Price pump: +{PRICE_PCT}%\n”
f”Watch period: {WATCH_HOURS}h\n”
f”New token age filter: {NEW_TOKEN_HOURS}h\n”
f”Holders surge: +{HOLDERS_PCT}%\n”
f”Vol/MCap ratio: {VOL_MCAP_RATIO}%\n”
f”Sweet spot window: {SWEET_SPOT_MIN}-{SWEET_SPOT_MAX} days\n”
f”Cool-down: {COOLDOWN_HOURS}h\n\n”
f”Change in Railway env vars:\n”
f”<code>MIN_MCAP  MIN_VOL_USD  VOL_MULTIPLIER  PRICE_PCT\n”
f”POLL_INTERVAL  COOLDOWN_HOURS  WATCH_HOURS\n”
f”CONFIRM_POLLS  HOLDERS_PCT  VOL_ACCEL_PCT  NEW_TOKEN_HOURS</code>”,
parse_mode=“HTML”
)

def hl_short(addr):
if addr and len(addr) > 10:
return addr[:6] + “…” + addr[-4:]
return addr or “”

async def hl_send_whale_alert(trade, cross_alpha=False):
global whale_alert_count
whale_alert_count += 1
coin    = trade.get(“coin”, “”).upper()
side    = trade.get(“side”, “”)
size    = float(trade.get(“sz”, 0))
price   = float(trade.get(“px”, 0))
usd_val = size * price
users   = trade.get(“users”, [])
wallet  = users[0] if users else trade.get(“user”, “”)
action  = “BUY” if side == “B” else “SELL”
tag     = “ [ALPHA MATCH]” if cross_alpha else “”

```
if wallet:
    if wallet not in wallet_history:
        wallet_history[wallet] = []
    wallet_history[wallet].append({
        "coin": coin, "side": side,
        "usd": usd_val, "time": datetime.utcnow()
    })
    wallet_history[wallet] = wallet_history[wallet][-10:]

accum_note = ""
if wallet and wallet in wallet_history:
    recent_buys = [
        t for t in wallet_history[wallet]
        if t["coin"] == coin
        and t["side"] == "B"
        and (datetime.utcnow() - t["time"]).total_seconds() < 86400
    ]
    if len(recent_buys) >= 3:
        total = sum(t["usd"] for t in recent_buys)
        accum_note = "ACCUMULATION: " + str(len(recent_buys)) + " buys = " + fv(total) + " in 24h"

lines = [
    "HYPERLIQUID WHALE" + tag,
    "---",
    "Token: " + coin,
    "Action: " + action + " " + fv(usd_val),
    "Wallet: " + hl_short(wallet),
    "Size: " + f"{size:,.0f}" + " " + coin,
    "Price: " + fp(price),
]
if accum_note:
    lines.append(accum_note)
lines.append(now_str())
await send("\n".join(lines))
log.info("WHALE: " + action + " " + coin + " " + fv(usd_val))
```

async def hl_monitor_trades():
if not HL_ENABLED:
return
log.info(“Connecting to Hyperliquid WebSocket…”)
while True:
try:
async with websockets.connect(HL_WS_URL, ping_interval=30) as ws:
await ws.send(json.dumps({
“method”: “subscribe”,
“subscription”: {“type”: “trades”, “coin”: “”}
}))
log.info(“Hyperliquid WebSocket connected”)
await send(
“HYPERLIQUID MONITOR ACTIVE\n”
“—\n”
“Watching all spot + perp trades\n”
“Alert threshold: “ + fv(MIN_WHALE_USD) + “\n” +
now_str()
)
async for raw in ws:
try:
data = json.loads(raw)
if data.get(“channel”) != “trades”:
continue
trades = data.get(“data”, [])
if isinstance(trades, dict):
trades = [trades]
for trade in trades:
sz  = float(trade.get(“sz”, 0))
px  = float(trade.get(“px”, 0))
usd = sz * px
if usd < MIN_WHALE_USD:
continue
coin = trade.get(“coin”, “”).upper()
alpha_match = any(
t.get(“symbol”, “”).upper() == coin
for t in token_map.values()
)
await hl_send_whale_alert(trade, cross_alpha=alpha_match)
except Exception as e:
log.error(“HL parse error: “ + str(e))
except Exception as e:
log.error(“HL WebSocket error: “ + str(e) + “ - reconnecting in 10s”)
await asyncio.sleep(10)

async def handle_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
“”“Monitor Binance announcement channels for listing news.”””
global cnt_announce
msg = update.channel_post or update.message
if not msg or not msg.text:
return

```
# Only process from Binance channels
chat = msg.chat
username = (chat.username or "").lower()
if not any(ch.lower() in username for ch in WATCH_CHANNELS):
    return

# Deduplicate
msg_id = f"{chat.id}:{msg.message_id}"
if msg_id in seen_announcement_ids:
    return
seen_announcement_ids.add(msg_id)

text = msg.text.lower()

# Check for relevant keywords
matched = [kw for kw in ALERT_KEYWORDS if kw in text]
if not matched:
    return

cnt_announce += 1

# Detect type
if "alpha" in text:
    icon = ""
    alert_type = "BINANCE ALPHA ANNOUNCEMENT"
elif "spot" in text or "listing" in text:
    icon = ""
    alert_type = "BINANCE SPOT LISTING"
else:
    icon = ""
    alert_type = "BINANCE ANNOUNCEMENT"

# Try to extract token symbol (usually in caps e.g. $BTC or ALL CAPS word)
import re
symbols = re.findall(r'\b[A-Z]{2,10}\b', msg.text)
# Filter out common non-token words
ignore = {"THE","AND","FOR","NEW","NOW","HAS","ARE","ALL","USD","USDT","BNB","YOU","CAN","THIS"}
symbols = [s for s in symbols if s not in ignore]
sym_line = f"Tokens mentioned: <b>{', '.join(symbols[:5])}</b>\n" if symbols else ""

# Build preview (first 280 chars)
preview = msg.text[:280] + ("..." if len(msg.text) > 280 else "")

await send(
    f"{icon} <b>{alert_type}</b>\n"
    f"---\n"
    f"{sym_line}"
    f"\n{preview}\n\n"
    f"Source: @{chat.username}\n"
    f"clock {now_str()}"
)
log.info(f"ANNOUNCEMENT: {alert_type} from @{chat.username}")
```

async def main():
global app
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler(“start”,    cmd_start))
app.add_handler(CommandHandler(“status”,   cmd_status))
app.add_handler(CommandHandler(“signals”,  cmd_signals))
app.add_handler(CommandHandler(“top”,      cmd_top))
app.add_handler(CommandHandler(“price”,    cmd_price))
app.add_handler(CommandHandler(“settings”, cmd_settings))

```
# Monitor Binance announcement channels
app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

scheduler = AsyncIOScheduler()
scheduler.add_job(poll_job,                  "interval", seconds=POLL_INTERVAL, id="poll")
scheduler.add_job(refresh_volume_baselines,  "interval", hours=24,              id="vol_refresh")
scheduler.add_job(daily_briefing,            "cron",     hour=9, minute=0,      id="briefing")
scheduler.start()

log.info("Alpha Watch Bot starting")
async with app:
    await app.start()
    await poll_job()
    await app.updater.start_polling(drop_pending_updates=True)
    # Run Hyperliquid monitor concurrently
    await asyncio.gather(
        asyncio.Event().wait(),
        hl_monitor_trades()
    )
```

if **name** == “**main**”:
asyncio.run(main())