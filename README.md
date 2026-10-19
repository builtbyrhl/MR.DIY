# FMGE Lecture Telegram Bot

Python (aiogram 3) + PostgreSQL + (optional) Redis.

## Setup
1. Copy `.env.example` to `.env` and fill `BOT_TOKEN`, `ADMIN_IDS`, `CHANNEL_ID`.
2. Install deps: `pip install -r requirements.txt`
3. Run: `python -m bot.main` (polling) or set `WEBHOOK_URL` for webhook mode.
4. Docker: `docker-compose up`

## Admin
- `/admin` for commands.
- Add lectures: `/addlecture subject_slug|title|channel_msg_id|[premium]`
- Content is forwarded from `CHANNEL_ID` (no external storage).

## Free hosting
Render/Koyeb free tier via webhook mode; DB on Supabase free; or Oracle Cloud Always Free VM with polling.
