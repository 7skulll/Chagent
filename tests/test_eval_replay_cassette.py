"""P3 model cassette: fingerprint semantics, round trip and stopping on drift.

The fingerprint is the whole safety property. If it hashes a timestamp, every
replay diverges for nothing
if it ignores a tool declaration, a changed tool
face replays against the old answer and looks green. Both directions are tested
here, plus the promise that a mismatch stops instead of walking to the next
recorded response.
"""

import json
from pathlib import Path
import tempfile
import unittest

from eval.replay.cassette import (
    CASSETTE_VERSION, Cassette, CassetteDivergence, CassetteExhausted, CassetteInvalid, Entry,
    ReplayClient, compare_fingerprints, fingerprint, record, request_from_row, verify,
)
from chagent.models.events import DoneEvent, ErrorEvent, StartEvent


def system_message(preamble: str = "You are Chagent.", tool: str = "read_file") -> dict:
    return {
        "role": "system", "content": "", "timestamp": 0,
        "sections": {"preamble": preamble},
        "tools_added": [{"name": tool, "description": "read", "parameters": {"type": "object"}}],
    }


def user_message(text: str = "hello", timestamp: int = 1) -> dict:
    return {"role": "user", "content": text, "timestamp": timestamp}


def response_doc(text: str = "hi", *, stop_reason: str = "stop") -> dict:
    return {
        "role": "assistant", "content": [{"type": "text", "text": text,
                                          "text_signature": None}],
        "api": "openai-chat", "provider": "test", "model": "test-model",
        "timestamp": 1793858296905, "stop_reason": stop_reason,
        "usage": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
                  "total_tokens": 2, "cost": None, "cache_write_1h": None,
                  "reasoning": None},
    }


def request_doc(*, messages=None, preamble: str = "You are Chagent.",
                tool: str = "read_file", model: str = "test-model",
                temperature: float | None = None) -> dict:
    return {
        "model": {"id": model, "provider": "test", "api": "openai-chat",
                  "context_window": 1000, "max_tokens": 100},
        "messages": messages or [system_message(preamble, tool), user_message()],
        "max_tokens": 64,
        "temperature": temperature,
    }


def rows(*pairs, status: str = "completed") -> list[dict]:
    return [{"iteration": index, "status": status, "request": request, "response": response}
            for index, (request, response) in enumerate(pairs)]


class FingerprintTests(unittest.TestCase):
    def test_same_request_has_the_same_fingerprint(self):
        self.assertEqual(fingerprint(request_doc()), fingerprint(request_doc()))

    def test_non_semantic_fields_are_ignored(self):
        first = request_doc(messages=[system_message(), user_message(timestamp=1)])
        second = request_doc(messages=[system_message(), user_message(timestamp=999)])
        second["run_id"] = "not-a-uuid"
        second["workspace"] = "/tmp/somewhere"
        self.assertEqual(fingerprint(first)["sha256"], fingerprint(second)["sha256"])

    def test_a_changed_system_section_changes_the_fingerprint(self):
        changed = request_doc(preamble="You are someone else.")
        self.assertNotEqual(fingerprint(request_doc())["sha256"], fingerprint(changed)["sha256"])

    def test_a_changed_tool_declaration_changes_the_fingerprint(self):
        changed = request_doc(tool="write_file")
        self.assertNotEqual(fingerprint(request_doc())["sha256"], fingerprint(changed)["sha256"])

    def test_an_added_message_changes_the_fingerprint(self):
        longer = request_doc(messages=[system_message(), user_message(), user_message("more")])
        self.assertNotEqual(fingerprint(request_doc())["sha256"], fingerprint(longer)["sha256"])

    def test_a_changed_model_or_temperature_changes_the_fingerprint(self):
        self.assertNotEqual(fingerprint(request_doc())["sha256"],
                            fingerprint(request_doc(model="other"))["sha256"])
        self.assertNotEqual(fingerprint(request_doc())["sha256"],
                            fingerprint(request_doc(temperature=0.5))["sha256"])

    def test_usage_and_response_ids_inside_history_are_ignored(self):
        assistant = response_doc()
        first = request_doc(messages=[system_message(), user_message(), assistant])
        second = dict(assistant, usage=None, response_id="another", duration_ms=99)
        other = request_doc(messages=[system_message(), user_message(), second])
        self.assertEqual(fingerprint(first)["sha256"], fingerprint(other)["sha256"])

    def test_drift_names_the_component(self):
        recorded = fingerprint(request_doc())
        current = fingerprint(request_doc(preamble="Changed."))
        drifts = compare_fingerprints(recorded, current)
        self.assertEqual(drifts[0]["component"], "prompt_sha256")
        self.assertEqual([item["component"] for item in drifts][:2],
                         ["prompt_sha256", "message[0]"])

    def test_a_shorter_message_list_is_a_count_drift(self):
        recorded = fingerprint(request_doc())
        current = fingerprint(request_doc(messages=[system_message()]))
        self.assertIn("messages", [item["component"] for item in
                                   compare_fingerprints(recorded, current)])


class RecordTests(unittest.TestCase):
    def test_records_entries_in_iteration_order(self):
        cassette = record(rows((request_doc(), response_doc("first")),
                               (request_doc(), response_doc("second"))))
        self.assertEqual(len(cassette.entries), 2)
        self.assertEqual(cassette.entries[0].response["content"][0]["text"], "first")
        self.assertEqual(cassette.entries[1].response["content"][0]["text"], "second")

    def test_calls_without_a_response_are_skipped_and_reported(self):
        cassette = record(rows((request_doc(), None), (request_doc(), response_doc())))
        self.assertEqual(len(cassette.entries), 1)
        self.assertTrue(any("no stored response" in note for note in cassette.warnings))

    def test_status_decides_the_kind(self):
        failed = record([{"iteration": 0, "status": "failed", "request": request_doc(),
                          "response": response_doc(stop_reason="error")}])
        cancelled = record([{"iteration": 0, "status": "cancelled", "request": request_doc(),
                             "response": response_doc(stop_reason="aborted")}])
        self.assertEqual(failed.entries[0].kind, "stream_error")
        self.assertEqual(cancelled.entries[0].kind, "cancelled")

    def test_no_responses_at_all_is_invalid(self):
        with self.assertRaises(CassetteInvalid):
            record([{"iteration": 0, "status": "failed", "request": request_doc(),
                     "response": None}])

    def test_text_is_redacted_before_storage(self):
        # The projector compares against the resolved root, so the path in the
        # text has to be the resolved one — ``/tmp`` is a link on macOS.
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root).resolve()
            response = response_doc(f"wrote {workspace}/a.txt")
            cassette = record(rows((request_doc(), response)), workspace=workspace)
        text = cassette.entries[0].response["content"][0]["text"]
        self.assertIn("/workspace/a.txt", text)
        self.assertNotIn(str(workspace), text)

    def test_save_and_load_round_trip(self):
        cassette = record(rows((request_doc(), response_doc())))
        with tempfile.TemporaryDirectory() as root:
            path = cassette.save(Path(root) / "cassette.json")
            loaded = Cassette.load(path)
        self.assertEqual(loaded.to_json(), cassette.to_json())

    def test_load_rejects_an_unknown_version(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "c.json"
            path.write_text(json.dumps({"cassette_version": 99, "entries": []}), encoding="utf-8")
            with self.assertRaises(CassetteInvalid):
                Cassette.load(path)

    def test_load_rejects_an_entry_without_a_fingerprint(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "c.json"
            path.write_text(json.dumps({"cassette_version": CASSETTE_VERSION, "entries": [
                {"kind": "response", "response": response_doc()}]}), encoding="utf-8")
            with self.assertRaises(CassetteInvalid):
                Cassette.load(path)

    def test_entry_kind_is_validated(self):
        with self.assertRaises(CassetteInvalid):
            Entry("magic", {"sha256": "x"}, response_doc())


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    def cassette(self, count: int = 2) -> Cassette:
        return record(rows(*((request_doc(), response_doc(f"answer {index}"))
                             for index in range(count))))

    def requests(self, count: int = 2):
        return [request_from_row({"request": request_doc()}) for _ in range(count)]

    async def test_replay_reproduces_every_recorded_answer(self):
        cassette = self.cassette(3)
        document = await verify(cassette, self.requests(3))
        self.assertTrue(document["ok"], document)
        self.assertEqual(document["matched"], 3)
        self.assertIsNone(document["stopped"])
        self.assertEqual([item["message"]["content"][0]["text"] for item in document["terminal"]],
                         ["answer 0", "answer 1", "answer 2"])

    async def test_replay_emits_start_and_terminator(self):
        cassette = self.cassette(1)
        client = ReplayClient(cassette)
        events = [event async for event in client.stream(self.requests(1)[0])]
        self.assertIsInstance(events[0], StartEvent)
        self.assertIsInstance(events[-1], DoneEvent)
        self.assertEqual(events[-1].reason, "stop")
        self.assertEqual(events[-1].message.content[0].text, "answer 0")

    async def test_a_stream_error_replays_as_an_error_event(self):
        cassette = record([{"iteration": 0, "status": "failed", "request": request_doc(),
                            "response": response_doc("boom", stop_reason="error")}])
        client = ReplayClient(cassette)
        events = [event async for event in client.stream(self.requests(1)[0])]
        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(events[-1].reason, "error")

    async def test_a_cancelled_call_replays_as_aborted(self):
        cassette = record([{"iteration": 0, "status": "cancelled", "request": request_doc(),
                            "response": response_doc("stopped", stop_reason="aborted")}])
        client = ReplayClient(cassette)
        events = [event async for event in client.stream(self.requests(1)[0])]
        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertEqual(events[-1].reason, "aborted")

    async def test_divergence_at_the_first_request_stops_without_consuming(self):
        cassette = self.cassette(2)
        drifted = request_from_row({"request": request_doc(preamble="Changed.")})
        document = await verify(cassette, [drifted, self.requests(1)[0]])
        self.assertFalse(document["ok"])
        self.assertEqual(document["matched"], 0)
        self.assertEqual(document["stopped"]["index"], 0)
        self.assertEqual(document["terminal"], [])
        self.assertEqual(document["stopped"]["drifts"][0]["component"], "prompt_sha256")

    async def test_divergence_later_keeps_the_prefix_and_stops(self):
        cassette = self.cassette(3)
        requests = self.requests(3)
        requests[1] = request_from_row({"request": request_doc(tool="write_file")})
        document = await verify(cassette, requests)
        self.assertEqual(document["matched"], 1)
        self.assertEqual(document["stopped"]["index"], 1)
        self.assertEqual(len(document["terminal"]), 1)

    async def test_more_requests_than_entries_is_reported(self):
        cassette = self.cassette(1)
        document = await verify(cassette, self.requests(2))
        self.assertFalse(document["ok"])
        self.assertEqual(document["matched"], 1)
        self.assertEqual(document["stopped"]["drifts"][0]["component"], "calls")

    async def test_the_client_raises_when_exhausted(self):
        client = ReplayClient(self.cassette(1))
        request = self.requests(1)[0]
        [event async for event in client.stream(request)]
        with self.assertRaises(CassetteExhausted):
            [event async for event in client.stream(request)]

    async def test_the_client_raises_divergence_with_drift_details(self):
        client = ReplayClient(self.cassette(1))
        drifted = request_from_row({"request": request_doc(tool="write_file")})
        with self.assertRaises(CassetteDivergence) as caught:
            [event async for event in client.stream(drifted)]
        self.assertEqual(caught.exception.index, 0)
        self.assertTrue(caught.exception.drifts)

    def test_request_from_row_rebuilds_the_model(self):
        request = request_from_row({"request": request_doc()})
        self.assertEqual(request.model.id, "test-model")
        self.assertEqual(request.model.provider, "test")
        self.assertEqual(request.purpose, "task")
        self.assertEqual(len(request.messages), 2)


class IntegrityRegressionTests(unittest.IsolatedAsyncioTestCase):
    def cassette(self):
        return record(rows((request_doc(), response_doc())))

    def test_endpoint_participates_without_exporting_url_or_credentials(self):
        a, b = request_doc(), request_doc()
        a['model']['endpoint'] = 'https://alice:secret@provider-a.example/v1'
        b['model']['endpoint'] = 'https://provider-b.example/v1'
        first, second = fingerprint(a), fingerprint(b)
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(compare_fingerprints(first, second)[0]['component'], 'model')
        blob = json.dumps(first)
        self.assertNotIn('secret', blob)
        self.assertNotIn('provider-a.example', blob)

    async def test_changed_endpoint_stops_actual_replay(self):
        a, b = request_doc(), request_doc()
        a['model']['endpoint'] = 'https://a.example/v1'
        b['model']['endpoint'] = 'https://b.example/v1'
        result = await verify(record(rows((a, response_doc()))), [request_from_row({'request': b})])
        self.assertFalse(result['ok'])
        self.assertEqual(result['matched'], 0)
        self.assertEqual(result['stopped']['drifts'][0]['component'], 'model')

    def test_shortened_message_hashes_are_refused_on_load(self):
        document = self.cassette().to_json()
        document['entries'][0]['fingerprint']['message_sha256'] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.json'
            path.write_text(json.dumps(document))
            with self.assertRaises(CassetteInvalid):
                Cassette.load(path)

    async def test_in_memory_mutation_is_refused_before_yielding(self):
        cassette = self.cassette()
        cassette.entries[0].fingerprint['message_sha256'] = []
        changed = request_doc()
        changed['messages'][1]['content'] = 'different user task'
        with self.assertRaises(CassetteInvalid):
            await verify(cassette, [request_from_row({'request': changed})])

    def test_mutated_fingerprint_cannot_be_saved(self):
        cassette = self.cassette()
        cassette.entries[0].fingerprint['messages'] = 0
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(CassetteInvalid):
            cassette.save(Path(tmp) / 'bad.json')

    def test_missing_component_and_bad_digest_are_refused(self):
        import copy
        original = self.cassette().entries[0]
        for key in original.fingerprint:
            altered = copy.deepcopy(original.fingerprint)
            del altered[key]
            with self.subTest(key=key), self.assertRaises(CassetteInvalid):
                Entry('response', altered, response_doc())
        altered = copy.deepcopy(original.fingerprint)
        altered['message_sha256'][0] = 'not-a-sha256'
        with self.assertRaises(CassetteInvalid):
            Entry('response', altered, response_doc())

    def test_aggregate_hash_must_match_components(self):
        value = fingerprint(request_doc())
        value['temperature'] = .5
        with self.assertRaises(CassetteInvalid):
            compare_fingerprints(value, fingerprint(request_doc()))

    def test_recomputed_hash_cannot_hide_shortened_hash_list(self):
        from eval.manifests import sha256_json
        value = fingerprint(request_doc())
        value['message_sha256'] = []
        value['sha256'] = sha256_json({k: v for k, v in value.items() if k != 'sha256'})
        with self.assertRaises(CassetteInvalid):
            Entry('response', value, response_doc())

    def test_fingerprint_number_fields_reject_boolean_coercion(self):
        from eval.manifests import sha256_json
        value = fingerprint(request_doc())
        value['max_tokens'] = True
        value['sha256'] = sha256_json({k: v for k, v in value.items() if k != 'sha256'})
        with self.assertRaises(CassetteInvalid):
            Entry('response', value, response_doc())

    def test_old_cassette_version_requires_rerecording(self):
        document = self.cassette().to_json()
        document['cassette_version'] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.json'
            path.write_text(json.dumps(document))
            with self.assertRaises(CassetteInvalid):
                Cassette.load(path)

    def test_empty_cassette_is_not_a_passing_replay(self):
        with self.assertRaises(CassetteInvalid):
            Cassette(())


if __name__ == "__main__":
    unittest.main()
