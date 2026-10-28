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
