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
- `/grant <user_id> [days]` only works for users who have already started the bot.
- Teachers: `/addteacher <subject_slug>|<name>`, `/teachers <subject_slug>`, `/setteacher <item_id>|<teacher_id|none>`, and `/setlectureteacher <lecture_id>|<teacher_id|none>`.
- Content is forwarded from `CHANNEL_ID`.

## Milestone 1: upload inbox + review + publishing
1. BotFather → `/setprivacy` → **Disable** (required to read forwarded videos in groups).
2. Add the bot to your private **Upload Inbox** group.
3. Set `UPLOAD_INBOX_CHAT_ID` (numeric like `-100123...` or `@username`).
4. Add courses (one per subject topic of the Med Lounge forum):
   `/addcourse <subject_slug>|<name>|[aliases]|[destination_chat]|[topic_id]`
   Example: `/addcourse anatomy|Anatomy|anatomy,anat|-100123...|12`
   `name`, `aliases`, `destination_chat` and `topic_id` are optional; the destination falls back to `DESTINATION_GROUP_ID`. Re-running the same command updates the existing course (e.g. to set a topic id).
5. Forward lectures into the inbox. Uploads are grouped into one **open daily batch per inbox chat**. The bot stays silent per upload; use `/review`.
6. Review:
   - `/review` — recent batches
   - `/batch <id>` / `/dryrun <id>` — sorted preview
   - `/approve <id>` — approve and lock the batch (does not publish)
   - `/publish <id>` — publish an approved batch to each course destination/topic
   - `/unlockpublish <item>` — clear an uncertain publish state after manually checking the destination topic
   - `/skip <item>` / `/keep <item>` / `/retry <item>`
   - `/setcourse <item> <course_id>` / `/setnumber <item> <n>`
   - `/pause` / `/resume`
7. Duplicates and uncertain year/number go to `needs_review`; nothing is assumed automatically. Teacher metadata is optional and is only assigned when the caption format and an existing subject-scoped teacher match are unambiguous.
8. Approving a batch closes that batch. Uploads received after approval create a new open batch for the same day.
9. Approved batches are locked. `/skip`, `/keep`, `/retry`, `/setcourse`, and `/setnumber` are rejected on approved items. Teacher assignment remains available because it only changes catalogue metadata.
10. Publishing is idempotent for completed items: `/publish` skips items that already have a published message ID. If the bot stops after Telegram accepts a send but before the database commit, the item is left in `publishing` and is **not auto-resent**. Verify the destination topic, then use `/unlockpublish <item>` and `/publish <id>` deliberately.
11. `/batch` and `/dryrun` show the current teacher assignment and any automatic teacher-matching failure reason for every item.

## Automatic teacher extraction
Put the teacher at the end of the first caption line. The exact supported format is:

```text
Lecture 10: Pathophysiology — Dr Sharma
```

Supported separators (the final separator on the first line is used):
- em dash: `—` (with or without surrounding spaces) — always treated as a teacher separator
- en dash: `–` (with or without surrounding spaces) — always treated as a teacher separator
- spaced pipe: ` | ` — always treated as a teacher separator
- spaced hyphen: ` - ` — requires a teacher label/honorific, or an exact teacher match, so ordinary subtitles are not misread

Using an em dash, en dash, or pipe with a teacher name that does not exist sends the item to `needs_review` rather than silently leaving the teacher unassigned.

The teacher segment may optionally use a `Teacher:`, `Faculty:`, or `By` prefix, for example `Lecture 10: Pathophysiology — Teacher: Dr Sharma` or `Lecture 10: Pathophysiology — by Dr Sharma`. Teacher names with `Dr`, `Dr.`, `Prof`, `Professor`, `Mr`, `Mrs`, `Ms`, `Miss`, `Sir`, `Madam`, `Asst. Prof`, or `Assoc. Prof` count as explicitly marked teacher segments.

Matching behavior:
- The course/subject is identified first using whole-word course-name/alias matches. A single-token alias may match the start of a word only when no exact name/alias match exists (for example `anat` → `Anatomy`), so `Physiology` never matches inside `Pathophysiology`. No match or multiple course matches sends the item to `needs_review`.
- The teacher is then matched by the existing normalized name rules within that single subject. The teacher must already exist; captions never create teacher records.
- Exactly one matching teacher is required. Unknown or ambiguous explicit teacher segments leave `teacher_id` unset and add a reason such as `teacher not found: Dr Sharma` to `/batch` and `/dryrun`.
- A trailing plain subtitle that does not match a teacher, such as `Anatomy A Lecture 2 - Upper Limb`, is left in the title and does not block classification.
- When a teacher is resolved, the teacher segment is removed from the lecture title.
- If no teacher is specified, the lecture remains under **Teacher not specified**.
- Manual `/setteacher` and `/setlectureteacher` corrections are marked as manual and are not overwritten by later `/retry` or automatic reclassification. Manually clearing a teacher also survives retries.

## Teacher-based student navigation
Students choose `Subject → Teacher → Lecture`. Teacher lists are built dynamically from the published catalogue, with lecture counts per teacher. Teachers are subject-specific: the same person teaching two subjects has one teacher record per subject. Names are normalized (spacing, punctuation, and case) within each subject to prevent duplicates.

Teacher assignment is optional. Uploads and existing lectures with no teacher stay visible under **Teacher not specified**. Create teachers with `/addteacher`, then assign them with `/setteacher` (upload item) or `/setlectureteacher` (existing lecture). Setting a teacher to `none` clears the assignment. Subject mismatches are rejected.

Teacher corrections update both the upload item and its linked lecture in one transaction. This prevents startup catalogue sync from restoring stale teacher metadata after a restart. Legacy lectures remain in the unspecified bucket until an admin sets a teacher.

## Content protection
Student media is sent with Telegram's `protect_content=True`, and the same flag is used when publishing to course topics and copying legacy channel lectures. This disables native forwarding/saving inside Telegram only. It is **not DRM**: users can still screen-record or redistribute files, and Telegram can remove the flag on some content. Do not describe it as copy-proof.

## Backup and restore
Take a backup before deploying schema changes.
- PostgreSQL: strip SQLAlchemy's `+asyncpg` suffix before passing the URL to libpq tools: `pg_dump --dbname="${DATABASE_URL/+asyncpg/}" --format=custom --file=mrdiy-backup.dump`.
- Restore only into an empty/new database: `pg_restore --dbname="${DATABASE_URL/+asyncpg/}" --clean --if-exists mrdiy-backup.dump`.
- SQLite (local/dev): stop the bot, copy the `.db` file, and keep the copy outside the repo.

The startup migrations are additive and idempotent. They never delete uploads, batches, lectures, users, or access decisions. Existing tables gain nullable `teacher_id`, `teacher_source`, and `teacher_review_reason` columns plus indexes without changing row data. If an old database already has duplicate open batches, the unique open-batch index is skipped with a warning rather than merging or deleting data; resolve duplicates manually after inspection.

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

## Deployment checklist
1. Run `python -m pytest` and `python -m compileall bot`.
2. Back up the production database.
3. Deploy the reviewed commit, then restart the bot.
4. Check startup logs for migration errors and confirm `/health` responds in webhook mode.
5. Open `/admin`, `/review`, and `/stats` as an admin. Do not publish a real batch until the destination course/topic is verified.
6. After deployment, confirm approved/unapproved users behave as expected with `/start`.