# Takachiho Boat Monitor

Monitors the official EIPRO booking calendar for Takachiho Gorge rental boats and sends a Telegram alert when the configured date appears bookable.

## Current target

- Date: 2026-08-31
- Guests: 2
- Telegram chat ID: configured in workflow
- Booking page: https://eipro.jp/takachiho1/eventCalendars/index

## GitHub Secret required

Add this repository secret:

- `TELEGRAM_BOT_TOKEN` — the token from @BotFather

Do not commit the bot token to the repository.

## Run manually

GitHub → Actions → Takachiho Boat Monitor → Run workflow.

The scheduled workflow checks every 5 minutes.

> Availability is ultimately determined by the official EIPRO booking page. The monitor is an alerting aid; after receiving an alert, open the official page immediately because seats can disappear quickly.
