from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.ingestion_repo import (
    clear_teacher_if_subject_mismatch,
    clear_topics_if_subject_mismatch,
    ensure_item_mutable,
    find_duplicate_file,
    find_lecture_number_conflict,
    get_item_by_source,
    get_or_create_batch,
    get_setting,
    is_ingestion_paused,
    list_courses,
    set_setting,
)
from bot.db.models import Course, UploadItem
from bot.db.repo import (
    find_matching_teachers,
    find_matching_topics,
    list_item_topics,
    replace_item_topics,
)
from bot.errors import ApprovedBatchError


logger = logging.getLogger(__name__)


LECTURE_PATTERNS = (
    re.compile(r"\b(?:lecture|lec|class|video|part)\s*[-:#.]?\s*(\d{1,3})\b", re.IGNORECASE),
)

LEADING_TITLE_PREFIX = re.compile(
    r"^\s*(?:lecture|lec|class|video|part)\s*[-:#.]?\s*\d{1,3}\s*[-:.]?\s*",
    re.IGNORECASE,
)

# Teacher extraction is intentionally conservative. A trailing segment is only
# removed from the title when it is explicitly marked as a teacher or when it
# matches a teacher in the uniquely identified subject.
TEACHER_SEPARATOR = re.compile(r"(?:\s*[—–]\s*|\s+\|\s+|\s+--\s+|\s+-\s+)")
TEACHER_LABEL_PREFIX = re.compile(
    r"^(?:(?:teacher|faculty)\s*[:\-]\s*|by\s+)", re.IGNORECASE
)
TEACHER_HONORIFIC = re.compile(
    r"^(?:dr|prof|professor|mr|mrs|ms|miss|sir|madam|"
    r"asst\.?\s+prof(?:essor)?|assoc\.?\s+prof(?:essor)?)\b",
    re.IGNORECASE,
)
COURSE_TOKEN = re.compile(r"\w+", re.UNICODE)
CAPTION_EDIT_BLOCKED_STATUSES = frozenset({"skipped", "publishing", "published"})


@dataclass(frozen=True)
class CaptionCandidate:
    """One possible interpretation of a structured caption."""

    format: str
    subject_name: str
    teacher_name: Optional[str]
    topic_name: Optional[str]
    numbers: tuple[int, ...]
    lecture_title: Optional[str]
    teacher_missing: bool
    number_required: bool = True


@dataclass
class ParsedCaption:
    title: str
    lecture_number: Optional[int]
    number_ambiguous: bool
    numbers: list[int]
    teacher_name: Optional[str] = None
    title_without_teacher: Optional[str] = None
    teacher_explicit: bool = False
    subject_name: Optional[str] = None
    topic_name: Optional[str] = None
    structured: bool = False
    teacher_missing: bool = False
    lecture_title: Optional[str] = None
    caption_format: str = "legacy"
    format_error: Optional[str] = None
    ambiguous: bool = False
    number_required: bool = True
    slash_candidates: tuple[CaptionCandidate, ...] = ()
    pipe_candidates: tuple[CaptionCandidate, ...] = ()


SLASH_FIELD_COUNT = 4
NUMBERED_SLASH_FIELD_COUNT = 5
NUMBERED_PIPE_FIELD_COUNT = 5
FINAL_LECTURE_NUMBER = re.compile(r"^[Ll]([1-9]\d{0,2})$")
LECTURE_FIELD_PREFIX = re.compile(
    r"^(?:lecture|lec|class|video|part)\b", re.IGNORECASE
)
FILENAME_LEADING_NUMBER = re.compile(
    r"^\s*"
    r"(?:(?:lecture|lec|class|video|part)\s+)?"
    r"(?:[Ll]\s*)?"
    r"(\d{1,3})"
    r"(?![0-9A-Za-z])"
    r"(?:\s*[.)\-:#_]\s*|\s+|$)",
    re.IGNORECASE,
)
FILENAME_TRIM_CHARS = " \t-—–:|,;._"
FILENAME_NOISE_TOKENS = frozenset({"atf"})


def _extract_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for pattern in LECTURE_PATTERNS:
        for match in pattern.finditer(text):
            number = int(match.group(1))
            if 1 <= number <= 999 and number not in numbers:
                numbers.append(number)

    leading = re.match(r"^\s*(\d{1,3})\s*[.)\-:]\s+", text)
    if leading:
        number = int(leading.group(1))
        if 1 <= number <= 999 and number not in numbers:
            numbers.append(number)
    return numbers


def _clean_filename_title(raw: str) -> Optional[str]:
    title = raw.replace("_", " ")
    title = " ".join(title.split())
    title = title.strip(FILENAME_TRIM_CHARS)
    tokens = title.split()
    while len(tokens) > 1 and tokens[-1] in FILENAME_NOISE_TOKENS:
        tokens.pop()
    title = " ".join(tokens).strip(FILENAME_TRIM_CHARS)
    return title or None


def parse_filename(file_name: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Extract a leading lecture number and cleaned title from a media filename.

    The path and extension are removed, repeated whitespace and underscores are
    normalized, and a recognized standalone trailing noise token is dropped.
    Only a number at the very start of the filename is treated as a lecture
    number. Titles that start with another number are considered ambiguous and
    are not used automatically.
    """
    if not file_name:
        return None, None

    name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.startswith(".") and name.count(".") == 1:
        return None, None
    stem = os.path.splitext(name)[0]
    stem = " ".join(stem.split())
    if not stem:
        return None, None

    match = FILENAME_LEADING_NUMBER.match(stem)
    if match:
        number = int(match.group(1))
        if not 1 <= number <= 999:
            return None, None
        title = _clean_filename_title(stem[match.end() :])
        if not title:
            return number, None
        if title[0].isdigit():
            return None, None
        return number, title

    if stem[0].isdigit():
        return None, None
    return None, _clean_filename_title(stem)


def _split_teacher(title: str) -> tuple[Optional[str], Optional[str], bool]:
    """Split a trailing teacher segment from a title.

    Returns ``(title_without_teacher, teacher_name, explicit)``. ``explicit`` is
    true when the candidate uses a documented label or an honorific, allowing
    the caller to report an unknown teacher without treating every hyphenated
    subtitle (for example "Upper Limb") as a failed teacher match.
    """
    separators = list(TEACHER_SEPARATOR.finditer(title))
    if not separators:
        return None, None, False

    separator = separators[-1]
    base = title[: separator.start()].strip(" \t-—–:|,;")
    candidate = title[separator.end() :].strip(" \t-—–:|,;")
    if not base or not candidate or len(candidate) > 128:
        return None, None, False

    separator_text = separator.group(0)
    explicit = any(marker in separator_text for marker in "—–|")
    label = TEACHER_LABEL_PREFIX.match(candidate)
    if label:
        candidate = candidate[label.end() :].strip(" \t-—–:|,;")
        explicit = True
    if TEACHER_HONORIFIC.match(candidate):
        explicit = True
    if len(candidate) < 2:
        return None, None, False
    return base, candidate, explicit


def _clean_field(value: str) -> str:
    return " ".join(value.split())


def _teacher_field(value: str) -> tuple[Optional[str], bool]:
    candidate = _clean_field(value).strip(" \t---:|,;")
    if not candidate:
        return None, True
    label = TEACHER_LABEL_PREFIX.match(candidate)
    if label:
        candidate = candidate[label.end() :].strip(" \t---:|,;")
    if len(candidate) < 2:
        return None, True
    return candidate[:128], False


def _number_and_title(value: str) -> tuple[list[int], Optional[str]]:
    """Return positive lecture numbers and an optional title suffix.

    The lecture field is intentionally strict: it must start with a number,
    ``L<n>``, or a spoken prefix such as ``Lecture``/``Class``. Arbitrary
    numeric text in a title is not treated as a lecture number.
    """
    cleaned = _clean_field(value)
    if not cleaned:
        return [], None

    short = re.fullmatch(r"[Ll]\s*(\d{1,3})", cleaned)
    if short:
        number = int(short.group(1))
        return ([number], None) if 1 <= number <= 999 else ([], None)

    numbers = _extract_numbers(cleaned)
    if len(numbers) > 1:
        return numbers, None
    match = re.match(
        r"^(?:(?:lecture|lec|class|video|part)\s*[-:#.]?\s*)?"
        r"(\d{1,3})(?![0-9A-Za-z])\s*[.)\-:–—]?\s*(.*)$",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return numbers, None
    number = int(match.group(1))
    if not 1 <= number <= 999:
        return [], None
    remainder = _clean_field(match.group(2)).strip(" \t---:|,;")
    return [number], remainder or None


def _looks_like_lecture_field(value: str) -> bool:
    return bool(LECTURE_FIELD_PREFIX.match(_clean_field(value)))


def _looks_like_teacher_field(value: str) -> bool:
    cleaned = _clean_field(value)
    return bool(
        TEACHER_LABEL_PREFIX.match(cleaned)
        or TEACHER_HONORIFIC.match(cleaned)
    )


def _normalized_phrase(value: str) -> str:
    return " ".join(
        re.sub(r"[^\w\s]", " ", value or "").casefold().split()
    )


def _preferred_video_title_error(
    title: Optional[str], subject: str, topic: str
) -> Optional[str]:
    clean = _clean_field(title or "")
    if not clean:
        return "video title missing"
    numbers, remainder = _number_and_title(clean)
    if _looks_like_lecture_field(clean) or (numbers and remainder is None):
        return f"generic video title: {clean}"
    normalized = _normalized_phrase(clean)
    generic = {
        _normalized_phrase(subject),
        _normalized_phrase(topic),
    } - {""}
    if normalized in generic:
        return f"generic video title: {clean}"
    return None


def _caption_candidate(
    *,
    format: str,
    subject: str,
    teacher_raw: str,
    topic: str,
    numbers: list[int],
    lecture_title: Optional[str],
    number_required: bool = True,
) -> CaptionCandidate:
    teacher, teacher_missing = _teacher_field(teacher_raw)
    return CaptionCandidate(
        format=format,
        subject_name=_clean_field(subject),
        teacher_name=teacher,
        topic_name=_clean_field(topic) or None,
        numbers=tuple(numbers),
        lecture_title=lecture_title,
        teacher_missing=teacher_missing,
        number_required=number_required,
    )


def _parsed_from_caption_candidate(
    candidate: CaptionCandidate,
    *,
    raw_line: str,
    format_error: Optional[str] = None,
    ambiguous: bool = False,
) -> ParsedCaption:
    numbers = list(candidate.numbers)
    number_ambiguous = len(numbers) > 1
    lecture_number = numbers[0] if len(numbers) == 1 else None
    title = (
        candidate.lecture_title
        or candidate.topic_name
        or candidate.subject_name
        or raw_line
        or "Untitled"
    )
    return ParsedCaption(
        title=title[:256],
        lecture_number=lecture_number,
        number_ambiguous=number_ambiguous,
        numbers=numbers,
        teacher_name=candidate.teacher_name,
        teacher_explicit=(
            candidate.teacher_name is not None and not candidate.teacher_missing
        ),
        subject_name=candidate.subject_name or None,
        topic_name=candidate.topic_name,
        structured=True,
        teacher_missing=candidate.teacher_missing,
        lecture_title=candidate.lecture_title,
        caption_format=candidate.format,
        format_error=format_error,
        ambiguous=ambiguous,
        number_required=candidate.number_required,
    )


def _ambiguous_slash_caption(
    line: str,
    preferred: CaptionCandidate,
    legacy: CaptionCandidate,
    numbers: list[int],
) -> ParsedCaption:
    return ParsedCaption(
        title=line[:256],
        lecture_number=None,
        number_ambiguous=bool(numbers),
        numbers=numbers,
        structured=True,
        ambiguous=True,
        caption_format="slash_ambiguous",
        format_error=(
            "ambiguous slash caption format: fields could match both the "
            "current and legacy layouts"
        ),
        slash_candidates=(preferred, legacy),
    )


def _number_like_final_field(value: str) -> bool:
    cleaned = _clean_field(value)
    if not cleaned:
        return False
    if re.fullmatch(r"\d{1,3}", cleaned):
        return True
    if cleaned.startswith(("L", "l")) and any(ch.isdigit() for ch in cleaned):
        return True
    return _looks_like_lecture_field(cleaned) and any(
        ch.isdigit() for ch in cleaned
    )


def _parse_numbered_slash_line(line: str) -> Optional[ParsedCaption]:
    """Parse ``Subject / Teacher / Subtopic / Video Title / L<number>``.

    The final field is the only place a lecture number is read from. If the
    fourth field contains an ``L<n>``-looking phrase it remains part of the
    video title. A malformed final number field is a reviewable format error.
    """
    parts = [part.strip() for part in line.split("/")]
    if len(parts) != NUMBERED_SLASH_FIELD_COUNT:
        return None

    number_field = _clean_field(parts[4])
    match = FINAL_LECTURE_NUMBER.fullmatch(number_field)
    number: Optional[int] = None
    format_error: Optional[str] = None
    if match:
        candidate_number = int(match.group(1))
        if 1 <= candidate_number <= 999:
            number = candidate_number
        else:
            format_error = f"invalid lecture number: {number_field}"
    elif _number_like_final_field(number_field):
        format_error = f"invalid lecture number: {number_field}"
    else:
        return None

    video_title = _clean_field(parts[3])
    candidate = _caption_candidate(
        format="slash_numbered",
        subject=parts[0],
        teacher_raw=parts[1],
        topic=parts[2],
        numbers=[number] if number is not None else [],
        lecture_title=video_title or None,
        number_required=True,
    )
    if format_error is None:
        format_error = _preferred_video_title_error(
            video_title, parts[0], parts[2]
        )
    return _parsed_from_caption_candidate(
        candidate, raw_line=line, format_error=format_error
    )


def _parse_slash_line(line: str) -> Optional[ParsedCaption]:
    parts = [part.strip() for part in line.split("/")]
    if len(parts) == NUMBERED_SLASH_FIELD_COUNT:
        numbered = _parse_numbered_slash_line(line)
        if numbered is not None:
            return numbered
    if len(parts) != SLASH_FIELD_COUNT:
        return None

    video_title = _clean_field(parts[3])
    legacy_numbers, legacy_title = _number_and_title(parts[2])
    preferred_number_like, _ = _number_and_title(parts[3])
    preferred = _caption_candidate(
        format="slash_preferred",
        subject=parts[0],
        teacher_raw=parts[1],
        topic=parts[2],
        numbers=[],
        lecture_title=video_title or None,
        number_required=False,
    )
    legacy = _caption_candidate(
        format="slash_legacy",
        subject=parts[0],
        teacher_raw=parts[3],
        topic=parts[1],
        numbers=legacy_numbers,
        lecture_title=legacy_title,
        number_required=True,
    )

    second_teacher = _looks_like_teacher_field(parts[1])
    fourth_teacher = _looks_like_teacher_field(parts[3])
    third_lecture = bool(legacy_numbers) or _looks_like_lecture_field(parts[2])
    fourth_lecture = bool(preferred_number_like) or _looks_like_lecture_field(
        parts[3]
    )

    if second_teacher and fourth_teacher:
        return _ambiguous_slash_caption(
            line,
            preferred,
            legacy,
            sorted(set(legacy_numbers + preferred_number_like)),
        )
    if fourth_teacher:
        if legacy_numbers:
            return _parsed_from_caption_candidate(legacy, raw_line=line)
        error = (
            f"invalid lecture number: {parts[2]}"
            if third_lecture
            else "lecture number missing"
        )
        return _parsed_from_caption_candidate(
            legacy, raw_line=line, format_error=error
        )
    if second_teacher:
        return _parsed_from_caption_candidate(
            preferred,
            raw_line=line,
            format_error=_preferred_video_title_error(
                video_title, parts[0], parts[2]
            ),
        )

    if legacy_numbers and fourth_lecture:
        return _ambiguous_slash_caption(
            line,
            preferred,
            legacy,
            sorted(set(legacy_numbers + preferred_number_like)),
        )
    if legacy_numbers:
        return _parsed_from_caption_candidate(legacy, raw_line=line)
    if third_lecture:
        return _parsed_from_caption_candidate(
            legacy,
            raw_line=line,
            format_error=f"invalid lecture number: {parts[2]}",
        )

    return _parsed_from_caption_candidate(
        preferred,
        raw_line=line,
        format_error=_preferred_video_title_error(
            video_title, parts[0], parts[2]
        ),
    )


def _parse_numbered_pipe_line(line: str) -> Optional[ParsedCaption]:
    """Parse ``Subject | Teacher | Subtopic | Video Title | L<number>``.

    The final field is the only place a lecture number is read from. If the
    fourth field contains an ``L<n>``-looking phrase it remains part of the
    video title. A malformed final number field is a reviewable format error.
    """
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != NUMBERED_PIPE_FIELD_COUNT:
        return None

    number_field = _clean_field(parts[4])
    match = FINAL_LECTURE_NUMBER.fullmatch(number_field)
    number: Optional[int] = None
    format_error: Optional[str] = None
    if not number_field:
        format_error = "lecture number missing"
    elif match:
        number = int(match.group(1))
    elif _number_like_final_field(number_field):
        format_error = f"invalid lecture number: {number_field}"
    else:
        return None

    video_title = _clean_field(parts[3])
    candidate = _caption_candidate(
        format="pipe_numbered",
        subject=parts[0],
        teacher_raw=parts[1],
        topic=parts[2],
        numbers=[number] if number is not None else [],
        lecture_title=video_title or None,
        number_required=True,
    )
    if format_error is None:
        format_error = _preferred_video_title_error(
            video_title, parts[0], parts[2]
        )
    return _parsed_from_caption_candidate(
        candidate, raw_line=line, format_error=format_error
    )


def _ambiguous_pipe_caption(
    line: str,
    preferred: CaptionCandidate,
    legacy: CaptionCandidate,
    numbers: list[int],
) -> ParsedCaption:
    return ParsedCaption(
        title=line[:256],
        lecture_number=None,
        number_ambiguous=bool(numbers),
        numbers=numbers,
        structured=True,
        ambiguous=True,
        caption_format="pipe_ambiguous",
        format_error=(
            "ambiguous pipe caption format: fields could match both the "
            "preferred and legacy layouts"
        ),
        pipe_candidates=(preferred, legacy),
    )


def _parse_pipe_line(line: str) -> Optional[ParsedCaption]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 3:
        return None

    if len(parts) == NUMBERED_PIPE_FIELD_COUNT:
        numbered = _parse_numbered_pipe_line(line)
        if numbered is not None:
            return numbered

    subject = parts[0]
    second = parts[1]
    third = parts[2]
    tail = " | ".join(parts[3:]).strip()
    legacy_numbers, legacy_title = _number_and_title(third)
    preferred_number_like, _ = _number_and_title(tail)

    preferred = _caption_candidate(
        format="pipe_preferred",
        subject=subject,
        teacher_raw=second,
        topic=third,
        numbers=[],
        lecture_title=tail or None,
        number_required=False,
    )
    legacy = _caption_candidate(
        format="pipe_legacy",
        subject=subject,
        teacher_raw=tail,
        topic=second,
        numbers=legacy_numbers,
        lecture_title=legacy_title,
        number_required=True,
    )

    if len(parts) == 3:
        if legacy_numbers:
            return _parsed_from_caption_candidate(legacy, raw_line=line)
        return None

    second_teacher = _looks_like_teacher_field(second)
    fourth_teacher = _looks_like_teacher_field(parts[3])
    third_lecture = bool(legacy_numbers) or _looks_like_lecture_field(third)

    if second_teacher and fourth_teacher:
        return _ambiguous_pipe_caption(
            line,
            preferred,
            legacy,
            sorted(set(legacy_numbers + preferred_number_like)),
        )
    if legacy_numbers:
        if second_teacher:
            return _ambiguous_pipe_caption(
                line,
                preferred,
                legacy,
                sorted(set(legacy_numbers + preferred_number_like)),
            )
        return _parsed_from_caption_candidate(legacy, raw_line=line)
    if fourth_teacher:
        error = (
            f"invalid lecture number: {third}"
            if third_lecture
            else "lecture number missing"
        )
        return _parsed_from_caption_candidate(
            legacy, raw_line=line, format_error=error
        )
    if second_teacher:
        return _parsed_from_caption_candidate(
            preferred,
            raw_line=line,
            format_error=_preferred_video_title_error(
                preferred.lecture_title, subject, third
            ),
        )
    if third_lecture:
        return _parsed_from_caption_candidate(
            legacy,
            raw_line=line,
            format_error=f"invalid lecture number: {third}",
        )

    return _ambiguous_pipe_caption(
        line,
        preferred,
        legacy,
        sorted(set(legacy_numbers + preferred_number_like)),
    )


def parse_caption(caption: Optional[str], file_name: Optional[str]) -> ParsedCaption:
    text = (caption or "").strip()

    first_line = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break

    if "|" in first_line:
        pipe = _parse_pipe_line(first_line)
        if pipe is not None:
            return pipe
    if "/" in first_line:
        slash = _parse_slash_line(first_line)
        if slash is not None:
            return slash

    file_number, file_title = parse_filename(file_name)
    numbers = _extract_numbers(text)
    if not numbers and file_number is not None:
        numbers = [file_number]
    if not numbers and file_name:
        stem = os.path.splitext(os.path.basename(file_name))[0].strip()
        numbers = _extract_numbers(stem)

    number_ambiguous = len(numbers) > 1
    lecture_number = numbers[0] if len(numbers) == 1 else None

    title = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            title = line
            break

    if not title and file_title:
        title = file_title
    elif not title and file_name:
        title = os.path.splitext(os.path.basename(file_name))[0].strip()

    if title:
        cleaned = LEADING_TITLE_PREFIX.sub("", title, count=1).strip()
        if cleaned:
            title = cleaned

    title_without_teacher = None
    teacher_name = None
    teacher_explicit = False
    if title:
        title_without_teacher, teacher_name, teacher_explicit = _split_teacher(title)

    return ParsedCaption(
        title=(title or "Untitled")[:256],
        lecture_number=lecture_number,
        number_ambiguous=number_ambiguous,
        numbers=numbers,
        teacher_name=teacher_name,
        title_without_teacher=title_without_teacher,
        teacher_explicit=teacher_explicit,
    )


def preferred_caption_error(
    caption: Optional[str], file_name: Optional[str]
) -> Optional[str]:
    """Return why a caption is not a valid preferred-layout caption."""
    parsed = parse_caption(caption, file_name)
    if not parsed.structured:
        return "caption is not structured"
    if (
        parsed.ambiguous
        or parsed.pipe_candidates
        or parsed.slash_candidates
    ):
        return parsed.format_error or "ambiguous caption layout"
    if parsed.caption_format not in {
        "pipe_preferred",
        "pipe_numbered",
        "slash_preferred",
        "slash_numbered",
    }:
        return f"caption uses the {parsed.caption_format} layout"
    if parsed.format_error:
        return parsed.format_error
    if not parsed.subject_name:
        return "subject missing"
    if parsed.teacher_missing or not parsed.teacher_name:
        return "teacher missing"
    if not parsed.topic_name:
        return "topic missing"
    if not (parsed.lecture_title or "").strip():
        return "video title missing"
    return None


def _course_candidates(course: Course) -> list[str]:
    candidates = [course.name]
    if course.aliases:
        candidates.extend(part.strip() for part in course.aliases.split(","))
    return [c for c in candidates if len(c.strip()) >= 3]


def _match_tokens(text: str) -> list[str]:
    return COURSE_TOKEN.findall(text.casefold())


def _contains_token_phrase(text_tokens: list[str], candidate: str) -> bool:
    candidate_tokens = _match_tokens(candidate)
    if not candidate_tokens:
        return False
    width = len(candidate_tokens)
    return any(
        text_tokens[index : index + width] == candidate_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def _contains_token_prefix(text_tokens: list[str], candidate: str) -> bool:
    candidate_tokens = _match_tokens(candidate)
    if len(candidate_tokens) != 1:
        return False
    return any(token.startswith(candidate_tokens[0]) for token in text_tokens)


def match_courses(courses: list[Course], text: str) -> list[Course]:
    """Match exact course words first, with unique aliases as a safe fallback.

    Whole-token phrase matches prevent ``Physiology`` from matching inside
    ``Pathophysiology``. A single-token alias such as ``anat`` may match the
    start of ``Anatomy`` only when no exact course-name/alias match exists.
    Multiple matches at either level remain ambiguous and are reviewed.
    """
    text_tokens = _match_tokens(text)
    exact: list[Course] = []
    prefix: list[Course] = []
    for course in courses:
        candidates = _course_candidates(course)
        if any(
            _contains_token_phrase(text_tokens, candidate)
            for candidate in candidates
        ):
            exact.append(course)
        elif any(
            _contains_token_prefix(text_tokens, candidate)
            for candidate in candidates
        ):
            prefix.append(course)
    return exact or prefix


async def _select_slash_candidate(
    session: AsyncSession,
    courses: list[Course],
    candidates: tuple[CaptionCandidate, ...],
) -> Optional[CaptionCandidate]:
    """Use existing records to resolve a numerically ambiguous slash caption."""
    fully_resolved: list[CaptionCandidate] = []
    for candidate in candidates:
        if (
            candidate.teacher_missing
            or candidate.teacher_name is None
            or candidate.topic_name is None
            or (
                candidate.number_required
                and len(candidate.numbers) != 1
            )
        ):
            continue
        if candidate.format == "slash_preferred" and _preferred_video_title_error(
            candidate.lecture_title,
            candidate.subject_name,
            candidate.topic_name or "",
        ):
            continue
        course_matches = match_courses(courses, candidate.subject_name)
        if len(course_matches) != 1:
            continue
        subject_id = course_matches[0].subject_id
        teachers = await find_matching_teachers(
            session, subject_id, candidate.teacher_name
        )
        topics = await find_matching_topics(
            session, subject_id, candidate.topic_name
        )
        if len(teachers) == 1 and len(topics) == 1:
            fully_resolved.append(candidate)
    if len(fully_resolved) == 1:
        return fully_resolved[0]
    return None


async def _select_pipe_candidate(
    session: AsyncSession,
    courses: list[Course],
    candidates: tuple[CaptionCandidate, ...],
) -> Optional[CaptionCandidate]:
    """Use existing teacher names and aliases to resolve a pipe layout."""
    evidence: list[tuple[CaptionCandidate, int]] = []
    for candidate in candidates:
        if (
            candidate.teacher_missing
            or candidate.teacher_name is None
            or (
                candidate.number_required
                and len(candidate.numbers) != 1
            )
        ):
            evidence.append((candidate, 0))
            continue
        course_matches = match_courses(courses, candidate.subject_name)
        if len(course_matches) != 1:
            evidence.append((candidate, 0))
            continue
        teachers = await find_matching_teachers(
            session, course_matches[0].subject_id, candidate.teacher_name
        )
        evidence.append((candidate, len(teachers)))

    matched = [candidate for candidate, count in evidence if count == 1]
    if len(matched) == 1 and sum(count for _, count in evidence) == 1:
        return matched[0]
    return None


async def classify_item(session: AsyncSession, item: UploadItem) -> UploadItem:
    parsed = parse_caption(item.raw_caption, item.file_name)
    courses = await list_courses(session)

    if parsed.pipe_candidates:
        selected = await _select_pipe_candidate(
            session, courses, parsed.pipe_candidates
        )
        if selected is not None:
            format_error = None
            if selected.format == "pipe_preferred":
                format_error = _preferred_video_title_error(
                    selected.lecture_title,
                    selected.subject_name,
                    selected.topic_name or "",
                )
            parsed = _parsed_from_caption_candidate(
                selected,
                raw_line=(item.raw_caption or "").strip(),
                format_error=format_error,
            )
        else:
            parsed = ParsedCaption(
                title=parsed.title,
                lecture_number=None,
                number_ambiguous=parsed.number_ambiguous,
                numbers=parsed.numbers,
                structured=True,
                ambiguous=True,
                caption_format="pipe_ambiguous",
                format_error=(
                    parsed.format_error
                    or "ambiguous pipe caption format: existing records "
                    "do not uniquely resolve the fields"
                ),
                pipe_candidates=parsed.pipe_candidates,
            )

    if parsed.slash_candidates:
        selected = await _select_slash_candidate(
            session, courses, parsed.slash_candidates
        )
        if selected is not None:
            parsed = _parsed_from_caption_candidate(
                selected,
                raw_line=(item.raw_caption or "").strip(),
            )
        else:
            parsed = ParsedCaption(
                title=parsed.title,
                lecture_number=None,
                number_ambiguous=True,
                numbers=parsed.numbers,
                structured=True,
                ambiguous=True,
                caption_format="slash_ambiguous",
                format_error=(
                    parsed.format_error
                    or "ambiguous slash caption format: existing records "
                    "do not uniquely resolve the fields"
                ),
            )

    item.title = parsed.title
    item.lecture_number = parsed.lecture_number

    if parsed.structured:
        match_text = parsed.subject_name or ""
    else:
        match_text = " ".join(
            part for part in (item.raw_caption, item.file_name, item.title) if part
        )
    matches = match_courses(courses, match_text)

    reasons: list[str] = []
    if item.duplicate_flag:
        reasons.append("suspected duplicate file")
    if parsed.format_error:
        reasons.append(parsed.format_error)

    course = None
    if len(matches) == 0:
        item.course_id = None
        reasons.append("course/year not confidently identified")
    elif len(matches) > 1:
        item.course_id = None
        reasons.append("multiple course matches")
    else:
        course = matches[0]
        item.course_id = course.id
        if await clear_teacher_if_subject_mismatch(
            session, item, course.subject_id
        ):
            logger.info(
                "Cleared incompatible teacher from upload item #%s after "
                "course reclassification",
                item.id,
            )
        await clear_topics_if_subject_mismatch(
            session, item, course.subject_id
        )

    teacher_reason = None
    auto_teacher_id = None
    manual_teacher = item.teacher_source == "manual" or (
        item.teacher_id is not None and item.teacher_source is None
    )
    if course is None:
        if not manual_teacher:
            if item.teacher_source == "auto":
                item.teacher_id = None
                item.teacher_source = None
            if parsed.teacher_missing:
                teacher_reason = "teacher missing"
            elif (
                parsed.teacher_name
                and parsed.teacher_explicit
                and item.teacher_id is None
            ):
                teacher_reason = (
                    "teacher not resolved: subject not uniquely identified"
                )
    else:
        if not manual_teacher:
            if parsed.teacher_missing:
                if item.teacher_source == "auto":
                    item.teacher_id = None
                    item.teacher_source = None
                teacher_reason = "teacher missing"
            elif parsed.teacher_name:
                teachers = await find_matching_teachers(
                    session, course.subject_id, parsed.teacher_name
                )
                if len(teachers) == 1:
                    item.teacher_id = teachers[0].id
                    item.teacher_source = "auto"
                    auto_teacher_id = teachers[0].id
                else:
                    if item.teacher_source == "auto":
                        item.teacher_id = None
                        item.teacher_source = None
                    if len(teachers) > 1:
                        teacher_reason = (
                            f"ambiguous teacher: {parsed.teacher_name}"
                        )
                    elif parsed.teacher_explicit:
                        teacher_reason = (
                            f"teacher not found: {parsed.teacher_name}"
                        )
            elif item.teacher_source == "auto":
                item.teacher_id = None
                item.teacher_source = None

    item.teacher_review_reason = teacher_reason
    if teacher_reason:
        reasons.append(teacher_reason)

    if (
        parsed.title_without_teacher
        and (parsed.teacher_explicit or auto_teacher_id is not None)
    ):
        item.title = parsed.title_without_teacher[:256]

    manual_topic = item.topic_source == "manual"
    topic_reason = None
    if parsed.structured:
        if not manual_topic:
            if not parsed.topic_name:
                await replace_item_topics(session, item.id, [])
                item.topic_source = None
                topic_reason = "topic missing"
            elif course is None:
                await replace_item_topics(session, item.id, [])
                item.topic_source = None
                topic_reason = (
                    "topic not resolved: subject not uniquely identified"
                )
            else:
                topics = await find_matching_topics(
                    session, course.subject_id, parsed.topic_name
                )
                if len(topics) == 1:
                    await replace_item_topics(
                        session, item.id, [topics[0].id]
                    )
                    item.topic_source = "auto"
                    item.title = (
                        parsed.lecture_title or topics[0].name
                    )[:256]
                else:
                    await replace_item_topics(session, item.id, [])
                    item.topic_source = None
                    if len(topics) > 1:
                        topic_reason = (
                            f"ambiguous topic: {parsed.topic_name}"
                        )
                    else:
                        topic_reason = (
                            f"topic not found: {parsed.topic_name}"
                        )
    elif not manual_topic:
        await replace_item_topics(session, item.id, [])
        item.topic_source = None

    item.topic_review_reason = topic_reason
    if topic_reason:
        reasons.append(topic_reason)

    if (
        parsed.lecture_number is not None
        and item.course_id is not None
        and item.id is not None
    ):
        topic_ids = [
            topic.id for topic in await list_item_topics(session, item.id)
        ]
        conflict = await find_lecture_number_conflict(
            session,
            item_id=item.id,
            course_id=item.course_id,
            lecture_number=parsed.lecture_number,
            topic_ids=topic_ids,
        )
        if conflict is not None:
            reasons.append(
                f"duplicate lecture number L{parsed.lecture_number} in this "
                f"course/topic (item #{conflict.id})"
            )

    if parsed.number_ambiguous:
        reasons.append("ambiguous lecture number")
    elif parsed.lecture_number is None and parsed.number_required:
        reasons.append("lecture number missing")

    item.review_reason = "; ".join(reasons) if reasons else None
    item.status = "needs_review" if reasons else "classified"
    item.error = None
    return item


async def ingest_media(
    session: AsyncSession,
    *,
    source_chat_id: int,
    source_message_id: int,
    media_type: str,
    caption: Optional[str],
    file_name: Optional[str],
    file_unique_id: Optional[str],
    file_id: Optional[str] = None,
    forward_from_chat_id: Optional[int] = None,
    forward_from_message_id: Optional[int] = None,
) -> tuple[UploadItem, bool]:
    existing = await get_item_by_source(session, source_chat_id, source_message_id)
    if existing:
        return existing, False

    paused = await is_ingestion_paused(session)
    batch = await get_or_create_batch(session, source_chat_id, datetime.now(timezone.utc).date())
    parsed = parse_caption(caption, file_name)

    item = UploadItem(
        batch_id=batch.id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        forward_from_chat_id=forward_from_chat_id,
        forward_from_message_id=forward_from_message_id,
        media_type=media_type,
        file_name=file_name,
        file_id=file_id,
        file_unique_id=file_unique_id,
        raw_caption=caption,
        original_caption=caption,
        title=parsed.title,
        lecture_number=parsed.lecture_number,
        status="paused" if paused else "received",
    )
    session.add(item)
    try:
        await session.commit()
        await session.refresh(item)
    except IntegrityError:
        await session.rollback()
        existing = await get_item_by_source(session, source_chat_id, source_message_id)
        if existing:
            return existing, False
        raise

    item_id = item.id
    if file_unique_id:
        duplicate = await find_duplicate_file(
            session, file_unique_id, item_id, source_chat_id, media_type
        )
        if duplicate:
            item.duplicate_flag = True
            item.duplicate_of_item_id = duplicate.id

    if paused:
        await session.commit()
        await session.refresh(item)
        return item, True

    try:
        await classify_item(session, item)
        await session.commit()
        await session.refresh(item)
    except Exception as exc:  # keep the stored item, record the failure
        logger.exception("Failed to classify upload item #%s", item_id)
        await session.rollback()
        failed = await session.get(UploadItem, item_id)
        if failed:
            failed.status = "failed"
            failed.error = str(exc)[:1000]
            await session.commit()
            await session.refresh(failed)
            return failed, True
        raise

    return item, True


async def retry_item(session: AsyncSession, item_id: int) -> Optional[UploadItem]:
    item = await session.get(UploadItem, item_id)
    if not item:
        return None
    await ensure_item_mutable(session, item)
    item.status = "received"
    item.error = None
    item.review_reason = None
    item.teacher_review_reason = None
    item.topic_review_reason = None
    if item.file_unique_id:
        duplicate = await find_duplicate_file(
            session, item.file_unique_id, item.id, item.source_chat_id, item.media_type
        )
        if duplicate:
            item.duplicate_flag = True
            item.duplicate_of_item_id = duplicate.id
    await session.commit()
    await classify_item(session, item)
    await session.commit()
    await session.refresh(item)
    return item


async def reclassify_item_caption(
    session: AsyncSession, item_id: int
) -> tuple[Optional[UploadItem], str]:
    """Reclassify an item using its current caption without rewriting it."""
    item = await session.get(UploadItem, item_id)
    if not item:
        return None, "not_found"
    if item.status == "skipped":
        return item, "skipped"
    if (
        item.status in CAPTION_EDIT_BLOCKED_STATUSES
        or item.publication_status in {"publishing", "published"}
    ):
        return item, "published"

    try:
        await ensure_item_mutable(session, item)
    except ApprovedBatchError:
        return item, "approved"

    item.status = "received"
    item.error = None
    item.review_reason = None
    item.teacher_review_reason = None
    item.topic_review_reason = None
    await session.commit()

    try:
        await classify_item(session, item)
        await session.commit()
        await session.refresh(item)
    except Exception as exc:  # keep the item and record a retryable failure
        logger.exception("Failed to reclassify upload item #%s", item_id)
        await session.rollback()
        failed = await session.get(UploadItem, item_id)
        if failed:
            failed.status = "failed"
            failed.error = str(exc)[:1000]
            await session.commit()
            await session.refresh(failed)
            return failed, "failed"
        raise
    return item, "reclassified"


async def _replace_item_caption(
    session: AsyncSession,
    item: UploadItem,
    caption: Optional[str],
    *,
    allow_empty: bool,
) -> tuple[Optional[UploadItem], Optional[str]]:
    clean_caption = (caption or "").strip()
    if not clean_caption and not allow_empty:
        return None, "Caption is required."

    if item.original_caption is None:
        item.original_caption = item.raw_caption
    item.raw_caption = clean_caption or None
    item.status = "received"
    item.error = None
    item.review_reason = None
    item.teacher_review_reason = None
    item.topic_review_reason = None
    await session.commit()

    await classify_item(session, item)
    await session.commit()
    await session.refresh(item)
    return item, None


async def set_item_caption(
    session: AsyncSession, item_id: int, caption: str
) -> tuple[Optional[UploadItem], Optional[str]]:
    """Edit an unapproved item's caption and reclassify it safely.

    The first edit preserves the previous caption in ``original_caption`` so
    the original metadata remains available for audit and recovery.
    """
    item = await session.get(UploadItem, item_id)
    if not item:
        return None, "Item not found."
    await ensure_item_mutable(session, item)
    if item.status == "skipped":
        return None, "Item is skipped. Use /keep to restore it before editing the caption."
    return await _replace_item_caption(
        session, item, caption, allow_empty=False
    )


async def update_ingested_caption(
    session: AsyncSession,
    *,
    source_chat_id: int,
    source_message_id: int,
    caption: Optional[str],
) -> tuple[Optional[UploadItem], str]:
    """Apply a Telegram caption edit to an existing upload.

    Only metadata for an already stored, mutable item is changed. Approved,
    skipped, publishing, or published items are left untouched. Missing items
    are not created from an edit event.
    """
    item = await get_item_by_source(
        session, source_chat_id, source_message_id
    )
    if item is None:
        return None, "not_ingested"

    clean_caption = (caption or "").strip()
    if (item.raw_caption or "").strip() == clean_caption:
        return item, "unchanged"

    if item.status == "skipped":
        return item, "skipped"
    if (
        item.status in CAPTION_EDIT_BLOCKED_STATUSES
        or item.publication_status in {"publishing", "published"}
    ):
        return item, "published"

    try:
        await ensure_item_mutable(session, item)
    except ApprovedBatchError:
        return item, "approved"

    item, error = await _replace_item_caption(
        session, item, caption, allow_empty=True
    )
    if error:
        return item, "error"
    return item, "updated"


async def keep_item(session: AsyncSession, item_id: int) -> Optional[UploadItem]:
    item = await session.get(UploadItem, item_id)
    if not item:
        return None
    await ensure_item_mutable(session, item)
    item.duplicate_flag = False
    item.duplicate_of_item_id = None
    item.status = "received"
    item.review_reason = None
    item.teacher_review_reason = None
    item.topic_review_reason = None
    await session.commit()
    await classify_item(session, item)
    await session.commit()
    await session.refresh(item)
    return item


async def pause_ingestion(session: AsyncSession) -> None:
    await set_setting(session, "ingestion_paused", "1")


async def resume_ingestion(session: AsyncSession) -> int:
    await set_setting(session, "ingestion_paused", "0")
    from sqlalchemy import select

    res = await session.execute(select(UploadItem).where(UploadItem.status == "paused"))
    items = list(res.scalars().all())
    for item in items:
        await classify_item(session, item)
    await session.commit()
    return len(items)


async def ingestion_is_paused(session: AsyncSession) -> bool:
    return (await get_setting(session, "ingestion_paused", "0")) == "1"
