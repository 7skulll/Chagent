"""Outbound tools against DeepSeek's Search and Fetch API (WD 批).

WHAT CHANGED
    W 批 shipped Tavily for search and a self-written HTML reader for fetch.
    WD 批 replaced both with one provider, so the properties this file guards
    changed shape: there is no local resolver check left to test (the request
    leaves from the provider's network), no redirect-following decision (one
    POST is the whole call), and no local 2 MB stream cap. What replaced them is
    a status-code contract — 401/402/403/404/429/5xx each get their own message
    — and a rendering budget measured against the real API, because the
    provider's ``summary`` came back 4–5× longer than its documentation claims.

WHAT IT PINS
    1. The request: endpoint, bearer header, and only the fields this version
       sends (``need_content`` is deliberately absent).
    2. The budget: per-result summary cut, total cut, and no ``summary`` or
       ``content`` in ``details`` (details are stored twice).
    3. The error mapping, because "retry" and "top up your balance" are
       different instructions to a model.
    4. The card fields the transcript draws (WK3): snippet, site name, date,
       authority, score — including the provider's hex-encoded ``site_name``.
"""

import json
import unittest
from datetime import date

import httpx

from chagent.config import Settings
from chagent.diagnostics.behavior import describe
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.web import (
    DEFAULT_COUNT, FETCH_ENDPOINT, MAX_RESPONSE_BYTES, MAX_SEARCH_CHARS, MAX_SUMMARY_CHARS,
    SEARCH_ENDPOINT, WebAccess, WebAccessError, WebFetchTool, WebSearchTool, create_web_tools,
)


def transport(handler, calls=None):
    """Mock transport that records the requests it was asked to make."""
    def wrapped(request):
        if calls is not None:
            calls.append(request)
        return handler(request)
    return httpx.MockTransport(wrapped)


def answer(payload, status=200):
    return httpx.Response(status, content=json.dumps(payload).encode())


def search_payload(count=2, summary_chars=120):
    return {"results": [
        {"rerank_score": 2.4, "title": f"Result {index}",
         "url": f"https://example.com/{index}",
         "snippet": f"snippet {index}",
         "summary": "summary text " * (summary_chars // 13 + 1),
         "content": "", "site_name": f"Site {index}", "site_icon": None,
         "date": "2026-03-01T16:00:00+00:00", "authority": 3}
        for index in range(count)
    ]}


def fetch_payload(chars=200):
    return {"title": "Example article", "content": "# Example article\n\n" + "body " * (chars // 5)}


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, handler, *, params=None, key="ds-secret", calls=None, today=None):
        tool = WebSearchTool(key, transport=transport(handler, calls),
                             today=today or (lambda: date(2026, 10, 30)))
        return await tool.execute("c1", params if params is not None else {"query": "python 3.14 release"})

    async def test_request_goes_to_the_documented_endpoint_with_only_the_fields_we_send(self):
        calls = []
        await self.search(lambda request: answer(search_payload()), calls=calls)
        request = calls[0]
        self.assertEqual(str(request.url), SEARCH_ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["authorization"], "Bearer ds-secret")
        body = json.loads(request.content)
        self.assertEqual(body, {"query": "python 3.14 release", "count": DEFAULT_COUNT})
        # need_content would return whole pages for every result; this batch
        # deliberately leaves it off until measurement justifies the cost.
        self.assertNotIn("need_content", body)

    async def test_results_carry_urls_excerpts_and_card_fields(self):
        result = await self.search(lambda request: answer(search_payload()))
        text = result.content[0].text
        self.assertIn("python 3.14 release", text)
        self.assertIn("https://example.com/0", text)
        self.assertIn("2026-03-01", text)
        self.assertFalse(result.is_error)
        rows = result.details["results"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["url"], "https://example.com/0")
        self.assertEqual(rows[0]["site_name"], "Site 0")
        self.assertEqual(rows[0]["date"], "2026-03-01")
        self.assertEqual(rows[0]["authority"], 3)
        self.assertEqual(rows[0]["score"], 2.4)
        self.assertTrue(rows[0]["snippet"].startswith("snippet 0"))

    async def test_details_never_carry_the_long_text(self):
        """summary and content are for the model once; details are stored twice."""
        result = await self.search(lambda request: answer(search_payload(summary_chars=3000)))
        rows = result.details["results"]
        self.assertNotIn("summary", rows[0])
        self.assertNotIn("content", rows[0])
        self.assertLessEqual(len(result.details["results"][0]["snippet"]), 240)
        self.assertLess(len(json.dumps(result.details)), 2000)

    async def test_the_rendered_excerpt_is_cut_per_result_and_in_total(self):
        big = await self.search(lambda request: answer(search_payload(count=8, summary_chars=4000)))
        self.assertLessEqual(len(big.content[0].text), MAX_SEARCH_CHARS + 200)
        self.assertTrue(big.details["truncated"] or len(big.content[0].text) <= MAX_SEARCH_CHARS)
        # A single result may not eat the whole budget either.
        one = await self.search(lambda request: answer(search_payload(count=1, summary_chars=4000)))
        body = one.content[0].text
        self.assertLessEqual(len(body), MAX_SUMMARY_CHARS + 400)

    async def test_recency_becomes_query_syntax_from_the_host_clock(self):
        calls = []
        await self.search(lambda request: answer(search_payload()), calls=calls,
                          params={"query": "release notes", "recency": "week"})
        body = json.loads(calls[0].content)
        # 2026-10-30 minus 7 days. The model cannot know today's date, so the
        # relative window is resolved here instead of being asked for in a
        # provider-specific field.
        self.assertEqual(body["query"], "release notes after:2026-10-23")

    async def test_search_operators_are_passed_through_untouched(self):
        calls = []
        raw = 'deepseek site:github.com OR site:huggingface.co lang:zh after:2025 "exact phrase" -ads'
        await self.search(lambda request: answer(search_payload()), calls=calls, params={"query": raw})
        self.assertEqual(json.loads(calls[0].content)["query"], raw)

    async def test_empty_results_are_reported_not_failed(self):
        result = await self.search(lambda request: answer({"results": []}))
        self.assertFalse(result.is_error)
        self.assertIn("No results", result.content[0].text)
        self.assertEqual(result.details["results"], [])

    async def test_each_status_gets_its_own_actionable_message(self):
        cases = {
            401: (PermissionError, "CHAGENT_SEARCH_API_KEY"),
            402: (PermissionError, "balance"),
            403: (PermissionError, "private"),
            404: (WebAccessError, "no usable page"),
            429: (WebAccessError, "retry after a pause"),
            500: (WebAccessError, "retry after a pause"),
            504: (WebAccessError, "retry after a pause"),
            400: (WebAccessError, "HTTP 400"),
        }
        for status, (kind, needle) in cases.items():
            with self.subTest(status=status):
                with self.assertRaises(kind) as caught:
                    await self.search(lambda request, code=status: answer({}, code))
                self.assertIn(needle, str(caught.exception))

    async def test_a_body_that_is_not_json_is_a_protocol_error(self):
        with self.assertRaises(WebAccessError):
            await self.search(lambda request: httpx.Response(200, content=b"<html>nope</html>"))
        with self.assertRaises(WebAccessError):
            await self.search(lambda request: answer({"unexpected": True}))

    async def test_an_oversized_response_is_refused_before_parsing(self):
        with self.assertRaises(WebAccessError):
            await self.search(lambda request: httpx.Response(
                200, content=b'{"results":[]}' + b" " * MAX_RESPONSE_BYTES))

    async def test_the_key_is_never_echoed(self):
        result = await self.search(lambda request: answer(search_payload()), key="ds-hidden")
        self.assertNotIn("ds-hidden", result.content[0].text)
        self.assertNotIn("ds-hidden", json.dumps(result.details))
        self.assertNotIn("ds-hidden", repr(result))
        self.assertNotIn("ds-hidden", repr(WebAccess("ds-hidden")))

    async def test_a_repeat_query_inside_one_run_costs_one_request(self):
        calls = []
        def handler(request):
            return answer(search_payload())
        tool = WebSearchTool("ds-secret", transport=transport(handler, calls))
        first = await tool.execute("c1", {"query": "current release"})
        second = await tool.execute("c2", {"query": "current release"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(second.details.get("cached"))
        self.assertEqual(first.content[0].text, second.content[0].text)
        # A different query is a different request.
        await tool.execute("c3", {"query": "another release"})
        self.assertEqual(len(calls), 2)

    async def test_a_hex_encoded_site_name_is_decoded(self):
        """Measured: the provider sometimes returns the label hex-encoded."""
        payload = search_payload(count=1)
        payload["results"][0]["site_name"] = "507974686f6e20646f63756d656e746174696f6e"
        result = await self.search(lambda request: answer(payload))
        self.assertEqual(result.details["results"][0]["site_name"], "Python documentation")

    async def test_blank_query_is_refused_before_any_request(self):
        calls = []
        with self.assertRaises(ValueError):
            await self.search(lambda request: answer(search_payload()), calls=calls, params={"query": "   "})
        self.assertEqual(calls, [])


class WebFetchTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler, *, params=None, calls=None):
        tool = WebFetchTool("ds-secret", transport=transport(handler, calls))
        return await tool.execute("c1", params if params is not None else {"url": "https://example.com/a"})

    async def test_markdown_comes_back_with_its_source(self):
        calls = []
        result = await self.fetch(lambda request: answer(fetch_payload()), calls=calls)
        self.assertEqual(str(calls[0].url), FETCH_ENDPOINT)
        self.assertEqual(json.loads(calls[0].content), {"url": "https://example.com/a"})
        text = result.content[0].text
        self.assertIn("https://example.com/a", text)
        self.assertIn("Example article", text)
        self.assertIn("# Example article", text)
        self.assertEqual(result.details["format"], "markdown")
        self.assertFalse(result.details["truncated"])

    async def test_long_pages_are_cut_at_max_chars(self):
        result = await self.fetch(lambda request: answer(fetch_payload(chars=40_000)),
                                  params={"url": "https://example.com/a", "max_chars": 1000})
        self.assertTrue(result.details["truncated"])
        self.assertLessEqual(len(result.content[0].text), 1000 + 200)
        self.assertIn("cut at max_chars=1000", result.content[0].text)

    async def test_late_target_and_continuation_use_one_cached_full_page(self):
        content = "a" * 25000 + "TARGET-METHOD: verified body" + "z" * 2500
        calls = []
        tool = WebFetchTool("ds-secret", transport=transport(
            lambda request: answer({"title": "Long page", "content": content}), calls))
        first = await tool.execute("c1", {"url": "https://example.com/a#intro", "max_chars": 1000})
        self.assertNotIn("verified body", first.content[0].text)
        found = await tool.execute("c2", {"url": "https://example.com/a#target", "find": "target-method", "max_chars": 1000})
        self.assertIn("TARGET-METHOD: verified body", found.content[0].text)
        self.assertEqual(found.details["match_offset"], 25000)
        self.assertEqual(found.details["offset"], 24500)
        self.assertTrue(found.details["cached"])
        next_page = await tool.execute("c3", {"url": "https://example.com/a", "offset": found.details["next_offset"], "max_chars": 1000})
        self.assertEqual(next_page.details["offset"], 25500)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("content", found.details)

    async def test_missing_phrase_and_end_of_page_are_explicit(self):
        tool = WebFetchTool("ds-secret", transport=transport(lambda request: answer(fetch_payload())))
        missing = await tool.execute("c1", {"url": "https://example.com/a", "find": "absent"})
        self.assertIsNone(missing.details["match_offset"])
        self.assertIn("not found", missing.content[0].text)
        end = await tool.execute("c2", {"url": "https://example.com/a", "offset": 999999})
        self.assertIsNone(end.details["next_offset"])
        self.assertEqual(end.details["offset"], end.details["chars"])

    async def test_an_empty_extraction_is_a_failure_not_an_empty_answer(self):
        """A 200 with no text would be read as "the page says nothing"."""
        with self.assertRaises(WebAccessError) as caught:
            await self.fetch(lambda request: answer({"title": "", "content": "  "}))
        self.assertIn("no extractable text", str(caught.exception))

    async def test_a_blocked_url_reports_the_provider_policy(self):
        body = {"error": {"message": "URL access denied (request_id: x)", "type": "permission_error",
                          "param": None, "code": "invalid_request_error"}}
        with self.assertRaises(PermissionError) as caught:
            await self.fetch(lambda request: answer(body, 403))
        self.assertIn("private", str(caught.exception))

    async def test_a_page_that_cannot_be_read_reports_every_cause(self):
        with self.assertRaises(WebAccessError) as caught:
            await self.fetch(lambda request: answer({}, 404))
        message = str(caught.exception)
        for needle in ("unavailable", "refused", "content type", "extractable"):
            self.assertIn(needle, message)

    async def test_scheme_and_credentials_are_rejected_without_a_request(self):
        calls = []
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pw@example.com/x"):
            with self.subTest(url=url):
                with self.assertRaises((ValueError, PermissionError)):
                    await self.fetch(lambda request: answer(fetch_payload()), calls=calls,
                                     params={"url": url})
        self.assertEqual(calls, [])

    async def test_timeouts_are_reported_as_such(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow", request=request)
        with self.assertRaises(WebAccessError) as caught:
            await self.fetch(handler)
        self.assertIn("timed out", str(caught.exception))


class WebAccessTests(unittest.TestCase):
    def test_no_key_registers_nothing(self):
        self.assertEqual(create_web_tools(WebAccess()), [])
        names = [item.name for item in create_builtin_registry(mode="work", web=WebAccess()).definitions()]
        self.assertNotIn("web_search", names)

    def test_a_key_registers_both_tools_alongside_the_work_face(self):
        registry = create_builtin_registry(mode="work", web=WebAccess("ds-k"))
        names = [item.name for item in registry.definitions()]
        self.assertEqual(names, ["apply_patch", "create_directory", "edit_file", "glob", "grep", "list_dir",
                                 "read_file", "read_skill", "web_fetch", "web_search", "write_file"])
        declaration = registry.get("web_search").declaration()
        self.assertEqual(declaration.parameters["required"], ["query"])
        # need_content is not offered in this batch on purpose.
        self.assertNotIn("need_content", declaration.parameters["properties"])

    def test_web_access_needs_neither_a_workspace_nor_host_commands(self):
        tool = create_web_tools(WebAccess("ds-k"))[0]
        self.assertFalse(tool.requires_workspace)
        self.assertFalse(tool.requires_host_execution)

    def test_enabling_search_without_a_key_fails_loudly_at_startup(self):
        with self.assertRaises(ValueError) as caught:
            Settings(web_access="deepseek")
        self.assertIn("CHAGENT_SEARCH_API_KEY", str(caught.exception))

    def test_the_removed_provider_is_refused_with_a_reason(self):
        """An old .env must not silently talk to a different provider."""
        with self.assertRaises(ValueError) as caught:
            Settings(web_access="tavily", search_api_key="whatever")
        self.assertIn("deepseek", str(caught.exception))
        with self.assertRaises(ValueError):
            Settings(web_access="bing")

    def test_the_search_key_falls_back_to_the_provider_key(self):
        settings = Settings.from_env({"CHAGENT_WEB_ACCESS": "deepseek", "DEEPSEEK_API_KEY": "sk-provider"})
        self.assertEqual(settings.search_api_key, "sk-provider")
        # Diagnostics must be able to say "fallback" without re-deriving it.
        self.assertEqual(settings.search_api_key_source, "DEEPSEEK_API_KEY")
        explicit = Settings.from_env({"CHAGENT_WEB_ACCESS": "deepseek", "DEEPSEEK_API_KEY": "sk-provider",
                                      "CHAGENT_SEARCH_API_KEY": " sk-search "})
        self.assertEqual(explicit.search_api_key, "sk-search")
        self.assertEqual(explicit.search_api_key_source, "CHAGENT_SEARCH_API_KEY")
        self.assertIn("web=deepseek", describe(explicit))
        self.assertEqual(Settings.from_env({}).search_api_key_source, "")
