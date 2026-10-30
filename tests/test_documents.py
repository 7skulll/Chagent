"""Document parsing, read_file dispatch and upload storage.

The samples are generated in-process so the suite stays self-contained: a
checked-in binary fixture would be unreadable in review and would rot silently
when a parser library changes.
"""

import asyncio
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from chagent.documents import DocumentError, ParseCache, parse, parse_with_cache
from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor
from chagent.tools.filesystem import ReadFileTool
from chagent.web.app import LocalBoundary
from chagent.web.uploads import MAX_UPLOAD_BYTES, safe_name, save


def build_pdf(pages: list[list[str]]) -> bytes:
    """A minimal PDF with a real text layer, one content stream per page."""
    if not pages:
        raise ValueError("A PDF needs at least one page")
    font_object = 3 + 2 * len(pages)
    kids = " ".join(f"{3 + index} 0 R" for index in range(len(pages)))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
    ]
    for index in range(len(pages)):
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {3 + len(pages) + index} 0 R "
            f"/Resources << /Font << /F1 {font_object} 0 R >> >> >>"
        )
    for lines in pages:
        body = "BT /F1 12 Tf 72 720 Td 16 TL\n" + "".join(f"({line}) Tj T*\n" for line in lines) + "ET"
        objects.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buffer = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(buffer)
    buffer += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        buffer += f"{offset:010d} 00000 n \n".encode()
    buffer += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return buffer


def build_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    from docx import Document

    document = Document()
    for text in paragraphs:
        if text.startswith("#"):
            document.add_heading(text.lstrip("#").strip(), level=text.count("#"))
        else:
            document.add_paragraph(text)
    if table:
        created = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                created.cell(row_index, column_index).text = value
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def build_xlsx(rows: list[list], title: str = "Sheet1") -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class DocumentParsingTests(unittest.TestCase):
    def test_pdf_keeps_paragraphs_and_page_numbers(self):
        pdf = build_pdf([["Policy", "", "Hotel limit is 500."], ["Leave rules", "", "Annual leave is accrued."]])
        blocks = parse("policy.pdf", pdf)
        self.assertEqual([block.page for block in blocks], [1, 1, 2, 2])
        self.assertEqual(blocks[0].text, "Policy")
        self.assertEqual(blocks[1].text, "Hotel limit is 500.")
        self.assertEqual(blocks[3].text, "Annual leave is accrued.")

    def test_pdf_strips_repeating_header_and_page_numbers(self):
        pages = [["ACME Confidential", f"Body of page {number}", str(number)] for number in range(1, 5)]
        blocks = parse("report.pdf", build_pdf(pages))
        text = "\n".join(block.text for block in blocks)
        self.assertNotIn("ACME Confidential", text)
        self.assertNotIn("\n1\n", text)
        for number in range(1, 5):
            self.assertIn(f"Body of page {number}", text)

    def test_pdf_without_a_text_layer_fails_instead_of_returning_nothing(self):
        with self.assertRaises(DocumentError) as raised:
            parse("scan.pdf", build_pdf([[], []]))
        self.assertIn("text layer", str(raised.exception))

    def test_damaged_pdf_is_reported_as_a_document_error(self):
        with self.assertRaises(DocumentError):
            parse("broken.pdf", b"%PDF-1.4 this is not a real pdf")

    def test_docx_keeps_headings_as_section_and_flattens_tables(self):
        blocks = parse("rules.docx", build_docx(
            ["# Reimbursement", "Hotel limit is 500.", "## 3.2 Limits", "Receipts are required."],
            [["Item", "Limit"], ["Hotel", "500"]],
        ))
        self.assertEqual(blocks[0].section, "Reimbursement")
        self.assertEqual(blocks[2].section, "3.2 Limits")
        tables = [block.text for block in blocks if block.text.startswith("Item:")]
        self.assertEqual(tables, ["Item: Hotel; Limit: 500"])

    def test_xlsx_repeats_the_header_on_every_row(self):
        blocks = parse("standards.xlsx", build_xlsx([["City", "Hotel"], ["Beijing", 600], ["Chengdu", 400]], "Limits"))
        self.assertEqual(blocks[0].section, "Limits")
        self.assertIn("City: Beijing; Hotel: 600", blocks[0].text)
        self.assertIn("City: Chengdu; Hotel: 400", blocks[0].text)

    def test_empty_workbook_fails_loudly(self):
        with self.assertRaises(DocumentError):
            parse("empty.xlsx", build_xlsx([]))

    def test_legacy_office_formats_are_refused_with_a_conversion_hint(self):
        with self.assertRaises(DocumentError) as raised:
            parse("old.doc", b"\xd0\xcf\x11\xe0")
        self.assertIn(".docx", str(raised.exception))

    def test_unknown_extension_is_refused(self):
        with self.assertRaises(DocumentError):
            parse("image.png", b"\x89PNG\r\n")

    def test_oversized_block_is_split_on_line_boundaries(self):
        paragraph = "\n".join(f"line {number} " + "x" * 60 for number in range(200))
        blocks = parse("long.docx", build_docx([paragraph]))
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(block.text) <= 3_000 + 200 for block in blocks))
        joined = "\n".join(block.text for block in blocks)
        self.assertIn("line 0 ", joined)
        self.assertIn("line 199 ", joined)

    def test_cache_returns_the_same_parse_for_one_digest(self):
        cache = ParseCache(limit=2)
        data = build_xlsx([["A", "B"], ["1", "2"]])
        first = parse_with_cache(cache, "digest-1", "a.xlsx", data)
        second = parse_with_cache(cache, "digest-1", "a.xlsx", data)
        self.assertIs(first, second)
        third = parse_with_cache(cache, "digest-1", "a.xlsx", b"different bytes")
        self.assertIs(first, third, "the digest, not the bytes, decides the cache key")

    def test_cache_evicts_oldest_entry(self):
        cache = ParseCache(limit=2)
        for index in range(3):
            parse_with_cache(cache, f"d{index}", "a.txt", f"content {index}".encode())
        self.assertIsNone(cache.get("d0"))
        self.assertIsNotNone(cache.get("d2"))


@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class ReadDocumentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.context = ToolContext(self.workspace)
        self.executor = ToolExecutor(create_builtin_registry(mode="code"))

    async def read(self, **params):
        return await self.executor.execute(ToolCall("call", "read_file", params), context=self.context)

    async def test_pdf_is_read_as_numbered_blocks_and_pages_forward(self):
        (self.workspace / "policy.pdf").write_bytes(build_pdf([["First"], ["Second"], ["Third"]]))
        first = await self.read(path="policy.pdf", limit=2)
        self.assertFalse(first.is_error)
        self.assertIn("B1 [page 1]: First", first.content[0].text)
        self.assertIn("Next offset: 3", first.content[0].text)
        self.assertEqual(first.details["total_blocks"], 3)
        second = await self.read(path="policy.pdf", offset=3)
        self.assertIn("B3 [page 3]: Third", second.content[0].text)
        self.assertIn("Next offset: none", second.content[0].text)

    async def test_docx_table_arrives_with_its_header(self):
        (self.workspace / "rules.docx").write_bytes(build_docx(["Rules"], [["Item", "Limit"], ["Hotel", "500"]]))
        result = await self.read(path="rules.docx")
        self.assertIn("Item: Hotel; Limit: 500", result.content[0].text)

    async def test_text_files_still_use_line_numbers(self):
        (self.workspace / "notes.md").write_text("first\nsecond\n")
        result = await self.read(path="notes.md", offset=2, limit=1)
        self.assertIn("L2: second", result.content[0].text)
        self.assertEqual(result.details["total_lines"], 2)

    async def test_unreadable_document_reports_an_error_instead_of_empty_text(self):
        (self.workspace / "broken.pdf").write_bytes(b"definitely not a pdf")
        result = await self.read(path="broken.pdf")
        self.assertTrue(result.is_error)
        self.assertIn("DocumentError", result.content[0].text)

    async def test_legacy_format_is_reported_to_the_model(self):
        (self.workspace / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0")
        result = await self.read(path="legacy.doc")
        self.assertTrue(result.is_error)
        self.assertIn(".docx", result.content[0].text)


@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class UploadStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    async def store(self, name: str, data: bytes):
        async def chunks():
            for start in range(0, len(data), 7):
                yield data[start:start + 7]
        return await save(self.workspace, name, chunks())

    async def test_upload_lands_in_the_workspace_and_is_readable(self):
        pdf = build_pdf([["Uploaded body"]])
        saved = await self.store("出差制度.pdf", pdf)
        self.assertEqual(saved["path"], "uploads/出差制度.pdf")
        self.assertEqual(saved["size"], len(pdf))
        stored = self.workspace / saved["path"]
        self.assertEqual(stored.read_bytes(), pdf)
        result = await ReadFileTool().execute("call", {"path": saved["path"]},
                                              context=ToolContext(self.workspace))
        self.assertIn("Uploaded body", result.content[0].text)

    async def test_second_upload_with_the_same_name_is_renamed(self):
        first = await self.store("report.pdf", b"one")
        second = await self.store("report.pdf", b"two")
        self.assertEqual(first["name"], "report.pdf")
        self.assertEqual(second["name"], "report-1.pdf")
        self.assertEqual((self.workspace / second["path"]).read_bytes(), b"two")

    async def test_traversal_and_hidden_names_are_reduced_to_one_component(self):
        self.assertEqual(safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(safe_name(".."), "upload")
        self.assertEqual(safe_name("sub/dir/report.pdf"), "report.pdf")
        saved = await self.store("../../escape.txt", b"x")
        self.assertEqual(saved["path"], "uploads/escape.txt")
        self.assertFalse((self.workspace.parent / "escape.txt").exists())

    async def test_empty_body_is_refused(self):
        with self.assertRaises(ValueError):
            await self.store("empty.txt", b"")

    async def test_oversized_body_is_refused_before_it_is_written(self):
        with self.assertRaises(ValueError):
            await self.store("big.bin", b"x" * (MAX_UPLOAD_BYTES + 1))
        self.assertFalse((self.workspace / "uploads" / "big.bin").exists())


class UploadBoundaryTests(unittest.TestCase):
    """The upload route must bypass the 64 KiB JSON-only body limit."""

    def setUp(self):
        app = FastAPI()

        @app.post("/api/uploads")
        async def receive(request: Request):
            body = await request.body()
            return {"size": len(body)}

        @app.post("/api/notes")
        async def notes(request: Request):
            return {"size": len(await request.body())}

        app.add_middleware(LocalBoundary)
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def test_upload_accepts_any_content_type_and_a_large_body(self):
        payload = b"%PDF-1.4" + b"x" * 200_000
        response = self.client.post("/api/uploads", params={"name": "r.pdf"}, content=payload,
                                    headers={"content-type": "application/pdf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["size"], len(payload))

    def test_other_json_routes_keep_the_64_kib_limit(self):
        response = self.client.post("/api/notes", json={"text": "x" * 100_000})
        self.assertEqual(response.status_code, 413)

    def test_other_routes_still_reject_non_json_bodies(self):
        response = self.client.post("/api/notes", content=b"plain text",
                                    headers={"content-type": "text/plain"})
        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()
