from __future__ import annotations

import math
import re

MAX_BUTTON_LABEL = 60

_LECTURE_PREFIX = re.compile(
    r"^\s*(?:lecture|lec)\s*#?\s*(\d+)\s*[-–—:|.]?\s*", re.IGNORECASE
)
_LECTURE_NUMBER = re.compile(
    r"\b(?:lecture|lec)\s*#?\s*(\d+)\b", re.IGNORECASE
)
_GENERIC_TITLES = {
    "lecture",
    "file",
    "document",
    "video",
    "untitled",
    "untitled lecture",
}
_NUMBER_FIELD = re.compile(
    r"^(?:\d{1,3}|[Ll]\s*\d{1,3}|(?:lecture|lec)\s*#?\s*\d{1,3})$",
    re.IGNORECASE,
)
_TEACHER_FIELD = re.compile(
    r"^(?:(?:teacher|faculty)\s*[:\-]\s*|by\s+|"
    r"(?:dr|prof|professor|mr|mrs|ms|miss|sir|madam|"
    r"asst\.?\s+prof(?:essor)?|assoc\.?\s+prof(?:essor)?)\b)",
    re.IGNORECASE,
)


def clip(text: str, limit: int = MAX_BUTTON_LABEL) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").casefold()).split())


def _is_generic_title(
    text: str, subject_name: str | None = None, course_name: str | None = None
) -> bool:
    normalized = _normalized(text)
    if not normalized or normalized in _GENERIC_TITLES:
        return True
    for base in (subject_name, course_name):
        base_normalized = _normalized(base or "")
        if not base_normalized:
            continue
        if normalized == base_normalized:
            return True
        if normalized.startswith(base_normalized + " "):
            remainder = normalized[len(base_normalized) :].strip()
            if re.fullmatch(
                r"(?:(?:lecture|lec|class|video|part|l)\s*)?#?\s*\d{1,3}",
                remainder,
            ):
                return True
    return False


def _first_caption_line(raw_caption: str | None) -> str:
    for line in (raw_caption or "").splitlines():
        clean = " ".join(line.split())
        if clean:
            return clean
    return ""


def _has_caption_number(value: str) -> bool:
    clean = " ".join(value.split())
    if _NUMBER_FIELD.fullmatch(clean):
        return True
    return bool(
        re.match(
            r"^(?:(?:lecture|lec|class|video|part)\s*[-:#.]?\s*)?"
            r"\d{1,3}(?![0-9A-Za-z])",
            clean,
            re.IGNORECASE,
        )
    )


def _slash_caption_title(line: str) -> str:
    parts = [" ".join(part.split()) for part in line.split("/")]
    if len(parts) != 4:
        return ""
    second_teacher = bool(_TEACHER_FIELD.match(parts[1]))
    fourth_teacher = bool(_TEACHER_FIELD.match(parts[3]))
    legacy_number = _has_caption_number(parts[2])
    preferred_number = _has_caption_number(parts[3])
    if fourth_teacher and not second_teacher:
        return parts[1]
    if second_teacher and not fourth_teacher:
        return parts[3]
    if legacy_number and not preferred_number:
        return parts[1]
    if preferred_number and not legacy_number:
        return parts[3]
    if not second_teacher and not fourth_teacher and not legacy_number:
        return parts[3]
    return ""


def _new_slash_video_title(raw_caption: str | None) -> str:
    line = _first_caption_line(raw_caption)
    if "/" not in line or "|" in line:
        return ""
    parts = [" ".join(part.split()) for part in line.split("/")]
    if len(parts) != 4:
        return ""
    second_teacher = bool(_TEACHER_FIELD.match(parts[1]))
    fourth_teacher = bool(_TEACHER_FIELD.match(parts[3]))
    if second_teacher and not fourth_teacher:
        return parts[3]
    if (
        not second_teacher
        and not fourth_teacher
        and not _has_caption_number(parts[2])
        and not _has_caption_number(parts[3])
    ):
        return parts[3]
    return ""


def _caption_title(raw_caption: str | None) -> str:
    line = _first_caption_line(raw_caption)
    if not line:
        return ""
    if "|" in line:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 3 and _NUMBER_FIELD.fullmatch(parts[2].strip()):
            return " ".join(parts[1].split())
    if "/" in line:
        return _slash_caption_title(line) or line
    return line


def _file_title(file_name: str | None) -> str:
    name = (file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return " ".join(name.split())


def resolve_lecture_title(
    canonical_title: str | None,
    *,
    subject_name: str | None = None,
    course_name: str | None = None,
    topic_names: list[str] | tuple[str, ...] = (),
    upload_title: str | None = None,
    raw_caption: str | None = None,
    original_caption: str | None = None,
    file_name: str | None = None,
) -> str:
    """Choose the most meaningful available title without changing stored data."""
    candidates: list[str] = [canonical_title or ""]
    video_title = _new_slash_video_title(raw_caption) or _new_slash_video_title(
        original_caption
    )
    if video_title:
        candidates.append(video_title)
    topics = [" ".join(name.split()) for name in topic_names if name]
    if topics:
        candidates.append(" / ".join(topics))
    candidates.append(upload_title or "")
    candidates.append(_caption_title(raw_caption))
    candidates.append(_caption_title(original_caption))
    candidates.append(_file_title(file_name))

    for candidate in candidates:
        clean = " ".join(candidate.split())
        if clean and not _is_generic_title(clean, subject_name, course_name):
            return clean
    return ""


def _display_source_title(lecture) -> str:
    if hasattr(lecture, "display_title"):
        return " ".join((getattr(lecture, "display_title", "") or "").split())
    return " ".join((getattr(lecture, "title", "") or "").split())


def total_pages(total: int, per_page: int) -> int:
    if total <= 0 or per_page <= 0:
        return 1
    return max(1, math.ceil(total / per_page))


def clamp_page(page: int, total: int, per_page: int) -> int:
    return min(max(page, 0), total_pages(total, per_page) - 1)


def page_suffix(page: int, total: int, per_page: int) -> str:
    pages = total_pages(total, per_page)
    if pages <= 1:
        return ""
    return f"\n\nPage {min(max(page, 0), pages - 1) + 1} of {pages}"


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    pct = max(0, min(100, int(done / total * 100)))
    filled = min(width, pct * width // 100)
    return "█" * filled + "░" * (width - filled)


def progress_percent(done: int, total: int) -> int:
    return int(done / total * 100) if total else 0


def progress_line(done: int, total: int) -> str:
    pct = progress_percent(done, total)
    return f"Progress: {done}/{total} ({pct}%)  {progress_bar(done, total)}"


def lecture_title(lecture) -> str:
    title = _display_source_title(lecture)
    order = getattr(lecture, "order", 0) or 0
    match = _LECTURE_PREFIX.match(title)
    if match and order and int(match.group(1)) == int(order):
        title = title[match.end():].strip(" -–—:|.")
    if title:
        return title
    return f"Lecture {order}" if order else "Untitled lecture"


def lecture_label(
    lecture,
    *,
    teacher_name: str | None = None,
    watched: bool = False,
) -> str:
    raw_title = _display_source_title(lecture)
    title = lecture_title(lecture)
    order = getattr(lecture, "order", 0) or 0
    number_match = _LECTURE_NUMBER.search(raw_title)
    already_numbered = bool(
        number_match and int(number_match.group(1)) == int(order)
    )
    leading_number = _LECTURE_PREFIX.match(raw_title)
    label = title
    fallback_title = f"Lecture {order}" if order else ""
    if order and title != fallback_title and (
        not already_numbered
        or (leading_number and title != fallback_title)
    ):
        label = f"L{order} · {title}"
    if teacher_name:
        label += f" · {teacher_name}"
    markers = []
    if watched:
        markers.append("✅")
    if getattr(lecture, "is_premium", False):
        markers.append("💎")
    if markers:
        label = f"{' '.join(markers)} {label}"
    return clip(label)


def lecture_count_label(count: int) -> str:
    return "1 lecture" if count == 1 else f"{count} lectures"
