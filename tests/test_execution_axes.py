"""The three execution axes, and the combinations each entry point accepts.

One word used to answer "where does this run", "may it execute" and "does a
human approve": the tests here pin the split. Each axis is derived from the
switches by a single function, so a start-up banner, ``/api/info`` and the tool
assembly cannot disagree with each other.
"""

from dataclasses import replace
import unittest
from unittest.mock import patch

from chagent.config import Settings
from chagent.execution import (
    CONTAINER, FULL, GATE_APPROVAL, GATE_NONE, NONE, PUBLISHED, READ_ONLY,
    ExecutionAxes, resolve,
)
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.web.policy import validate_web_settings


def container_settings(**overrides) -> Settings:
    values = {"execution_profile": "containerized", "container_declared": True,
              "allow_host_commands": True, "sandbox_network": True}
    values.update(overrides)
    return Settings(**values)


class AxesResolutionTests(unittest.TestCase):
    def test_default_is_the_degraded_read_only_shape(self):
        axes = Settings().axes
        self.assertEqual(axes.combination, (NONE, READ_ONLY, GATE_NONE))
        self.assertFalse(axes.executable)
        self.assertTrue(axes.published)

    def test_container_with_commands_is_the_only_executable_shape(self):
        axes = container_settings().axes
        self.assertEqual(axes.combination, (CONTAINER, FULL, GATE_NONE))
        self.assertTrue(axes.executable)
        self.assertTrue(axes.published)

    def test_container_without_commands_is_read_only(self):
        axes = container_settings(allow_host_commands=False).axes
        self.assertEqual(axes.combination, (CONTAINER, READ_ONLY, GATE_NONE))
        self.assertFalse(axes.executable)

    def test_the_build_switch_alone_does_not_create_a_capability(self):
        axes = container_settings(code_mode=False).axes
        self.assertEqual(axes.capability, READ_ONLY)

    def test_commands_without_a_container_boundary_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "container boundary"):
            Settings(allow_host_commands=True)

    def test_commands_require_declaration_writable_workspace_and_network(self):
        base = container_settings()
        for field, message in (("container_declared", "container declaration"),
                               ("sandbox_writable", "CHAGENT_SANDBOX_WRITABLE"),
                               ("sandbox_network", "CHAGENT_SANDBOX_NETWORK")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                replace(base, **{field: False})

    def test_boundary_declaration_must_match_the_profile(self):
        # A declaration that contradicts the profile is a mistake, not a
        # preference: the profile is what the deployment was built for.
        with self.assertRaisesRegex(ValueError, "conflicts"):
            container_settings(boundary_setting="none")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            Settings(execution_profile="configured", boundary_setting="container")

    def test_retired_profiles_and_sandbox_modes_are_rejected(self):
        for profile in ("trusted-development", "isolated", ""):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                Settings(execution_profile=profile)
        for mode in ("isolated", "workspace-write", "container"):
            with self.subTest(sandbox=mode), self.assertRaises(ValueError):
                Settings(sandbox=mode)

    def test_approval_gate_is_reserved_but_not_offered(self):
        axes = container_settings(approval="on-request").axes
        self.assertEqual(axes.gate, GATE_APPROVAL)
        # The broker exists; no entry point wires it up yet, so the shape is
        # not published to a model.
        self.assertFalse(axes.published)

    def test_approval_without_commands_is_not_a_combination(self):
        with self.assertRaisesRegex(ValueError, "unsupported execution combination"):
            resolve(execution_profile="containerized", boundary="container",
                    allow_host_commands=False, approval="on-request",
                    container_declared=True)

    def test_published_shapes_are_exactly_three(self):
        self.assertEqual(set(PUBLISHED), {
            (CONTAINER, FULL, GATE_NONE), (CONTAINER, READ_ONLY, GATE_NONE), (NONE, READ_ONLY, GATE_NONE),
        })
        for axes in (ExecutionAxes(NONE, FULL, GATE_NONE), ExecutionAxes(NONE, READ_ONLY, GATE_APPROVAL)):
            self.assertFalse(axes.executable)
            self.assertFalse(axes.published)

    def test_describe_names_all_three_axes(self):
        self.assertEqual(Settings().axes.describe(), "boundary=none capability=read-only gate=none")


class EntryPointTests(unittest.TestCase):
    def test_web_entry_accepts_only_container_full_without_approval(self):
        with patch("chagent.web.policy.Path.exists", return_value=True):
            validate_web_settings(container_settings())
            for overrides in ({"allow_host_commands": False}, {"approval": "on-request"}):
                with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "实际容器"):
                    validate_web_settings(container_settings(**overrides))
            with self.assertRaisesRegex(ValueError, "实际容器"):
                validate_web_settings(Settings())

    def test_sandbox_predicate_is_the_runtime_half_of_the_same_rule(self):
        self.assertTrue(SandboxSettings(containerized=True, mode="off", writable_workspace=True).allows_execution)
        self.assertFalse(SandboxSettings(containerized=True, mode="off", writable_workspace=False).allows_execution)
        self.assertFalse(SandboxSettings(containerized=False).allows_execution)

    def test_tool_assembly_refuses_a_container_without_writes(self):
        read_only = Sandbox(SandboxSettings(containerized=True, writable_workspace=False))
        self.addCleanup(read_only.close)
        with self.assertRaisesRegex(ValueError, "writable container boundary"):
            create_builtin_registry(mode="work", unified=True, containerized=True,
                                    allow_host_commands=True, sandbox=read_only)


if __name__ == "__main__":
    unittest.main()
