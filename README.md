# FMGE Lecture Telegram Bot (MR.DIY)

Python (aiogram 3) + PostgreSQL.

## Setup
1. Copy `.env.example` to `.env` and fill `BOT_TOKEN`, `ADMIN_IDS`, `CHANNEL_ID`.
2. Install deps: `pip install -r requirements.txt`
3. Run: `python -m bot.main` (polling) or set `WEBHOOK_URL` for webhook mode.
4. Docker: `docker-compose up`

## Admin
- `/admin` for commands.
- Add lectures: `/addlecture subject_slug|title|channel_msg_id|[premium]`
- `/reorder <subject_slug>` to sort by channel sequence.
- Content is forwarded from `CHANNEL_ID`.

## Deploy on Render
1. Push repo to GitHub.
2. Render → New → Blueprint → select repo (uses `render.yaml`).
3. Set env vars: `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL` (Supabase/Neon free Postgres), `CHANNEL_ID`.
4. Render sets `RENDER_EXTERNAL_URL` → webhook auto-configured. Health check at `/health`.
5. Make bot admin in your channel/group.

## Free hosting alternatives
Koyeb/Fly.io via webhook; Oracle Cloud Always Free VM with polling.