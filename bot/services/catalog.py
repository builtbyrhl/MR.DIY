from __future__ import annotations

from bot.db.repo import list_subjects

DEFAULT_SUBJECTS = [
    ("anatomy", "Anatomy", "🫀", 1),
    ("physiology", "Physiology", "⚡", 2),
    ("biochemistry", "Biochemistry", "🧪", 3),
    ("pathology", "Pathology", "🔬", 4),
    ("pharmacology", "Pharmacology", "💊", 5),
    ("microbiology", "Microbiology", "🦠", 6),
    ("forensic", "Forensic Medicine", "⚖️", 7),
    ("psm", "PSM / Community Medicine", "🏥", 8),
    ("medicine", "Medicine", "🩺", 9),
    ("surgery", "Surgery", "🔪", 10),
    ("obg", "OBG", "👶", 11),
    ("pediatrics", "Pediatrics", "🧸", 12),
    ("ophthalmology", "Ophthalmology", "👁️", 13),
    ("ent", "ENT", "👂", 14),
    ("orthopedics", "Orthopedics", "🦴", 15),
    ("dermatology", "Dermatology", "🧬", 16),
    ("psychiatry", "Psychiatry", "🧠", 17),
    ("radiology", "Radiology", "📡", 18),
    ("anesthesia", "Anesthesia", "💉", 19),
]


async def ensure_subjects(session):
    existing = await list_subjects(session)
    if existing:
        return
    from bot.db.models import Subject
    for slug, name, emoji, order in DEFAULT_SUBJECTS:
        session.add(Subject(slug=slug, name=name, emoji=emoji, order=order))
    await session.commit()
