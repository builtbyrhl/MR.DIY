# FMGE Lecture Telegram Bot (MR.DIY)

Python (aiogram 3) + PostgreSQL.

## Setup
1. Copy `.env.example` to `.env` and fill `BOT_TOKEN`, `ADMIN_IDS`, `CHANNEL_ID`.
2. Install deps: `pip install -r requirements.txt`
3. Run: `python -m bot.main` (polling) or set `WEBHOOK_URL` for webhook mode.
4. Docker: `docker-compose up`

Optional access and retention settings:
- `ACCESS_DEFAULT_DAYS` — default approval duration. `0` keeps lifetime access; `1`/`2` gives 24/48-hour access. Explicit `/approveuser <id> <days>` overrides it.
- `MEDIA_RETENTION_HOURS` — hours before delivered student media is deleted (1–48, default 24). Values of 48 are scheduled with a small safety buffer inside Telegram's limit.
- `MEDIA_CLEANUP_INTERVAL_SECONDS` — how often the cleanup worker checks for due messages (default 300).

## Admin
- `/admin` for commands.
- Add lectures: `/addlecture subject_slug|title|channel_msg_id|[premium]`
- `/reorder <subject_slug>` to sort by channel sequence.
- `/grant <user_id> [days]` only works for users who have already started the bot.
- Student access: `/access` lists pending requests and approvals expiring within 7 days. `/approveuser <user_id> [days]` grants access for the optional duration (or `ACCESS_DEFAULT_DAYS`; pass `0` for lifetime); `/rejectuser <user_id>` and `/revokeuser <user_id>` remove it.
- Students can check `/myaccess` (or **🔑 My Access**) for their status, expiry, and media-retention window.
- Teachers: `/addteacher <subject_slug>|<name>|[alias1,alias2]`, `/teachers <subject_slug>`, `/setteacher <item_id>|<teacher_id|none>`, `/setteacherbulk <item_ids>|<teacher_id>`, and `/setlectureteacher <lecture_id>|<teacher_id|none>`.
- Teacher aliases: `/teacheralias <teacher_id>|<alias>`, `/removealias <teacher_id>|<alias>`, and `/renameteacher <teacher_id>|<new name>`. Aliases are explicit and subject-scoped; spelling is never guessed.
- Topics: `/addtopic <subject_slug>|<name>`, `/topics <subject_slug>`, `/settopic <item_ids>|<topic_ids|none>`, and `/setlecturetopic <lecture_id>|<topic_ids|none>`.
- Bulk assignment: `/assign <item_ids>|<course_id|->|<topic_ids|none|->|<teacher_id|none|->` (use `-` to leave a field unchanged).
- Bulk teacher assignment: `/setteacherbulk 7,9,8|1` assigns one existing teacher to up to 200 items atomically. Item IDs must be unique positive integers. Invalid input, a missing item, or a subject mismatch leaves every item unchanged.
- Caption correction: `/setcaption <item_id>|<caption>` reclassifies an unapproved item and preserves the previous caption.
- Caption recovery: `/refreshcaptions <batch_id> [apply]` previews or applies captions recovered from the stored source messages. It never publishes or creates uploads.
- Content is forwarded from `CHANNEL_ID`.

### Administrators
`ADMIN_IDS` is the immutable owner/superadmin list. Owners can manage additional administrators from a private chat:
- `/admins` — list owners and database-granted admins.
- `/addadmin <user_id>` — grant admin access; the user must have started the bot first.
- `/removeadmin <user_id>` — revoke a database-granted admin. Owners and the last remaining administrator cannot be removed here.

To find a new admin's numeric ID, ask them to `/start` the bot, then look in `/access` for their pending user ID (or use a trusted Telegram user-ID tool). After they are added, `/whereami` also shows their own user ID. Database admins have the same command access as owners except for add/remove-admin commands, which are owner-only. Keep at least one owner in `ADMIN_IDS`.

Admin membership is re-read from the database at the start of each update, so separate bot processes see add/remove changes without a restart. An update already in flight may still use the previous snapshot.

## Milestone 1: upload inbox + review + publishing
1. BotFather → `/setprivacy` → **Disable** (required to read forwarded videos in groups).
2. Add the bot to your private **Upload Inbox** group.
3. Set `UPLOAD_INBOX_CHAT_ID` (numeric like `-100123...` or `@username`). The inbox must be a group/supergroup. Channel posts and edited channel posts are not ingested.
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
   - `/reconcilepublish <item>|<message_id>|<destination_chat_id>|<topic_id>` — record an existing published message without sending again
   - `/unlockpublish <item>` — clear an uncertain publish state only after confirming no message exists
   - `/skip <item>` / `/keep <item>` / `/retry <item>`
   - `/setcourse <item> <course_id>` / `/setnumber <item> <n>`
   - `/settopic <item_ids>|<topic_ids|none>`
    - `/assign <item_ids>|<course_id|->|<topic_ids|none|->|<teacher_id|none|->`
    - `/setteacherplan <batch:<id>|items:<id,id,...>>|<teacher_id|teacher name> [apply]` (preview first; apply assigns an existing subject-scoped teacher to eligible unapproved items and flags missing/ambiguous teachers for review)
    - `/setcaption <item>|<caption>` (unapproved, non-skipped items only)
    - `/refreshcaptions <batch_id> [apply]` (preview first; apply only updates non-preferred captions)
    - `/generatecaptions <batch_id> [apply]` (preview first; apply generates pipe captions from filenames using existing course, teacher, and topic metadata; valid captions are preserved)
    - `/pause` / `/resume`
7. Duplicates and uncertain year/number go to `needs_review`; nothing is assumed automatically. Teacher and topic metadata are optional and are only assigned when the caption format and an existing subject-scoped teacher/topic match are unambiguous.
8. Approving a batch closes that batch. Uploads received after approval create a new open batch for the same day. Editing an upload's caption in the inbox reclassifies it while unapproved; edits to approved, publishing, published, or skipped items are ignored and keep the original caption for audit.
9. Approved batches are locked. `/skip`, `/keep`, `/retry`, `/setcourse`, `/setnumber`, and `/setcaption` are rejected on approved items. Teacher and topic assignment remains available because it only changes catalogue metadata.
10. Publishing is idempotent for completed items: `/publish` skips items that already have a published message ID. If the bot stops after Telegram accepts a send but before the database commit, or Telegram times out/loses the response, the item is left in `publishing` and is **not auto-resent**. Definite Telegram rejections, including flood-control `429` responses, are marked `failed` and can be retried; wait for the reported retry delay first. For an uncertain state, verify the destination topic. If the message exists, use `/reconcilepublish <item>|<message_id>|<destination_chat_id>|<topic_id>` to record it without sending again. If it does not exist, use `/unlockpublish <item>` and `/publish <id>` deliberately. Never unlock an item whose message already exists.
 11. `/batch` and `/dryrun` show the current teacher and topic assignments plus any automatic matching failure reason for every item.
 12. Filename fallback: the bot stores `video.file_name` or `document.file_name` when Telegram provides it. It strips the extension and reads a leading lecture number such as `08 Posterior Triangle of Neck.mp4` as `L8` with title `Posterior Triangle of Neck`. `/generatecaptions` can turn that filename data into the standard pipe caption only when the item already has a course, teacher, and single topic. If Telegram does not expose a filename, the number/title is missing, or required metadata is missing, the item is left for manual review.

## Topic organization

The standard caption format uses pipe separators:

```text
Subject | Teacher | Subtopic or System | Video Title | L<number>
Anatomy | Dr Mohammad Azam Sir | Thorax | Thoracic wall | L1
Anatomy | Dr Mohammad Azam Sir | Thorax | Intercostal Artery | L2
```

The final field must be a standalone `L<number>` between 1 and 999; matching is case-insensitive. The video-title field is stored as the student-visible lecture title, and the lecture number is stored as an integer for numeric ordering. Do not put the number in the title. A missing, malformed, or out-of-range final number sends the item to `needs_review`. An `L<number>` inside the title is not used as the lecture number.

The four-field pipe layout remains supported for captions without a number:

```text
Subject | Teacher | Subtopic or System | Video Title
Anatomy | Dr Mohammad Azam Sir | Neuroanatomy | Brainstem
```

Empty, pure lecture-number, or subject/topic-repeat titles are treated as generic and sent to `needs_review`.

The legacy pipe layout remains valid:

```text
Subject | Subtopic or System | Lecture Number | Teacher
Anatomy | Head and Neck | Lecture 12 | Dr Mohammad Azam Sir
```

The first line must contain at least three pipe-separated fields. In the legacy layout, the third field must contain a lecture number and a missing fourth field flags `teacher missing`. A legacy lecture field may include a title suffix, for example `Lecture 3: Cranial Nerves`.

Slash-separated captions are also supported for legacy and imported content, including:

```text
Subject / Teacher / Subtopic or System / Video Title / L<number>
Subject / Teacher / Subtopic or System / Video Title
Subject / Subtopic or System / Lecture Number / Teacher
```

The four-field slash layouts require exactly four fields; the numbered slash layout requires exactly five. Existing course, teacher, and topic records resolve fields and aliases; if an ambiguous four-field layout does not produce exactly one valid interpretation, the item goes to `needs_review`.

Topics are subject-scoped and must already exist. Match names exactly after case, spacing, and punctuation normalization; captions never create topics. Unknown, missing, or ambiguous topics send the item to `needs_review` with a reason such as `topic not found: Neuroanatomy`. Add topics with `/addtopic anatomy|Skull` before ingesting. Pipe and legacy slash captions use the canonical topic or original lecture title; the numbered pipe and slash layouts use their video-title field as the visible title.

Lectures may belong to multiple topics. Lecture numbers are unique within a course and shared topic, so `L1` can exist in both `Skull` and `Upper Limb`, but a second `L1` in `Skull` is sent to `needs_review` as a duplicate. Topic assignment can be corrected after batch approval; clearing a topic is also treated as a manual decision and survives `/retry`.

Existing uploads and legacy captions are never assigned topics automatically. Re-ingesting old Telegram messages creates new uploads, so use `/refreshcaptions <batch_id>` to preview captions recovered from the stored source messages, then `/refreshcaptions <batch_id> apply` to update invalid or non-preferred captions, fill missing lecture numbers from numbered captions, and reclassify already-valid ones. The preview shows current, source, and proposed lecture numbers. Alternatively use `/setcaption` on unapproved, non-skipped items (use `/keep` first to restore a skipped item) or `/settopic` and `/setlecturetopic` on existing catalogue entries to classify them deliberately. The recovery command forwards temporary copies only to the requesting admin's private chat and deletes them; it preserves valid preferred captions, never publishes, and never creates new uploads. Telegram caption edits update the database only while the item remains unapproved; edits to approved, publishing, or published items are deliberately ignored. To correct the student-visible title of an already published video without re-uploading it, use `/setlecturetitle <lecture_id>|<title>`. This mirrors the title to the linked upload while leaving the original caption, file ID, publication state, and audit history unchanged. Legacy lectures remain available through teacher browsing.

## Automatic teacher extraction
For legacy captions, put the teacher at the end of the first caption line. In the explicit `Subject | Topic | Lecture Number | Teacher` format, only the fourth field is used as the teacher. A legacy teacher caption looks like:

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
- The teacher is then matched by canonical name or explicit alias within that single subject. The teacher must already exist; captions never create teacher records and spelling is never guessed. For example, add `Dr Mohammad Azam` as an alias when the teacher is displayed as `Dr. Muhammad Azam`.
- Exactly one matching teacher is required. Unknown or ambiguous explicit teacher segments leave `teacher_id` unset and add a reason such as `teacher not found: Dr Sharma` to `/batch` and `/dryrun`.
- A trailing plain subtitle that does not match a teacher, such as `Anatomy A Lecture 2 - Upper Limb`, is left in the title and does not block classification.
- When a teacher is resolved, the teacher segment is removed from the lecture title.
- If no teacher is specified, the lecture remains under **Teacher not specified**.
- Manual `/setteacher`, `/setteacherbulk`, and `/setlectureteacher` corrections are marked as manual and are not overwritten by later `/retry` or automatic reclassification. Manually clearing a teacher also survives retries.

### Recovering unresolved teacher items
1. Check the subject roster with `/teachers anatomy`. Existing aliases are shown under each teacher.
2. If the teacher is missing, add them with `/addteacher anatomy|Dr. Muhammad Azam|Dr Mohammad Azam,Mohammad Azam`. If only the spelling variant is missing, use `/teacheralias <teacher_id>|Dr Mohammad Azam`. Do not create a duplicate teacher just to match a spelling.
3. Run `/batch <batch_id>` to get the item IDs and confirm the expected teacher ID.
4. Either retry classification with `/retry <item_id>` for each item after adding the alias, or assign explicitly with `/setteacherbulk <item_ids>|<teacher_id>` or `/assign <item_ids>|-|-|<teacher_id>`.
5. Run `/dryrun <batch_id>`, then `/approve <batch_id>` and `/publish <batch_id>` once the destination and metadata are verified. Existing items are not reclassified automatically after an alias change.

## Student home
The reply menu keeps five primary actions: **📚 My Courses**, **🔍 Search**, **▶️ Continue**, **🔑 My Access**, and **❓ Help**. Continue reopens the last watched lecture; My Access shows access status, expiry, and the media-retention window. Bookmarks, progress, premium, and subjects remain available as commands and through their existing handlers.

## Teacher-based student navigation
Students choose `Subject → Teacher → System/Topic → Lecture`, or use **All teacher lectures** for a flat list. Teacher and topic lists are built dynamically from the published catalogue, with lecture counts per teacher. Teachers are subject-specific: the same person teaching two subjects has one teacher record per subject. Canonical names and explicit aliases are normalized (spacing, punctuation, and case) within each subject to prevent duplicates.

Teacher assignment is optional. Uploads and existing lectures with no teacher stay visible under **Teacher not specified**. Create teachers with `/addteacher`, then assign them with `/setteacher` (upload item), `/setteacherbulk` (many upload items in one transaction), or `/setlectureteacher` (existing lecture). Setting a teacher to `none` clears the assignment; `/setteacherbulk` requires a positive teacher ID and cannot clear teachers. Subject mismatches are rejected. `/renameteacher` keeps the same teacher ID and all upload/lecture links; `/teacheralias` adds alternate caption spellings without renaming the teacher.

Teacher corrections update both the upload item and its linked lecture in one transaction. This prevents startup catalogue sync from restoring stale teacher metadata after a restart. Legacy lectures remain in the unspecified bucket until an admin sets a teacher.

## Topic-based student navigation

Students can choose `Subject → Topic → Lecture` in addition to `Subject → Teacher → Lecture`. Topic lists are built from published lecture-topic associations with lecture counts, and the same lecture record is reused across every topic route. Pagination and back navigation preserve the selected topic. Topic links are projected from the upload item when publishing and are re-synced at startup. Moving an upload to another subject clears incompatible topic associations. Existing access approval and premium behavior are unchanged.

## Content protection
Student media is sent with Telegram's `protect_content=True`, and the same flag is used when publishing to course topics and copying legacy channel lectures. This disables native forwarding/saving inside Telegram only. It is **not DRM**: users can still screen-record or redistribute files, and Telegram can remove the flag on some content. Do not describe it as copy-proof.

The bot also records each private student delivery in `student_media_messages` and a restart-safe background worker deletes it after `MEDIA_RETENTION_HOURS` (default 24, maximum 48). The worker only targets those recorded private deliveries; it never deletes upload records, source channel posts, or published course-topic messages. Deletion is best-effort: Telegram only allows bots to delete messages for about 48 hours, and a message may already be gone or protected from deletion. Records are marked `deleted`, `failed`, or `missed` accordingly, so a missed deletion is never reported as successful. Only deliveries made after this feature is deployed are tracked; existing private copies are not swept automatically.

## Backup and restore
Take a backup before deploying schema changes.
- PostgreSQL: strip SQLAlchemy's `+asyncpg` suffix before passing the URL to libpq tools: `pg_dump --dbname="${DATABASE_URL/+asyncpg/}" --format=custom --file=mrdiy-backup.dump`.
- Restore only into an empty/new database: `pg_restore --dbname="${DATABASE_URL/+asyncpg/}" --clean --if-exists mrdiy-backup.dump`.
- SQLite (local/dev): stop the bot, copy the `.db` file, and keep the copy outside the repo.

The startup migrations are additive and idempotent. They never delete uploads, batches, lectures, users, access decisions, or delivered-media records. Existing tables gain nullable `teacher_id`, `teacher_source`, `teacher_review_reason`, `original_caption`, `topic_source`, `topic_review_reason`, and `access_expires_at` columns, and the new `topics`, `upload_item_topics`, `lecture_topics`, `teacher_aliases`, `admin_users`, and `student_media_messages` tables are created. Existing rows keep their values and are not auto-classified into topics. Existing approved users keep lifetime access because `access_expires_at` remains NULL. If an old database already has duplicate open batches, the unique open-batch index is skipped with a warning rather than merging or deleting data; resolve duplicates manually after inspection.

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