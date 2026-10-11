"""Parse the intentionally small, member-facing subset of CHANGELOG.md."""
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence


_RELEASE_HEADER = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?:[ \t]+\((?P<title>[^)]+)\))?[ \t]*$",
    re.MULTILINE,
)
_ANNOUNCE_MARKER = re.compile(r"^announce:\s*true\s*$", re.MULTILINE | re.IGNORECASE)
_ANNOUNCEMENT_SECTION = re.compile(
    r"^### Announcement[ \t]*$\n(?P<body>.*?)(?=^#{2,3}\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_BULLET = re.compile(r"^\s*-\s+(?P<text>\S.*)$", re.MULTILINE)


@dataclass(frozen=True)
class ReleaseAnnouncement:
    version: str
    title: str
    bullets: tuple[str, ...]


RELEASE_BOARD_TITLE = "📚 What's new"
BOARD_DESCRIPTION_LIMIT = 4_096
OLDER_UPDATES_NOTE = "_Older updates not shown._"


def _announcement_section(release: ReleaseAnnouncement) -> str:
    bullets = "\n".join(f"• {bullet}" for bullet in release.bullets)
    return f"**{release.version} — {release.title}**\n{bullets}"


def _trim_board(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_release_board(
    announcements: Sequence[ReleaseAnnouncement],
    *,
    limit: int = BOARD_DESCRIPTION_LIMIT,
) -> str:
    """Newest-first changelog body that fits one Discord embed description."""
    if not announcements:
        return ""
    included: list[str] = []
    for announcement in announcements:
        trial = included + [_announcement_section(announcement)]
        omitted = len(announcements) - len(trial)
        body = "\n\n".join(trial)
        if omitted:
            body = f"{body}\n\n{OLDER_UPDATES_NOTE}"
        if len(body) <= limit:
            included = trial
            continue
        if included:
            break
        return _trim_board(trial[0], limit)
    body = "\n\n".join(included)
    omitted = len(announcements) - len(included)
    if omitted:
        suffix = f"\n\n{OLDER_UPDATES_NOTE}"
        if len(body) + len(suffix) > limit:
            body = _trim_board(body, limit - len(suffix))
        body += suffix
    return body


def parse_release_announcements(changelog_path="CHANGELOG.md"):
    """Return only releases expressly approved for member-facing announcements.

    A release must contain both ``announce: true`` and a ``### Announcement``
    section.  This keeps maintenance notes and ordinary commits out of Discord.
    """
    changelog = Path(changelog_path).read_text(encoding="utf-8")
    headers = list(_RELEASE_HEADER.finditer(changelog))
    announcements = []

    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(changelog)
        release = changelog[header.end():end]
        if not _ANNOUNCE_MARKER.search(release):
            continue

        section = _ANNOUNCEMENT_SECTION.search(release)
        if not section:
            continue
        bullets = tuple(match.group("text") for match in _BULLET.finditer(section.group("body")))
        if not bullets:
            continue

        announcements.append(
            ReleaseAnnouncement(
                version=header.group("version"),
                title=header.group("title") or "Book Club Bot update",
                bullets=bullets,
            )
        )

    return announcements


def latest_changelog_version(changelog_path="CHANGELOG.md") -> str:
    """Return the version token of the newest CHANGELOG heading, or ``unknown``."""
    path = Path(changelog_path)
    if not path.is_file():
        path = Path(__file__).resolve().parent / "CHANGELOG.md"
    try:
        match = _RELEASE_HEADER.search(path.read_text(encoding="utf-8"))
    except OSError:
        return "unknown"
    if match:
        return match.group("version")
    return "unknown"


def get_release_announcement(version: str, changelog_path="CHANGELOG.md"):
    """Return the explicit announcement for one app version, if it has one."""
    return next(
        (announcement for announcement in parse_release_announcements(changelog_path)
         if announcement.version == version),
        None,
    )
