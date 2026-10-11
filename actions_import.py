"""Goodreads finished-history CSV import and personal shelf CSV export."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
import csv

from actions import GOODREADS_SOURCE, get_or_create_book, get_or_create_user, log_event
from database import connect

MAX_READ_ROWS = 2000
MAX_FILE_BYTES = 5 * 1024 * 1024
REQUIRED_HEADERS = ("title", "exclusive shelf")
# Spreadsheet apps evaluate cells starting with these; export prefixes a quote.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class ImportRow:
    title: str
    author: str | None
    score: int | None
    read_at: str | None


@dataclass
class ImportPreview:
    ok: bool
    error: str | None = None
    to_add: list[ImportRow] = field(default_factory=list)
    already_on_shelf: int = 0
    to_read_ignored: int = 0
    currently_reading_ignored: int = 0
    dnf_ignored: int = 0
    other_ignored: int = 0
    blank_skipped: int = 0
    truncated: bool = False


@dataclass
class ImportApplyResult:
    added: int = 0
    already_on_shelf: int = 0
    ratings_added: int = 0


def _header_index(headers: list[str]) -> dict[str, int]:
    return {h.strip().lower(): i for i, h in enumerate(headers) if h and h.strip()}


def _cell(row: list[str], index: dict[str, int], name: str) -> str:
    i = index.get(name)
    if i is None or i >= len(row):
        return ""
    return (row[i] or "").strip()


def _unescape_formula(text: str) -> str:
    """Undo the export's leading-quote guard so titles round-trip."""
    if len(text) > 1 and text[0] == "'" and text[1].startswith(FORMULA_PREFIXES):
        return text[1:]
    return text


def _has_dnf_shelf(bookshelves: str) -> bool:
    return any(part.strip().lower() == "dnf" for part in (bookshelves or "").split(","))


def _parse_gr_date(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _parse_rating(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        stars = int(text)
    except (TypeError, ValueError):
        return None
    if 1 <= stars <= 5:
        return stars * 2
    return None


def parse_goodreads_csv(text: str, *, existing_shelf_titles: set[str] | None = None) -> ImportPreview:
    """Parse a Goodreads library export. Only Exclusive Shelf = read is eligible."""
    if not text or not str(text).strip():
        return ImportPreview(ok=False, error="not_goodreads")
    reader = csv.reader(StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return ImportPreview(ok=False, error="not_goodreads")
    index = _header_index(headers)
    if any(name not in index for name in REQUIRED_HEADERS):
        return ImportPreview(ok=False, error="not_goodreads")

    existing = {t.lower() for t in (existing_shelf_titles or set())}
    seen_this_file: set[str] = set()
    preview = ImportPreview(ok=True)
    read_seen = 0

    for raw in reader:
        if not raw or all(not (c or "").strip() for c in raw):
            continue
        title = _unescape_formula(_cell(raw, index, "title"))
        shelf = _cell(raw, index, "exclusive shelf").lower()
        if not title:
            preview.blank_skipped += 1
            continue
        if shelf != "read":
            if shelf == "to-read":
                preview.to_read_ignored += 1
            elif shelf == "currently-reading":
                preview.currently_reading_ignored += 1
            else:
                preview.other_ignored += 1
            continue
        if _has_dnf_shelf(_cell(raw, index, "bookshelves")):
            preview.dnf_ignored += 1
            continue

        read_seen += 1
        if read_seen > MAX_READ_ROWS:
            preview.truncated = True
            continue

        key = title.lower()
        if key in existing or key in seen_this_file:
            preview.already_on_shelf += 1
            continue
        seen_this_file.add(key)
        author = _unescape_formula(_cell(raw, index, "author")) or None
        date_read = _parse_gr_date(_cell(raw, index, "date read"))
        date_added = _parse_gr_date(_cell(raw, index, "date added"))
        preview.to_add.append(
            ImportRow(
                title=title,
                author=author,
                score=_parse_rating(_cell(raw, index, "my rating")),
                read_at=date_read or date_added,
            )
        )

    return preview


def existing_shelf_titles(discord_id: str, guild_id: str) -> set[str]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT LOWER(b.title)
               FROM reading_progress rp
               JOIN books b ON b.book_id = rp.book_id
               JOIN users u ON u.user_id = rp.user_id
               WHERE u.discord_id = ? AND rp.guild_id = ?""",
            (discord_id, guild_id),
        )
        return {row[0] for row in cur.fetchall() if row[0]}
    finally:
        conn.close()


def apply_goodreads_import(
    discord_id: str,
    display_name: str,
    guild_id: str,
    rows: list[ImportRow],
) -> ImportApplyResult:
    """Insert finished history in one transaction. No Google Books, no achievements."""
    result = ImportApplyResult()
    if not guild_id or not rows:
        return result
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        cur = conn.cursor()
        for row in rows:
            title = (row.title or "").strip()
            if not title:
                continue
            book_id = get_or_create_book(
                conn, title, row.author, enrich=False, commit=False
            )
            cur.execute(
                """SELECT 1 FROM reading_progress
                   WHERE user_id = ? AND book_id = ? AND guild_id = ?
                   LIMIT 1""",
                (user_id, book_id, guild_id),
            )
            if cur.fetchone():
                result.already_on_shelf += 1
                continue
            cur.execute(
                """INSERT INTO reading_progress
                   (user_id, book_id, guild_id, status, started_at, finished_at,
                    last_activity_at, source)
                   VALUES (?, ?, ?, 'finished', ?, ?, NULL, ?)""",
                (user_id, book_id, guild_id, row.read_at, row.read_at, GOODREADS_SOURCE),
            )
            result.added += 1
            if row.score is not None:
                cur.execute(
                    "SELECT 1 FROM ratings WHERE user_id = ? AND book_id = ? LIMIT 1",
                    (user_id, book_id),
                )
                if not cur.fetchone():
                    cur.execute(
                        """INSERT INTO ratings (user_id, book_id, score, rated_at, source)
                           VALUES (?, ?, ?, ?, ?)""",
                        (user_id, book_id, row.score, row.read_at, GOODREADS_SOURCE),
                    )
                    result.ratings_added += 1
        conn.commit()
        log_event(
            discord_id,
            guild_id,
            "goodreads_import",
            {
                "added": result.added,
                "already_on_shelf": result.already_on_shelf,
                "ratings_added": result.ratings_added,
            },
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


EXPORT_HEADERS = (
    "Title",
    "Author",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Number of Pages",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Exclusive Shelf",
)
EXPORT_FILENAME = "book-club-shelf.csv"

_STATUS_SHELF = {
    "finished": "read",
    "reading": "currently-reading",
    "wishlist": "to-read",
    "dnf": "read",
}


@dataclass
class ExportRow:
    title: str
    author: str
    pages: str
    stars: str
    date_read: str
    date_added: str
    bookshelves: str
    exclusive_shelf: str
    book_id: int
    progress_id: int
    source: str | None
    status: str


@dataclass
class ExportResult:
    csv_text: str
    row_count: int = 0
    finished: int = 0
    reading: int = 0
    to_read: int = 0
    dnf: int = 0
    queue_included: int = 0
    truncated: bool = False


def _format_gr_date(value) -> str:
    text = (str(value).strip() if value else "")
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.date().strftime("%Y/%m/%d")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    return ""


def _stars_from_score(score) -> str:
    if score is None or score == "":
        return "0"
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "0"
    if value <= 0:
        return "0"
    return str(min(5, max(1, (value + 1) // 2)))


def _keep_progress_row(current: ExportRow | None, incoming: ExportRow) -> ExportRow:
    if current is None:
        return incoming
    current_live = current.source != GOODREADS_SOURCE
    incoming_live = incoming.source != GOODREADS_SOURCE
    if incoming_live and not current_live:
        return incoming
    if current_live and not incoming_live:
        return current
    return incoming if incoming.progress_id >= current.progress_id else current


def _escape_formula(text: str) -> str:
    """Keep spreadsheet apps from evaluating member-supplied text as a formula."""
    if text and text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _row_cells(row: ExportRow) -> list[str]:
    return [
        _escape_formula(row.title),
        _escape_formula(row.author),
        "",
        "",
        row.stars,
        row.pages,
        row.date_read,
        row.date_added,
        row.bookshelves,
        row.exclusive_shelf,
    ]


def _write_export_csv(rows: list[ExportRow]) -> str:
    buf = StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(EXPORT_HEADERS)
    for row in rows:
        writer.writerow(_row_cells(row))
    return buf.getvalue()


def _sort_export_rows(rows: list[ExportRow]) -> list[ExportRow]:
    rank = {"read": 0, "currently-reading": 1, "to-read": 2}

    def key(row: ExportRow):
        date_num = int(row.date_read.replace("/", "") or 0)
        return (
            rank.get(row.exclusive_shelf, 9),
            -date_num,
            row.title.lower(),
        )

    return sorted(rows, key=key)


def build_shelf_export(discord_id: str, guild_id: str) -> ExportResult:
    """Goodreads-shaped CSV of this member's shelf in this guild."""
    result = ExportResult(csv_text="")
    if not discord_id or not guild_id:
        return result
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        user_row = cur.fetchone()
        if not user_row:
            return result
        user_id = user_row[0]
        cur.execute(
            """SELECT rp.progress_id, rp.book_id, rp.status, rp.source,
                      rp.started_at, rp.finished_at, b.title, b.author, b.page_count,
                      r.score
               FROM reading_progress rp
               JOIN books b ON b.book_id = rp.book_id
               LEFT JOIN ratings r ON r.user_id = rp.user_id AND r.book_id = rp.book_id
               WHERE rp.user_id = ? AND rp.guild_id = ?
               ORDER BY rp.progress_id ASC""",
            (user_id, guild_id),
        )
        by_book: dict[int, ExportRow] = {}
        for raw in cur.fetchall():
            title = (raw[6] or "").strip()
            if not title:
                continue
            status = (raw[2] or "").strip().lower()
            shelf = _STATUS_SHELF.get(status)
            if not shelf:
                continue
            bookshelves = "dnf" if status == "dnf" else shelf
            date_read = _format_gr_date(raw[5]) if status in {"finished", "dnf"} else ""
            date_added = _format_gr_date(raw[4]) or date_read
            pages = str(raw[8]) if raw[8] else ""
            row = ExportRow(
                title=title,
                author=(raw[7] or "").strip(),
                pages=pages,
                stars=_stars_from_score(raw[9]),
                date_read=date_read,
                date_added=date_added,
                bookshelves=bookshelves,
                exclusive_shelf=shelf,
                book_id=raw[1],
                progress_id=raw[0],
                source=raw[3],
                status=status,
            )
            by_book[row.book_id] = _keep_progress_row(by_book.get(row.book_id), row)

        cur.execute(
            """SELECT b.book_id, b.title, b.author, b.page_count, rq.created_at
               FROM reading_queue rq
               JOIN books b ON b.book_id = rq.book_id
               WHERE rq.user_id = ? AND rq.guild_id = ?
               ORDER BY rq.created_at ASC, rq.queue_id ASC""",
            (user_id, guild_id),
        )
        next_progress_id = 0
        for raw in cur.fetchall():
            book_id = raw[0]
            if book_id in by_book:
                continue
            title = (raw[1] or "").strip()
            if not title:
                continue
            next_progress_id -= 1
            by_book[book_id] = ExportRow(
                title=title,
                author=(raw[2] or "").strip(),
                pages=str(raw[3]) if raw[3] else "",
                stars="0",
                date_read="",
                date_added=_format_gr_date(raw[4]),
                bookshelves="to-read",
                exclusive_shelf="to-read",
                book_id=book_id,
                progress_id=next_progress_id,
                source=None,
                status="queue",
            )

        rows = _sort_export_rows(list(by_book.values()))
        if len(rows) > MAX_READ_ROWS:
            rows = rows[:MAX_READ_ROWS]
            result.truncated = True
        result.row_count = len(rows)
        result.queue_included = sum(1 for row in rows if row.status == "queue")
        for row in rows:
            if row.status == "dnf":
                result.dnf += 1
            elif row.exclusive_shelf == "read":
                result.finished += 1
            elif row.exclusive_shelf == "currently-reading":
                result.reading += 1
            else:
                result.to_read += 1
        if rows:
            result.csv_text = _write_export_csv(rows)
        return result
    finally:
        conn.close()
