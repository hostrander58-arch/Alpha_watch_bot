# Alpha Watch — Telegram Bot Setup Guide

## What you'll get
A bot that messages you on Telegram 24/7 with:
- ⚡ New token listings on Binance Alpha
- 📈 Volume spike alerts (default: 5× baseline)
- 💰 Price pump alerts (default: +10% from entry)
- 🚀 Momentum alerts (both at once — strongest signal)

---

## Step 1 — Create your Telegram bot (2 min)

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Give it a name e.g. `Alpha Watch`
4. Give it a username e.g. `myalphawatch_bot`
5. BotFather gives you a token like:
   `7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   **Copy this — it's your TELEGRAM_TOKEN**

---

## Step 2 — Get your Chat ID (1 min)

1. Search for **@userinfobot** on Telegram
2. Send it any message
3. It replies with your ID e.g. `123456789`
   **Copy this — it's your CHAT_ID**

---

## Step 3 — Deploy on Render (3 min)

1. Go to **github.com** and create a free account if you don't have one
2. Create a **new repository** (click + → New repository)
   - Name it `alpha-watch-bot`
   - Set it to Private
   - Click Create repository
3. Upload the 3 files from this zip:
   - `bot.py`
   - `requirements.txt`
   - `render.yaml`
   (drag them into the GitHub file upload page)

4. Go to **render.com** and sign up free (use GitHub login)
5. Click **New → Blueprint**
6. Connect your `alpha-watch-bot` GitHub repo
7. Render will detect `render.yaml` automatically
8. In the **Environment Variables** section, add:
   - `TELEGRAM_TOKEN` → paste your bot token from Step 1
   - `CHAT_ID` → paste your ID from Step 2
9. Click **Apply** → Deploy

Render will build and start your bot in ~2 minutes.

---

## Step 4 — Test it

1. Open Telegram
2. Search for your bot username (e.g. `@myalphawatch_bot`)
3. Send `/start`
4. You should see the welcome message immediately
5. Send `/status` to confirm it's monitoring

---

## Bot Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message + help |
| `/status` | Live stats — tokens tracked, alerts fired |
| `/tokens` | Shows tokens with active signals |
| `/settings` | Current threshold settings |

---

## Adjust Thresholds

In your Render dashboard → alpha-watch-bot → Environment:

| Variable | Default | What it does |
|----------|---------|-------------|
| `POLL_INTERVAL` | `60` | Seconds between checks |
| `VOL_MULTIPLIER` | `5` | Volume spike threshold (5 = 5× baseline) |
| `PRICE_PCT` | `10` | Price pump threshold (10 = +10% from entry) |

Change any value and click Save — Render restarts the bot automatically.

---

## Free tier notes
Render's free worker tier runs 24/7 with no sleep (unlike web services).
No credit card required.
