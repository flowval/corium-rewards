# Corium points Telegram bot

Tracks `cosmi` and `hnnicalls` on the Hunters and Community boards at
https://corium.so/points and reports SOL per account, per board and combined.

## Commands
- `/update` - check now, show individual and combined totals
- `/notify` / `/stop` - turn change alerts on/off for this chat
- `/myid` - show your chat id
- `/debug` - show raw matched rows and JSON endpoints the page calls

## Environment variables
| Name | Required | Purpose |
|------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | yes | Token from @BotFather |
| `NOTIFY_CHAT_ID` | recommended | Chat id(s), comma separated. Gets alerts; only these chats can use the bot |
| `CHECK_MINUTES` | no | Auto-check interval (default 10) |

## Run locally
    pip install -r requirements.txt
    playwright install chromium
    export TELEGRAM_BOT_TOKEN="..."
    python corium_bot.py

## Deploy on Render
Create a Background Worker (always on) or a free Web Service (sleeps after
15 min idle; ping its URL every 10 min to keep it awake), choose Docker, connect this repo, and set
the environment variables above. Never commit your token.
