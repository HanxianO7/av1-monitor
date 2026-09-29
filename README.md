# Alta Via 1 rifugio monitor

Checks your huts every 4 hours on GitHub (free) and messages you on Telegram **only** when:

- 🟢 dorm beds for 2 appear on your night (Lagazuoi, Coldai, Vazzoler, Carestiato, Passo Duran)
- 🟢 Scotoni shows rooms on 25 Jun (you check if they're dorm beds)
- 🟡 a hut page says 2027 bookings are open (page-watch huts)
- ⚠️ a check has failed 3 runs in a row (so it never dies silently)

Still-open beds get one reminder every 24 h. It never books or pays. Live status is in `STATUS.md`.

## Setup (about 15 min, easiest on a laptop)

1. **Telegram bot**
   - In Telegram, message **@BotFather** → `/newbot` → copy the **token**.
   - Send any message to your new bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser → copy the number after `"chat":{"id":` (your **chat id**).
2. **GitHub repo**
   - Create a **private** repo (e.g. `av1-monitor`).
   - Upload everything in this folder, including the hidden `.github` folder.
3. **Secrets**: repo → Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN` = your token
   - `TELEGRAM_CHAT_ID` = your chat id
4. **Test**: Actions tab → "Alta Via 1 rifugio monitor" → Run workflow → tick *test alert* → you should get a ✅ message.
5. **First real run**: Run workflow again without the tick. Open `STATUS.md` to see results.

That's it. It stops by itself after 30 Jun 2027 (you can also disable it under Actions).

## Settings (`huts.json`)

| Setting | Default | Meaning |
|---|---|---|
| `alerts.portal_live` | `false` | Also alert when a portal opens but your night is full |
| `alerts.page_changes` | `false` | Also alert on any booking-text change on watched pages |
| `alerts.remind_hours` | `24` | Reminder interval while beds stay open (`0` = off) |
| `huts[].pages` | set | Pages watched for "2027 bookings open" text |

To change frequency, edit the `cron` line in `.github/workflows/monitor.yml`.
