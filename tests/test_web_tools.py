"""Outbound tools: one search provider, one text-only reader, both bounded."""

import json
from pathlib import Path
import unittest

import httpx

from chagent.config import Settings
from chagent.diagnostics.behavior import describe
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.web import (
    MAX_BODY_BYTES, MAX_FETCH_CHARS, MAX_SEARCH_CHARS, WebAccess, WebFetchTool, WebSearchTool,
    create_web_tools, html_to_text,
)


def search_payload(*, results=2, content="excerpt text"):
    return {
        "answer": "A short summary.",
        "results": [{"title": f"Title {index}", "url": f"https://example.com/{index}",
                     "content": content, "score": 0.9 - index / 100}
                    for index in range(results)],
    }


def transport(handler, calls=None):
    """A transport that records every request it hands back a response for."""
    def wrapped(request):
        if calls is not None:
            calls.append(request)
        return handler(request)
    return httpx.MockTransport(wrapped)


def answer(payload, status=200):
    return httpx.Response(status, content=json.dumps(payload).encode())


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, handler, *, params=None, key="tvly-secret", calls=None):
        tool = WebSearchTool(key, transport=transport(handler, calls))
        return await tool.execute("c1", params if params is not None else {"query": "python 3.14 release"})

    async def test_results_carry_source_urls_and_excerpts(self):
        result = await self.search(lambda request: answer(search_payload()))
        text = result.content[0].text
        self.assertIn("python 3.14 release", text)
        self.assertIn("https://example.com/0", text)
        self.assertIn("excerpt text", text)
        self.assertIn("A short summary.", text)
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.details["results"]), 2)
        # The transcript draws a source card from these, so each row carries the
        # excerpt it shows and the URL it opens — not just a score.
        first = result.details["results"][0]
        self.assertEqual(first["url"], "https://example.com/0")
        self.assertIn("excerpt text", first["snippet"])

    async def test_the_key_is_sent_as_a_bearer_header_and_never_echoed(self):
        calls = []
        result = await self.search(lambda request: answer(search_payload()), calls=calls)
        self.assertEqual(calls[0].headers["authorization"], "Bearer tvly-secret")
        self.assertNotIn("tvly-secret", result.content[0].text)
        self.assertNotIn("tvly-secret", json.dumps(result.details))

    async def test_recency_switches_to_the_news_topic_with_a_day_window(self):
        calls = []
        await self.search(lambda request: answer(search_payload()), calls=calls,
                          params={"query": "jdk", "recency": "week"})
        body = json.loads(calls[0].content)
        self.assertEqual(body["topic"], "news")
        self.assertEqual(body["days"], 7)

    async def test_blank_query_is_refused_before_any_request(self):
        calls = []
        with self.assertRaises(ValueError):
            await self.search(lambda request: answer(search_payload()),
                              params={"query": "   "}, calls=calls)
        self.assertEqual(calls, [])

    async def test_rejected_key_becomes_an_actionable_message(self):
        with self.assertRaises(PermissionError) as caught:
            await self.search(lambda request: httpx.Response(401))
        self.assertIn("CHAGENT_TAVILY_API_KEY", str(caught.exception))

    async def test_rate_limit_and_outage_are_distinguished(self):
        for status, expected in ((429, "rate limit"), (503, "unavailable")):
            with self.subTest(status=status), self.assertRaises(RuntimeError) as caught:
                await self.search(lambda request: httpx.Response(status))
            self.assertIn(expected, str(caught.exception))

    async def test_an_empty_result_set_is_reported_not_failed(self):
        result = await self.search(lambda request: answer({"results": []}))
        self.assertFalse(result.is_error)
        self.assertIn("No results", result.content[0].text)
        self.assertEqual(result.details["results"], [])

    async def test_output_is_capped_and_the_cut_is_flagged(self):
        # The provider can return more than it was asked for; the per-call cap is
        # what keeps a surprise out of the context window.
        result = await self.search(
            lambda request: answer(search_payload(results=20, content="x" * 600)))
        text = result.content[0].text
        self.assertLessEqual(len(text), MAX_SEARCH_CHARS + 80)
        self.assertTrue(result.details["truncated"])
        self.assertIn("Truncated at", text)

    async def test_a_repeat_query_inside_one_run_costs_one_request(self):
        calls = []
        tool = WebSearchTool("tvly-secret",
                             transport=transport(lambda request: answer(search_payload()), calls))
        first = await tool.execute("c1", {"query": "same"})
        second = await tool.execute("c2", {"query": "same"})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("cached", first.details)
        self.assertTrue(second.details["cached"])


class WebFetchTests(unittest.IsolatedAsyncioTestCase):
    PAGE = (b"<html><head><title>t</title><style>body{color:red}</style></head>"
            b"<body><h1>Heading</h1><p>First &amp; second</p><script>alert(1)</script>"
            b"<p>Third</p></body></html>")

    async def fetch(self, handler, *, params=None, calls=None):
        tool = WebFetchTool(transport=transport(handler, calls))
        return await tool.execute("c1", params if params is not None else {"url": "https://example.com/a"})

    async def test_markup_is_stripped_and_scripts_are_dropped(self):
        result = await self.fetch(lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=self.PAGE))
        text = result.content[0].text
        self.assertIn("Heading", text)
        self.assertIn("First & second", text)
        self.assertIn("Third", text)
        self.assertNotIn("alert(1)", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("<p>", text)

    async def test_private_and_loopback_targets_are_refused_without_a_request(self):
        calls = []
        for url in ("http://127.0.0.1:8080/admin", "http://localhost/", "http://10.0.0.1/",
                    "http://[::1]/", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(PermissionError):
                await self.fetch(lambda request: httpx.Response(200, content=b"x"),
                                 params={"url": url}, calls=calls)
        self.assertEqual(calls, [])

    async def test_scheme_and_credentials_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "https://user:pw@example.com/"):
            with self.subTest(url=url), self.assertRaises((ValueError, PermissionError)):
                await self.fetch(lambda request: httpx.Response(200, content=b"x"), params={"url": url})

    async def test_a_redirect_is_reported_instead_of_followed(self):
        result = await self.fetch(lambda request: httpx.Response(
            302, headers={"location": "https://example.com/b"}))
        self.assertIn("Redirected to https://example.com/b", result.content[0].text)
        self.assertEqual(result.details["status"], 302)

    async def test_non_text_content_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            await self.fetch(lambda request: httpx.Response(
                200, headers={"content-type": "image/png"}, content=b"\x89PNG"))
        self.assertIn("not text", str(caught.exception))

    async def test_http_errors_surface_the_status(self):
        with self.assertRaises(RuntimeError) as caught:
            await self.fetch(lambda request: httpx.Response(404))
        self.assertIn("404", str(caught.exception))

    async def test_long_pages_are_cut_at_max_chars(self):
        result = await self.fetch(lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"a" * 30_000),
            params={"url": "https://example.com/big", "max_chars": 2000})
        self.assertTrue(result.details["truncated"])
        self.assertEqual(result.details["chars"], 2000)

    async def test_oversized_bodies_are_capped_while_streaming(self):
        result = await self.fetch(lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"a" * (MAX_BODY_BYTES + 5_000)),
            params={"url": "https://example.com/huge", "max_chars": MAX_FETCH_CHARS})
        self.assertTrue(result.details["body_capped"])

    async def test_plain_text_is_returned_without_extraction(self):
        result = await self.fetch(lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"line one\nline two"))
        self.assertIn("line one", result.content[0].text)


class HtmlTests(unittest.TestCase):
    def test_entities_are_decoded_and_blank_lines_collapsed(self):
        text = html_to_text("<div>a &lt; b</div><div></div><div></div><div>c</div>")
        self.assertEqual(text, "a < b\n\nc")


class WebAccessTests(unittest.TestCase):
    def test_no_key_registers_nothing(self):
        self.assertEqual(create_web_tools(WebAccess()), [])
        names = [item.name for item in create_builtin_registry(mode="work", web=WebAccess()).definitions()]
        self.assertNotIn("web_search", names)

    def test_a_key_registers_both_tools_alongside_host_commands(self):
        registry = create_builtin_registry(mode="code", allow_host_commands=True, web=WebAccess("tvly-k"))
        names = [item.name for item in registry.definitions()]
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)
        self.assertEqual(len(names), len(set(names)))

    def test_the_key_is_not_in_the_settings_repr(self):
        settings = Settings(web_access="tavily", tavily_api_key="tvly-hidden")
        self.assertNotIn("tvly-hidden", repr(settings))

    def test_enabling_tavily_without_a_key_fails_loudly(self):
        with self.assertRaises(ValueError) as caught:
            Settings(web_access="tavily")
        self.assertIn("CHAGENT_TAVILY_API_KEY", str(caught.exception))

    def test_unknown_backends_are_rejected(self):
        with self.assertRaises(ValueError):
            Settings(web_access="bing")

    def test_environment_round_trip_and_the_behavior_line(self):
        settings = Settings.from_env({"CHAGENT_WEB_ACCESS": "tavily",
                                      "CHAGENT_TAVILY_API_KEY": " tvly-from-env "})
        self.assertEqual(settings.tavily_api_key, "tvly-from-env")
        self.assertIn("web=tavily", describe(settings))
        self.assertIn("web=off", describe(Settings()))


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_tools_need_neither_a_workspace_nor_host_commands(self):
        from chagent.domain.messages import ToolCall
        from chagent.tools.executor import ToolExecutor

        registry = create_builtin_registry(mode="work", web=WebAccess("tvly-k"))
        tool = registry.get("web_fetch")
        self.assertFalse(tool.requires_workspace)
        self.assertFalse(tool.requires_host_execution)
        # Loopback is refused by the tool itself, which proves the call got past
        # the executor's host-command gate without that gate being open.
        result = await ToolExecutor(registry).execute(ToolCall("c1", "web_fetch", {"url": "http://127.0.0.1/"}))
        self.assertTrue(result.is_error)
        self.assertIn("PermissionError", result.content[0].text)


class IsolationTests(unittest.TestCase):
    def test_the_module_does_not_depend_on_the_workspace_layer(self):
        # Independence from WorkspaceFS is what lets these tools work in a run
        # with no workspace at all.
        source = Path("src/chagent/tools/web.py").read_text(encoding="utf-8")
        self.assertNotIn("WorkspaceFS", source)
