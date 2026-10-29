import json
from pathlib import Path
import tempfile
import unittest

from chagent.config import Settings


class SettingsTests(unittest.TestCase):
    def test_environment_overrides(self):
        settings = Settings.from_env({
            "CHAGENT_DATABASE_URL": "postgresql:///custom_db",
            "CHAGENT_DB_POOL_MIN_SIZE": "0",
            "CHAGENT_DB_POOL_MAX_SIZE": "2",
            "CHAGENT_DB_TIMEOUT": "3.5",
            "CHAGENT_MAX_TURNS": "48",
            "CHAGENT_MAX_TOOL_CONCURRENCY": "3",
        })
        self.assertEqual(settings.database_url, "postgresql:///custom_db")
        self.assertEqual(settings.db_pool_min_size, 0)
        self.assertEqual(settings.db_pool_max_size, 2)
        self.assertEqual(settings.db_timeout, 3.5)
        self.assertEqual(settings.max_turns, 48)
        self.assertEqual(settings.max_tool_concurrency, 3)

    def test_credentials_are_not_in_repr_or_validation_errors(self):
        secret = "do-not-expose-this"
        settings = Settings(database_url=f"postgresql://user:{secret}@localhost/chagent")
        self.assertNotIn(secret, repr(settings))
        with self.assertRaises(ValueError) as error:
            Settings(database_url=f"invalid-{secret}")
        self.assertNotIn(secret, str(error.exception))

    def test_reject_invalid_configuration(self):
        for options in (
            {"database_url": ""},
            {"database_url": "postgresql://localhost"},
            {"db_pool_min_size": -1},
            {"db_pool_max_size": 0},
            {"db_pool_min_size": 3, "db_pool_max_size": 2},
            {"db_timeout": 0},
            {"db_timeout": float("nan")},
            {"db_timeout": float("inf")},
            {"max_turns": 0},
            {"max_turns": 129},
            {"max_tool_concurrency": 0},
            {"max_tool_concurrency": 33},
            {"max_tool_concurrency": True},
            {"max_tool_concurrency": 2.0},
            {"memory_worker_concurrency": 0},
            {"memory_worker_concurrency": 5},
            {"memory_worker_concurrency": True},
            # Workers must leave the run its own connection.
            {"db_pool_max_size": 2, "memory_worker_concurrency": 2},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Settings(**options)

    def test_memory_worker_concurrency_fits_inside_the_pool(self):
        settings = Settings.from_env({"CHAGENT_DB_POOL_MAX_SIZE": "4", "CHAGENT_MEMORY_WORKER_CONCURRENCY": "3"})
        self.assertEqual(settings.memory_worker_concurrency, 3)
        # A single-connection pool still allows one worker: they serialize anyway.
        self.assertEqual(Settings(db_pool_max_size=1, memory_worker_concurrency=1).memory_worker_concurrency, 1)

    def test_host_commands_require_explicit_boolean_setting(self):
        self.assertFalse(Settings.from_env({}).allow_host_commands)
        self.assertTrue(Settings.from_env({"CHAGENT_ALLOW_HOST_COMMANDS": "true"}).allow_host_commands)
        with self.assertRaises(ValueError):
            Settings.from_env({"CHAGENT_ALLOW_HOST_COMMANDS": "maybe"})


class ResolvedConfigurationTests(unittest.TestCase):
    """``config show`` must explain behaviour and name shadowed values."""

    def test_dotenv_parses_comments_quotes_and_export(self):
        from chagent.config import dotenv
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\n"
                "\n"
                "CHAGENT_MODEL=plain\n"
                "export CHAGENT_PROVIDER=\"deepseek\"\n"
                "DEEPSEEK_API_KEY='quoted-value'\n"
                "NOT_A_PAIR\n",
                encoding="utf-8")
            values = dotenv.read(path)
        self.assertEqual(values, {"CHAGENT_MODEL": "plain", "CHAGENT_PROVIDER": "deepseek",
                                 "DEEPSEEK_API_KEY": "quoted-value"})

    def test_shadowed_values_are_named_but_secrets_are_not_printed(self):
        from chagent.config import dotenv
        entries = dotenv.shadowed(
            {"CHAGENT_MODEL": "from-shell", "CHAGENT_API_KEY": "secret-value"},
            {"CHAGENT_MODEL": "from-file", "DEEPSEEK_API_KEY": "other-secret"})
        self.assertEqual([(item["key"], item["kind"]) for item in entries],
                         [("CHAGENT_MODEL", "overridden"), ("CHAGENT_API_KEY", "shell-only")])
        lines = "\n".join(dotenv.describe(entries))
        self.assertIn("from-file", lines)
        self.assertIn("from-shell", lines)
        self.assertNotIn("secret-value", lines)

    def test_behavior_line_names_sandbox_host_commands_and_compaction(self):
        from chagent.config import Settings
        from chagent.diagnostics.behavior import describe
        from chagent.domain.summaries import CompactionPolicy
        line = describe(Settings(sandbox="workspace-write", sandbox_network=False, allow_host_commands=True),
                        CompactionPolicy(enabled=False))
        self.assertIn("sandbox=workspace-write/no-network", line)
        self.assertIn("host_commands=on", line)
        self.assertIn("compaction=off", line)

    def test_collect_applies_the_file_as_defaults_and_reports_the_winner(self):
        from chagent.config import dotenv
        from chagent.config.__main__ import collect
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("CHAGENT_PROVIDER=deepseek\nDEEPSEEK_API_KEY=file-secret\n"
                            "CHAGENT_MODEL=file-model\nCHAGENT_SANDBOX=workspace-write\n",
                            encoding="utf-8")
            file_env = dotenv.read(path)
            # The environment wins, exactly as `uv run --env-file` behaves.
            env = {**file_env, "CHAGENT_MODEL": "shell-model"}
            sections, problems = collect(env, file_env, path)
        self.assertEqual(problems, [])
        self.assertIn("deepseek/shell-model", sections["model"])
        self.assertIn("sandbox=workspace-write", sections["behavior"])
        self.assertEqual(sections["shadowed"],
                         ["CHAGENT_MODEL from .env 'file-model' is overridden by environment 'shell-model'"])
        self.assertNotIn("file-secret", json.dumps(sections))
