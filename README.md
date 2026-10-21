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

## Milestone 1: upload inbox + review (no publishing)
1. BotFather → `/setprivacy` → **Disable** (required to read forwarded videos in groups).
2. Add the bot to your private **Upload Inbox** group.
3. Set `UPLOAD_INBOX_CHAT_ID` (numeric like `-100123...` or `@username`).
4. Add courses:
   `/addcourse 1st Year|anatomy|Dr A|dr a,anatomy a|-100123...|12`
5. Forward lectures into the inbox. Each upload is stored in a daily batch. The bot stays silent per upload; use `/review`.
6. Review:
   - `/review` — recent batches
   - `/batch <id>` / `/dryrun <id>` — sorted preview
   - `/approve <id>` — approve only (nothing is published yet)
   - `/skip <item>` / `/keep <item>` / `/retry <item>`
   - `/setcourse <item> <course_id>` / `/setnumber <item> <n>`
   - `/pause` / `/resume`
7. Duplicates and uncertain year/teacher/number go to `needs_review`; nothing is assumed automatically.
8. Approved batches are locked. `/skip`, `/keep`, `/retry`, `/setcourse`, and `/setnumber` are rejected on approved items.

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
4. Set environment variables: `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`, `CHANNEL_ID`, `UPLOAD_INBOX_CHAT_ID`, and optionally `WEBHOOK_URL`/`PORT`.
5. If using polling: set service type to Worker (no HTTP port/health check).
6. If using webhook: set `WEBHOOK_URL` to your Northflank public URL and keep health check at `/health`.
7. Deploy and check logs for "DB initialized" and polling start.

## Free hosting alternatives
Koyeb/Fly.io via webhook; Oracle Cloud Always Free VM with polling.