"""P3 contract comparison: what a replay is allowed to call "the same".

The interesting cases are the refusals. Normalising an undeclared UUID would make
every difference arguable; allowing a rule on ``status`` or ``exit_code`` would
delete the fact the replay exists to check; comparing error prose would fail on a
cosmetic edit. Each of those is a test here, not a comment.
"""

import unittest

from eval.replay.contract import (
    ContractInvalid, ErrorContract, IRREDUCIBLE, Normalization, NormalizationRules,
    compare, compare_error, compare_state, snapshot_facts,
)


def rules(*declarations) -> NormalizationRules:
    return NormalizationRules(tuple(declarations))


class ComparisonTests(unittest.TestCase):
    def test_equal_documents_have_no_differences(self):
        result = compare({"a": 1, "b": {"c": "x"}}, {"a": 1, "b": {"c": "x"}})
        self.assertTrue(result.equal)
        self.assertEqual(result.compared, 2)
        self.assertEqual(result.differences, ())

    def test_changed_value_names_the_path(self):
        result = compare({"b": {"c": "x"}}, {"b": {"c": "y"}})
        self.assertFalse(result.equal)
        self.assertEqual(len(result.differences), 1)
        self.assertEqual(result.differences[0].path, "b.c")
        self.assertEqual(result.differences[0].kind, "changed")

    def test_missing_and_extra_are_distinct(self):
        result = compare({"a": 1, "b": 2}, {"a": 1, "c": 3})
        kinds = {item.path: item.kind for item in result.differences}
        self.assertEqual(kinds, {"b": "missing", "c": "extra"})

    def test_types_are_compared_not_coerced(self):
        result = compare({"n": 1, "b": True}, {"n": "1", "b": 1})
        kinds = {item.path: item.kind for item in result.differences}
        self.assertEqual(kinds, {"n": "type", "b": "type"})

    def test_ints_and_floats_compare_by_value(self):
        self.assertTrue(compare({"n": 1}, {"n": 1.0}).equal)

    def test_lists_flatten_by_index(self):
        result = compare({"files": [{"sha256": "a"}]}, {"files": [{"sha256": "b"}]})
        self.assertEqual(result.differences[0].path, "files[0].sha256")

    def test_truncates_long_values_in_output(self):
        result = compare({"text": "a" * 500}, {"text": "b" * 500})
        self.assertTrue(result.differences[0].expected.endswith("…"))
        self.assertLessEqual(len(result.differences[0].expected), 121)


class NormalizationTests(unittest.TestCase):
    def test_declared_uuid_is_normalised_and_reported(self):
        result = compare({"id": "11111111-1111-1111-1111-111111111111"},
                         {"id": "22222222-2222-2222-2222-222222222222"},
                         rules(Normalization("id", "uuid")))
        self.assertTrue(result.equal)
        self.assertEqual(result.normalized, ("id",))

    def test_undeclared_uuid_is_a_difference(self):
        result = compare({"id": "11111111-1111-1111-1111-111111111111"},
                         {"id": "22222222-2222-2222-2222-222222222222"})
        self.assertFalse(result.equal)

    def test_declared_uuid_that_is_not_a_uuid_stays_a_difference(self):
        result = compare({"id": "not-a-uuid"}, {"id": "also-not"}, rules(Normalization("id", "uuid")))
        self.assertFalse(result.equal)
        self.assertEqual(result.normalized, ())

    def test_timestamp_accepts_numbers_only(self):
        accepted = compare({"at": 1}, {"at": 2}, rules(Normalization("at", "timestamp")))
        refused = compare({"at": "yesterday"}, {"at": "today"},
                          rules(Normalization("at", "timestamp")))
        self.assertTrue(accepted.equal)
        self.assertFalse(refused.equal)

    def test_only_two_kinds_are_offered(self):
        from eval.replay.contract import NORMALIZERS

        self.assertEqual(NORMALIZERS, ("uuid", "timestamp"))

    def test_unknown_normalizer_is_rejected(self):
        with self.assertRaises(ContractInvalid):
            Normalization("id", "everything")

    def test_irreducible_fields_cannot_be_declared(self):
        for name in IRREDUCIBLE:
            with self.subTest(field=name), self.assertRaises(ContractInvalid):
                Normalization(name, "uuid")
                NormalizationRules((Normalization(name, "uuid"),))

    def test_nested_irreducible_suffix_is_protected(self):
        with self.assertRaises(ContractInvalid):
            NormalizationRules((Normalization("meta.error.code", "uuid"),))
        with self.assertRaises(ContractInvalid):
            NormalizationRules((Normalization("*.exit_code", "timestamp"),))

    def test_duplicate_declaration_is_rejected(self):
        with self.assertRaises(ContractInvalid):
            NormalizationRules((Normalization("id", "uuid"), Normalization("id", "timestamp")))

    def test_invalid_field_pattern_is_rejected(self):
        for pattern in ("", "a..b", "a b", "a[", "-a"):
            with self.subTest(pattern=pattern), self.assertRaises(ContractInvalid):
                Normalization(pattern, "uuid")

    def test_wildcard_covers_exactly_one_segment(self):
        declared = rules(Normalization("items[*].id", "uuid"))
        self.assertIsNotNone(declared.find("items[0].id"))
        self.assertIsNone(declared.find("items[0].nested.id"))
        self.assertIsNone(declared.find("other.id"))

    def test_most_specific_declaration_wins(self):
        declared = rules(Normalization("*.at", "timestamp"), Normalization("meta.at", "timestamp"))
        self.assertEqual(declared.find("meta.at").field, "meta.at")

    def test_status_declaration_is_refused_by_the_builder_not_at_compare_time(self):
        with self.assertRaises(ContractInvalid) as caught:
            NormalizationRules((Normalization("call.status", "uuid"),))
        self.assertIn("may not be normalised", str(caught.exception))


class ErrorContractTests(unittest.TestCase):
    def test_reads_structured_fields_only(self):
        contract = ErrorContract.from_tool_result({
            "is_error": True,
            "content": [{"type": "text", "text": "the tool said no"}],
            "details": {"failure": {"code": "denied"}, "exit_code": 3,
                        "outcome_unknown": "true"},
        })
        self.assertTrue(contract.failed)
        self.assertEqual(contract.code, "denied")
        self.assertEqual(contract.exit_code, 3)
        self.assertTrue(contract.outcome_unknown)
        self.assertEqual(contract.message, "the tool said no")

    def test_successful_result_has_no_failure_fields(self):
        contract = ErrorContract.from_tool_result({"content": [], "details": {"files": []}})
        self.assertFalse(contract.failed)
        self.assertIsNone(contract.code)
        self.assertIsNone(contract.exit_code)

    def test_message_is_not_compared_by_default(self):
        expected = ErrorContract(True, "denied", message="old wording")
        actual = ErrorContract(True, "denied", message="new wording")
        result = compare_error(expected, actual)
        self.assertTrue(result.equal)
        self.assertTrue(any("message text" in note for note in result.notes))

    def test_message_is_compared_when_asked(self):
        expected = ErrorContract(True, "denied", message="old wording")
        actual = ErrorContract(True, "denied", message="new wording")
        self.assertFalse(compare_error(expected, actual, exact_message=True).equal)

    def test_a_different_code_is_a_difference(self):
        result = compare_error(ErrorContract(True, "denied"), ErrorContract(True, "expired"))
        self.assertFalse(result.equal)
        self.assertEqual(result.differences[0].path, "code")

    def test_a_success_replayed_as_a_failure_is_a_difference(self):
        result = compare_error(ErrorContract(False), ErrorContract(True, "execution_failed"))
        self.assertFalse(result.equal)
        self.assertEqual({item.path for item in result.differences}, {"failed", "code"})


class StateContractTests(unittest.TestCase):
    def test_state_is_compared_under_a_files_key(self):
        expected = {"a.txt": snapshot_facts(b"one")}
        actual = {"a.txt": snapshot_facts(b"two")}
        result = compare_state(expected, actual)
        self.assertFalse(result.equal)
        self.assertEqual(result.differences[0].path, 'files["a.txt"].sha256')

    def test_identical_state_matches(self):
        facts = {"a.txt": snapshot_facts(b"one")}
        self.assertTrue(compare_state(facts, dict(facts)).equal)

    def test_directory_facts_carry_no_hash(self):
        self.assertEqual(snapshot_facts(None, kind="directory"), {"kind": "directory"})

    def test_unreadable_file_is_named_not_hashed(self):
        self.assertEqual(snapshot_facts(None), {"kind": "unreadable"})


class StructureRegressionTests(unittest.TestCase):
    def test_empty_nodes_are_present_and_typed(self):
        for want, have in (({}, {'x': {}}), ({'x': []}, {'x': {}}), ([], {}),
                           ({'x': []}, {}), ([[]], []), ({}, {'x': []})):
            with self.subTest(want=want, have=have):
                self.assertFalse(compare(want, have).equal)
                self.assertFalse(compare(have, want).equal)
        self.assertTrue(compare({'x': []}, {'x': []}).equal)

    def test_dotted_keys_do_not_collide_with_nested_paths(self):
        want = {'a.b': 1, 'a': {'b': 2}}
        have = {'a.b': 999, 'a': {'b': 2}}
        result = compare(want, have)
        self.assertFalse(result.equal)
        self.assertEqual(result.differences[0].path, '["a.b"]')

    def test_bracket_keys_do_not_collide_with_list_indices(self):
        result = compare({'x[0]': 1, 'x': [2]}, {'x[0]': 3, 'x': [2]})
        self.assertFalse(result.equal)
        self.assertEqual(result.differences[0].path, '["x[0]"]')

    def test_container_to_scalar_change_is_reported_at_parent(self):
        result = compare({'x': [1]}, {'x': 1})
        self.assertEqual([(item.path, item.kind) for item in result.differences], [('x', 'type')])

    def test_wildcard_cannot_normalize_protected_fields(self):
        for field in ('*', 'meta.*', '*.code', 'call.*'):
            with self.subTest(field=field), self.assertRaises(ContractInvalid):
                rules(Normalization(field, 'timestamp'))

    def test_concrete_protected_array_value_is_not_normalized(self):
        result = compare({'exit_code': [0]}, {'exit_code': [1]},
                         rules(Normalization('exit_code[*]', 'timestamp')))
        self.assertFalse(result.equal)

    def test_normalization_requires_both_values_to_have_declared_shape(self):
        result = compare({'id': '11111111-1111-1111-1111-111111111111'},
                         {'id': '<uuid>'}, rules(Normalization('id', 'uuid')))
        self.assertFalse(result.equal)

    def test_wildcard_matches_literal_dotted_key_as_one_segment(self):
        result = compare({'files': {'a.txt': {'at': 1}}},
                         {'files': {'a.txt': {'at': 2}}},
                         rules(Normalization('files.*.at', 'timestamp')))
        self.assertTrue(result.equal)
        self.assertEqual(result.normalized, ('files["a.txt"].at',))

    def test_list_pattern_does_not_match_a_literal_bracket_key(self):
        result = compare({'items[0]': {'at': 1}}, {'items[0]': {'at': 2}},
                         rules(Normalization('items[*].at', 'timestamp')))
        self.assertFalse(result.equal)


if __name__ == "__main__":
    unittest.main()
