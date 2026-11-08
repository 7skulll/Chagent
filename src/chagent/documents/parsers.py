"""Turn binary documents into ordered text blocks the file tools can page through.

This is a reader, not an ingestion pipeline: no chunking for retrieval, no
embeddings, no persistence. A block is one contiguous piece of text plus the
position it came from (page number, sheet name), because the model needs to say
"on page 7" and to resume exactly where the previous call stopped.

Everything here fails loudly. A scanned PDF has no text layer, and returning an
empty string would make the model conclude the document is blank.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from io import BytesIO
import logging
import re
from threading import Lock

#: pypdf logs a warning for every damaged file it opens. The failure is already
#: reported to the model as a DocumentError, so the duplicate stderr noise would
#: only clutter the terminal the run is being watched in.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: Extensions handled by this module. ``read_file`` consults this set to decide
#: whether to decode UTF-8 or to parse.
BINARY_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx"})

#: Old binary Office formats share an extension prefix with the XML ones and
#: cannot be parsed without external converters, so they get their own message.
LEGACY_SUFFIXES = frozenset({".doc", ".xls", ".ppt"})

TEXT_ENCODINGS = ("utf-8-sig", "gb18030")

#: A single cell or line longer than this is almost certainly binary noise that
#: survived decoding; clipping keeps one bad row from filling the context.
MAX_BLOCK_CHARS = 40_000

#: Blocks above this size are split on line boundaries. A PDF page in layout
#: mode is often one 8 000-character run, and a model paging 200 blocks at a
#: time would never reach the rest of it.
SPLIT_BLOCK_CHARS = 3_000


class DocumentError(ValueError):
    """A document that cannot be read; the message is safe to show the model."""


class MissingDependencyError(DocumentError):
    """The optional parser for this format is not installed."""


@dataclass(frozen=True, slots=True)
class Block:
    """One contiguous run of text and where it came from."""

    text: str
    page: int | None = None
    section: str | None = None


def suffix_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def is_binary_document(name: str) -> bool:
    """True when ``name`` must be parsed rather than decoded as UTF-8.

    Legacy Office formats are included on purpose: decoding ``.doc`` byte soup
    as text produces mojibake, while the parser can say "convert it to .docx".
    """
    suffix = suffix_of(name)
    return suffix in BINARY_SUFFIXES or suffix in LEGACY_SUFFIXES


def parse(name: str, data: bytes) -> list[Block]:
    """Parse ``data`` according to the extension of ``name``."""
    suffix = suffix_of(name)
    if suffix in LEGACY_SUFFIXES:
        raise DocumentError(
            f"{suffix} is the legacy binary Office format and cannot be read. "
            f"Save it as {suffix}x and read that instead."
        )
    if suffix == ".pdf":
        return _parse_pdf(data)
    if suffix == ".docx":
        return _parse_docx(data)
    if suffix == ".xlsx":
        return _parse_xlsx(data)
    if suffix in {".txt", ".md", ".markdown"}:
        return [Block(text=part) for part in _paragraphs(_decode_text(data))]
    raise DocumentError(f"Unsupported document type: {suffix or '(no extension)'}")


def _decode_text(data: bytes) -> str:
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    raise DocumentError("The file is neither UTF-8 nor GB18030 text; it may be a binary format.")


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def _require_blocks(blocks: list[Block], message: str) -> list[Block]:
    if not any(block.text.strip() for block in blocks):
        raise DocumentError(message)
    return _fit(blocks)


def _fit(blocks: list[Block]) -> list[Block]:
    """Clip pathological blocks, then split oversized ones on line breaks."""
    clipped = []
    for block in blocks:
        text = block.text
        if len(text) > MAX_BLOCK_CHARS:
            dropped = len(text) - MAX_BLOCK_CHARS
            text = text[:MAX_BLOCK_CHARS] + f"\n[… {dropped} characters omitted from this block]"
        clipped.append(Block(text=text, page=block.page, section=block.section))

    result = []
    for block in clipped:
        if len(block.text) <= SPLIT_BLOCK_CHARS:
            result.append(block)
            continue
        piece: list[str] = []
        size = 0
        for line in block.text.split("\n"):
            if piece and size + len(line) + 1 > SPLIT_BLOCK_CHARS:
                result.append(Block(text="\n".join(piece), page=block.page, section=block.section))
                piece, size = [], 0
            piece.append(line)
            size += len(line) + 1
        if piece:
            result.append(Block(text="\n".join(piece), page=block.page, section=block.section))
    return result


# --------------------------------------------------------------------------- pdf


_PAGE_NUMBER = re.compile(r"^(?:第\s*\d+\s*页(?:\s*[/／]\s*共?\s*\d+\s*页)?|page\s*\d+(?:\s*(?:/|of)\s*\d+)?|\d{1,4})$", re.IGNORECASE)
_EDGE_LINES = 2


def _parse_pdf(data: bytes) -> list[Block]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise MissingDependencyError(
            "Reading PDF requires the optional 'documents' dependencies. "
            "Install with: pip install 'chagent[documents]'"
        ) from None

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            # An empty password opens the many PDFs that are "encrypted" only to
            # set permissions; a real password cannot be guessed, so say so.
            try:
                reader.decrypt("")
            except Exception:
                raise DocumentError("The PDF is password protected and cannot be read.") from None
        pages = [_page_text(page) for page in reader.pages]
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(f"The PDF could not be parsed ({type(error).__name__}); it may be damaged.") from None

    if not any(page.strip() for page in pages):
        raise DocumentError(
            "The PDF has no text layer, so it is probably a scan. OCR is not supported; "
            "convert it to text first."
        )

    lines = [[_clean_line(line) for line in page.splitlines()] for page in pages]
    lines = _strip_running_lines(lines)

    blocks = []
    for number, page_lines in enumerate(lines, start=1):
        # Blank lines are the only paragraph signal a PDF gives us, so they are
        # kept through the header/footer pass and become block boundaries here.
        buffer: list[str] = []
        for line in page_lines:
            if not line:
                if buffer:
                    blocks.append(Block(text="\n".join(buffer), page=number))
                    buffer = []
                continue
            buffer.append(line)
        if buffer:
            blocks.append(Block(text="\n".join(buffer), page=number))
    return _require_blocks(blocks, "The PDF has no extractable text.")


def _page_text(page) -> str:
    """Prefer layout mode, which keeps table rows aligned; fall back per page."""
    try:
        return page.extract_text(extraction_mode="layout") or ""
    except Exception:
        # Blank pages raise in layout mode (missing /Contents); the plain mode
        # still works for them, and a truly broken page fails below.
        try:
            return page.extract_text() or ""
        except Exception:
            return ""


def _clean_line(line: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", line).strip()


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Drop page numbers and headers/footers repeated across most pages.

    A header is only recognised when the same line appears at the top or bottom
    of at least 60% of the pages (and the document has at least three), so a
    repeated first sentence in a short document survives. Emptying a page is
    never allowed: the original lines are kept in that case.

    Blank lines outside the trimmed edge are preserved, because they are what
    separated paragraphs before the page was cleaned.
    """
    if len(pages) < 3:
        return pages

    top: Counter[str] = Counter()
    bottom: Counter[str] = Counter()
    for page in pages:
        nonempty = [line for line in page if line]
        for line in nonempty[:_EDGE_LINES]:
            top[line] += 1
        for line in nonempty[-_EDGE_LINES:]:
            bottom[line] += 1

    threshold = max(2, int(len(pages) * 0.6))
    drop_top = {line for line, count in top.items() if count >= threshold}
    drop_bottom = {line for line, count in bottom.items() if count >= threshold}

    result = []
    for page in pages:
        start, end = 0, len(page)
        while start < end and (not page[start] or page[start] in drop_top
                               or _PAGE_NUMBER.match(page[start])):
            start += 1
        while end > start and (not page[end - 1] or page[end - 1] in drop_bottom
                               or _PAGE_NUMBER.match(page[end - 1])):
            end -= 1
        trimmed = page[start:end]
        result.append(trimmed if any(trimmed) else page)
    return result


# --------------------------------------------------------------------------- docx


def _parse_docx(data: bytes) -> list[Block]:
    try:
        from docx import Document
        from docx.table import Table
    except ImportError:
        raise MissingDependencyError(
            "Reading .docx requires the optional 'documents' dependencies. "
            "Install with: pip install 'chagent[documents]'"
        ) from None

    try:
        document = Document(BytesIO(data))
    except Exception as error:
        raise DocumentError(f"The Word file could not be parsed ({type(error).__name__}).") from None

    blocks: list[Block] = []
    heading = ""
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            text = _table_text([[cell.text.strip() for cell in row.cells] for row in item.rows])
            if text:
                blocks.append(Block(text=text, section=heading or None))
            continue
        text = item.text.strip()
        if not text:
            continue
        level = _docx_heading_level(item)
        if level:
            # Headings are kept as context, not as decoration: a table under
            # "3.2 报销标准" is meaningless without the heading attached.
            heading = text
            blocks.append(Block(text=text, section=text))
            continue
        blocks.append(Block(text=text, section=heading or None))
    return _require_blocks(blocks, "The Word file has no extractable content.")


def _docx_heading_level(paragraph) -> int:
    style = paragraph.style.name if paragraph.style else ""
    match = re.search(r"(\d+)\s*$", style)
    if not match:
        return 0
    lowered = style.lower()
    if lowered.startswith("heading") or "标题" in style or "標題" in style:
        return int(match.group(1))
    return 0


# --------------------------------------------------------------------------- xlsx


def _parse_xlsx(data: bytes) -> list[Block]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise MissingDependencyError(
            "Reading .xlsx requires the optional 'documents' dependencies. "
            "Install with: pip install 'chagent[documents]'"
        ) from None

    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as error:
        raise DocumentError(f"The Excel file could not be parsed ({type(error).__name__}).") from None

    blocks: list[Block] = []
    for sheet in workbook.worksheets:
        rows = [
            ["" if value is None else str(value).strip() for value in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue
        text = _table_text(rows)
        if text:
            # The sheet name travels in ``section``; repeating it in the body
            # would print it twice on every page that starts with this block.
            blocks.append(Block(text=text, section=sheet.title))
    if workbook is not None:
        workbook.close()
    return _require_blocks(blocks, "The Excel workbook has no non-empty cells.")


def _table_text(cells: list[list[str]]) -> str:
    """Render each row as ``header: value`` pairs.

    Repeating the header on every row is what keeps a table readable after it
    has been split across pages: a bare ``168`` means nothing without knowing it
    is the 住宿标准 column.
    """
    if not cells:
        return ""
    header = cells[0]
    lines = []
    for row in cells[1:]:
        pairs = [
            f"{header[i] if i < len(header) and header[i] else f'column {i + 1}'}: {value}"
            for i, value in enumerate(row)
            if value
        ]
        if pairs:
            lines.append("; ".join(pairs))
    if not lines and any(header):
        lines = [" | ".join(value for value in header if value)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- cache


class ParseCache:
    """Parse each distinct document revision once, then re-serve its blocks.

    Paging through a 400-page PDF means many ``read_file`` calls against the
    same bytes; re-parsing would burn seconds per page. The key is the content
    digest, so an overwritten file is re-parsed instead of serving stale text.
    """

    def __init__(self, limit: int = 8):
        self.limit = limit
        self._items: OrderedDict[str, tuple[Block, ...]] = OrderedDict()
        self._lock = Lock()

    def get(self, digest: str) -> tuple[Block, ...] | None:
        with self._lock:
            blocks = self._items.get(digest)
            if blocks is not None:
                self._items.move_to_end(digest)
            return blocks

    def put(self, digest: str, blocks: list[Block]) -> tuple[Block, ...]:
        frozen = tuple(blocks)
        with self._lock:
            self._items[digest] = frozen
            self._items.move_to_end(digest)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        return frozen

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def parse_with_cache(cache: ParseCache, digest: str, name: str, data: bytes) -> tuple[Block, ...]:
    blocks = cache.get(digest)
    if blocks is None:
        blocks = cache.put(digest, parse(name, data))
    return blocks
