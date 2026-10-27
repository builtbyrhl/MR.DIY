from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import Message, ReplyParameters
from aiogram.filters import Command
from sqlalchemy.exc import IntegrityError

from bot.db.repo import (
    add_db_admin,
    grant_premium,
    list_db_admins,
    remove_db_admin,
)
from bot.services.auth import (
    all_admin_ids,
    db_admin_ids,
    is_admin,
    is_owner,
    load_db_admin_cache,
    owner_ids,
)

router = Router()
logger = logging.getLogger(__name__)


def _is_private_chat(message: Message) -> bool:
    return getattr(getattr(message, "chat", None), "type", None) == "private"


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 Admin commands:\n\n"
        "📚 Catalogue:\n"
        "/addsubject <slug>|<name>|<emoji>\n"
        "/addlecture <subject_slug>|<title>|<channel_msg_id>|[premium]\n"
        "/reorder <subject_slug>\n\n"
        "👥 Student access:\n"
        "/access — pending requests and expiring access\n"
        "/approveuser <user_id> [days; 0 = lifetime]\n"
        "/rejectuser <user_id>\n"
        "/revokeuser <user_id>\n"
        "/grant <user_id> [days]\n\n"
        "📥 Upload & catalogue:\n"
        "Caption: Subject | Teacher | Topic | Video Title | L<number>\n"
        "Example: Anatomy | Dr Azam | Thorax | Thoracic wall | L1\n"
        "Also: Subject | Teacher | Topic | Video Title (no number)\n"
        "Legacy: Subject | Topic | Lecture Number | Teacher or "
        "Subject / Topic / Lecture Number / Teacher\n"
        "/addcourse <subject_slug>|<name>|[aliases]|[dest]|[topic]\n"
        "/courses\n"
        "/addteacher <subject_slug>|<name>|[alias1,alias2]\n"
        "/teachers <subject_slug>\n"
        "/teacheralias <teacher_id>|<alias>\n"
        "/removealias <teacher_id>|<alias>\n"
        "/renameteacher <teacher_id>|<new name>\n"
        "/setteacher <item_id>|<teacher_id|none>\n"
        "/setteacherbulk <item_ids>|<teacher_id>\n"
        "/setteacherplan <batch:<id>|items:<ids>>|<teacher_id|name> [apply]\n"
        "/setlectureteacher <lecture_id>|<teacher_id|none>\n"
        "/setlecturetitle <lecture_id>|<title> - no re-upload\n"
        "/addtopic <subject_slug>|<name>\n"
        "/topics <subject_slug>\n"
        "/settopic <item_ids>|<topic_ids|none>\n"
        "/setlecturetopic <lecture_id>|<topic_ids|none>\n"
        "/assign <item_ids>|<course|->|<topics|none|->|<teacher|none|->\n"
        "/setcaption <item_id>|<caption> — reclassify an unapproved item\n"
        "/review\n"
        "/refreshcaptions <batch_id> [apply] - recover source captions\n"
        "/generatecaptions <batch_id> [apply] - pipe captions from filenames\n"
        "/batch <id> · /dryrun <id> · /approve <id>\n"
        "/publish <id>\n"
        "/reconcilepublish <item>|<message>|<destination>|<topic> — if the message already exists\n"
        "/unlockpublish <item> — only if the message does not exist\n"
        "/skip <item> · /keep <item> · /retry <item>\n"
        "/pause · /resume\n\n"
        "🏛 Institutions:\n"
        "/addinstitution <name>\n"
        "/institutions\n"
        "/setinstitutiondefault <institution>\n"
        "/setinstitutionbatch <batch_id>|<institution>\n"
        "/setinstitutionitem <item_id>|<institution>\n\n"
        "📊 Utilities:\n"
        "/stats\n"
        "/whereami\n\n"
        "🔐 Admins (private chat only):\n"
        "/admins\n"
        "/addadmin <user_id> - owner only\n"
        "/removeadmin <user_id> - owner only",
        parse_mode=None,
    )


@router.message(Command("whereami"))
async def whereami_cmd(message: Message):
    sender = getattr(message, "from_user", None)
    if sender is None or not is_admin(sender.id):
        logger.warning(
            "Ignored /whereami: sender is not an admin (chat=%s sender=%s)",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(sender, "id", None),
        )
        return
    chat = message.chat
    thread_id = getattr(message, "message_thread_id", None)
    title = getattr(chat, "title", None) or getattr(chat, "type", "unknown")
    text = (
        f"user.id: {sender.id}\n"
        f"chat.id: {chat.id}\n"
        f"message_thread_id: {thread_id if thread_id is not None else 'None'}\n"
        f"chat title: {title}"
    )
    # Reply to the command message itself so the answer stays in the same
    # forum topic (a plain chat message would land in the General topic).
    await message.answer(
        text,
        reply_parameters=ReplyParameters(
            message_id=message.message_id,
            allow_sending_without_reply=True,
        ),
    )


@router.message(Command("addsubject"))
async def add_subject(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in message.text.replace("/addsubject", "", 1).strip().split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await message.answer("Usage: /addsubject slug|name|emoji")
        return
    slug, name = parts[0], parts[1]
    emoji = parts[2] if len(parts) > 2 else "📘"
    from bot.db.models import Subject
    session.add(Subject(slug=slug, name=name, emoji=emoji))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.answer(f"Subject '{slug}' already exists.")
        return
    await message.answer(f"Added subject {name}")


@router.message(Command("addlecture"))
async def add_lecture(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in message.text.replace("/addlecture", "", 1).strip().split("|")]
    if len(parts) < 3 or not parts[0] or not parts[1]:
        await message.answer("Usage: /addlecture subject_slug|title|channel_msg_id|[premium]")
        return
    slug, title = parts[0], parts[1]
    try:
        msg_id = int(parts[2])
    except ValueError:
        await message.answer("Channel message ID must be a number.")
        return
    if msg_id <= 0:
        await message.answer("Channel message ID must be a positive number.")
        return
    premium = (parts[3].lower() == "premium") if len(parts) > 3 else False
    from bot.db.repo import get_subject
    subj = await get_subject(session, slug)
    if not subj:
        await message.answer("Subject not found")
        return
    from bot.db.models import Lecture
    from sqlalchemy import select, func
    max_order = await session.execute(select(func.coalesce(func.max(Lecture.order), 0)).where(Lecture.subject_id == subj.id))
    next_order = (max_order.scalar() or 0) + 1
    session.add(Lecture(subject_id=subj.id, title=title, channel_message_id=msg_id, is_premium=premium, order=next_order))
    await session.commit()
    await message.answer(f"Added lecture: {title}")


@router.message(Command("grant"))
async def grant_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /grant <user_id> [days]")
        return
    try:
        uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 365
    except ValueError:
        await message.answer("Usage: /grant <user_id> [days]")
        return
    if uid <= 0 or days <= 0:
        await message.answer("User ID and days must be positive numbers.")
        return
    from bot.db.models import User
    user = await session.get(User, uid)
    if user is None:
        await message.answer(
            f"User {uid} was not found. They must start the bot first."
        )
        return
    await grant_premium(session, uid, days)
    await message.answer(f"Granted premium to {uid} for {days} days")


def _slugify_name(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "institution"


@router.message(Command("addinstitution"))
async def addinstitution_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from bot.db.models import Institution

    text = message.text or ""
    parts = text.replace("/addinstitution", "", 1).strip()
    if not parts:
        await message.answer("Usage: /addinstitution <name>")
        return
    name = parts.strip()
    slug = _slugify_name(name)
    inst = Institution(name=name, slug=slug)
    session.add(inst)
    try:
        await session.commit()
        await session.refresh(inst)
    except IntegrityError:
        await session.rollback()
        await message.answer("Institution already exists.")
        return
    await message.answer(f"Added institution #{inst.id}: {inst.name} ({inst.slug})")


@router.message(Command("institutions"))
async def institutions_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from bot.db.models import Institution
    from sqlalchemy import select

    res = await session.execute(select(Institution).order_by(Institution.id))
    items = res.scalars().all()
    if not items:
        await message.answer("No institutions.")
        return
    lines = ["Institutions:"]
    for inst in items:
        lines.append(f"#{inst.id}: {inst.name} ({inst.slug})")
    await message.answer("\n".join(lines))


@router.message(Command("setinstitutiondefault"))
async def setinstitutiondefault_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from bot.db.models import Institution
    from bot.db.ingestion_repo import set_setting
    from sqlalchemy import select

    text = message.text or ""
    arg = text.replace("/setinstitutiondefault", "", 1).strip()
    if not arg:
        await message.answer("Usage: /setinstitutiondefault <institution_id_or_name>")
        return
    res = await session.execute(select(Institution))
    insts = res.scalars().all()
    inst = None
    if arg.isdigit():
        for i in insts:
            if i.id == int(arg):
                inst = i
                break
    if inst is None:
        arg_low = arg.lower()
        for i in insts:
            if i.slug.lower() == arg_low or i.name.lower() == arg_low:
                inst = i
                break
    if inst is None:
        await message.answer("Institution not found.")
        return
    await set_setting(session, "default_institution_id", str(inst.id))
    await message.answer(f"Default institution set to #{inst.id}: {inst.name}")


@router.message(Command("setinstitutionbatch"))
async def setinstitutionbatch_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from bot.db.models import Institution, UploadBatch
    from sqlalchemy import select

    text = message.text or ""
    arg = text.replace("/setinstitutionbatch", "", 1).strip()
    if "|" not in arg:
        await message.answer("Usage: /setinstitutionbatch <batch_id>|<institution>")
        return
    batch_part, inst_part = [x.strip() for x in arg.split("|", 1)]
    if not batch_part.isdigit():
        await message.answer("Batch ID must be a number.")
        return
    batch = await session.get(UploadBatch, int(batch_part))
    if not batch:
        await message.answer("Batch not found.")
        return
    res = await session.execute(select(Institution))
    insts = res.scalars().all()
    inst = None
    if inst_part.isdigit():
        for i in insts:
            if i.id == int(inst_part):
                inst = i
                break
    if inst is None:
        ip = inst_part.lower()
        for i in insts:
            if i.slug.lower() == ip or i.name.lower() == ip:
                inst = i
                break
    if inst is None:
        await message.answer("Institution not found.")
        return
    from bot.db.models import UploadItem
    from sqlalchemy import update

    await session.execute(update(UploadItem).where(UploadItem.batch_id == batch.id).values(institution_id=inst.id))
    await session.commit()
    await message.answer(f"Set institution for batch #{batch.id} to #{inst.id}: {inst.name}")


@router.message(Command("setinstitutionitem"))
async def setinstitutionitem_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from bot.db.models import Institution, UploadItem
    from sqlalchemy import select

    text = message.text or ""
    arg = text.replace("/setinstitutionitem", "", 1).strip()
    if "|" not in arg:
        await message.answer("Usage: /setinstitutionitem <item_id>|<institution>")
        return
    item_part, inst_part = [x.strip() for x in arg.split("|", 1)]
    if not item_part.isdigit():
        await message.answer("Item ID must be a number.")
        return
    item = await session.get(UploadItem, int(item_part))
    if not item:
        await message.answer("Item not found.")
        return
    res = await session.execute(select(Institution))
    insts = res.scalars().all()
    inst = None
    if inst_part.isdigit():
        for i in insts:
            if i.id == int(inst_part):
                inst = i
                break
    if inst is None:
        ip = inst_part.lower()
        for i in insts:
            if i.slug.lower() == ip or i.name.lower() == ip:
                inst = i
                break
    if inst is None:
        await message.answer("Institution not found.")
        return
    item.institution_id = inst.id
    await session.commit()
    await message.answer(f"Set institution for item #{item.id} to #{inst.id}: {inst.name}")


@router.message(Command("reorder"))
async def reorder_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /reorder <subject_slug>")
        return
    from bot.db.repo import get_subject
    subj = await get_subject(session, args[1])
    if not subj:
        await message.answer("Subject not found")
        return
    from bot.db.models import Lecture
    from sqlalchemy import select
    res = await session.execute(
        select(Lecture).where(Lecture.subject_id == subj.id).order_by(Lecture.channel_message_id, Lecture.id)
    )
    lectures = list(res.scalars().all())
    for i, lec in enumerate(lectures, start=1):
        lec.order = i
    await session.commit()
    await message.answer(f"Reordered {len(lectures)} lectures in {subj.name}")


@router.message(Command("stats"))
async def stats_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    from sqlalchemy import select, func
    from bot.db.models import User, Lecture
    u = await session.execute(select(func.count(User.id)))
    l = await session.execute(select(func.count(Lecture.id)))
    await message.answer(f"Users: {u.scalar()}\nLectures: {l.scalar()}")


@router.message(Command("admins"))
async def admins_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    if not _is_private_chat(message):
        await message.answer("Use /admins in a private chat.")
        return
    db_admins = await list_db_admins(session)
    lines = [
        "Owners (ADMIN_IDS, cannot be removed from chat):",
        ", ".join(str(uid) for uid in sorted(owner_ids())) or "none",
        "",
        "Database-granted admins:",
        ", ".join(str(row.user_id) for row in db_admins) or "none",
    ]
    await message.answer("\n".join(lines), parse_mode=None)


@router.message(Command("addadmin"))
async def addadmin_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    if not is_owner(message.from_user.id):
        await message.answer("Only owner admins can add administrators.")
        return
    if not _is_private_chat(message):
        await message.answer("Use /addadmin in a private chat.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Usage: /addadmin <user_id>")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("User ID must be a number.")
        return
    if user_id <= 0:
        await message.answer("User ID must be a positive number.")
        return
    if user_id in owner_ids():
        await message.answer(f"User {user_id} is already an owner admin.")
        return
    admin, error = await add_db_admin(
        session, user_id, granted_by=message.from_user.id
    )
    if error:
        await message.answer(error)
        return
    await load_db_admin_cache(session)
    await message.answer(
        f"Granted administrator access to {user_id}. "
        "They must start the bot and use /admin."
    )


@router.message(Command("removeadmin"))
async def removeadmin_cmd(message: Message, session):
    if not is_admin(message.from_user.id):
        return
    if not is_owner(message.from_user.id):
        await message.answer("Only owner admins can remove administrators.")
        return
    if not _is_private_chat(message):
        await message.answer("Use /removeadmin in a private chat.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Usage: /removeadmin <user_id>")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("User ID must be a number.")
        return
    if is_owner(user_id):
        await message.answer(
            "Owner admins come from ADMIN_IDS and cannot be removed here."
        )
        return
    if user_id not in db_admin_ids():
        await message.answer("User is not a database-granted admin.")
        return
    if len(all_admin_ids()) <= 1:
        await message.answer("Cannot remove the last remaining administrator.")
        return
    _, error = await remove_db_admin(session, user_id)
    if error:
        await message.answer(error)
        return
    await load_db_admin_cache(session)
    await message.answer(f"Removed administrator access from {user_id}.")
