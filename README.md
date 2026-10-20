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

## Deploy on Northflank
1. Push repo to GitHub (already done).
2. Northflank → New Service → "Deploy from GitHub repo".
3. Build type: Buildpacks (or Dockerfile). Start command: `python -m bot.main` (or rely on `Procfile`).
4. Set environment variables: `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`, `CHANNEL_ID`, and optionally `WEBHOOK_URL`/`PORT`.
5. If using polling: set service type to Worker (no HTTP port/health check).
6. If using webhook: set `WEBHOOK_URL` to your Northflank public URL and keep health check at `/health`.
7. Deploy and check logs for "DB initialized" and polling start.

## Free hosting alternatives
Koyeb/Fly.io via webhook; Oracle Cloud Always Free VM with polling.