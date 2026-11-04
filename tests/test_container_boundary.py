"""Container execution requires both an explicit declaration and a container runtime."""

from dataclasses import replace
import asyncio
import unittest
from unittest.mock import patch

from chagent.config import Settings
from chagent.tools.sandbox import Sandbox
from chagent.tools.tests_runner import build_command, detect_suite
from tempfile import TemporaryDirectory
from pathlib import Path
from chagent.web.policy import validate_web_settings


class ContainerBoundaryTests(unittest.TestCase):
    @staticmethod
    def container_settings():
        return Settings(execution_profile="containerized", sandbox="off", container_declared=True,
                        allow_host_commands=True, sandbox_network=True)

    def test_configuration_rejects_missing_declaration(self):
        with self.assertRaisesRegex(ValueError, "container declaration"):
            Settings(execution_profile="containerized", sandbox="off", allow_host_commands=True,
                     sandbox_network=True)

    def test_web_entry_checks_container_marker(self):
        settings = self.container_settings()
        self.assertEqual(settings.boundary, "container")
        with patch("chagent.web.policy.Path.exists", return_value=False):
            with self.assertRaisesRegex(ValueError, "实际容器"):
                validate_web_settings(settings)

    def test_project_commands_do_not_inherit_model_keys(self):
        settings = self.container_settings()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret", "CHAGENT_DATABASE_URL": "postgresql:///secret"}):
            child = Sandbox.from_settings(settings).environment(None)
        self.assertNotIn("DEEPSEEK_API_KEY", child)
        self.assertNotIn("CHAGENT_DATABASE_URL", child)
        self.assertIn("PATH", child)

    def test_cli_flag_cannot_enable_host_commands(self):
        from chagent.cli import _run
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "实际容器"):
                asyncio.run(_run("probe", Path(directory), None, "exec_command", {"cmd": "true"},
                                 True, Settings(), None, None))

    def test_maven_project_uses_maven_runner(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "pom.xml").write_text("<project/>", encoding="utf-8")
            self.assertEqual(detect_suite(workspace), "maven")
            self.assertEqual(build_command("maven", workspace, None), ["mvn", "-B", "-ntp", "test"])
