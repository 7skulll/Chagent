"""TB2.1 admission, adapter ownership and synthetic PostgreSQL chain."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from harbor.agents.factory import AgentFactory
from harbor.models.agent.context import AgentContext
from harbor.trial.hooks import TrialEvent
from psycopg import AsyncConnection, sql

from chagent.models.fake import FakeModelClient
from chagent.tools.context import ToolContext
from eval.benchmarks.terminal_bench import tb2
from eval.benchmarks.terminal_bench.harbor_agent import HarborExecution, TerminalCommand
from eval.cli import _eval_database
from eval.manifests import file_sha256, prompt_material, sha256_json


class Backend:
    def __init__(self, trial_id, failure=False, session_id="trial__env"):
        self.context_id = trial_id
        self.session_id = session_id
        self.calls = []
        self.failure = failure

    async def exec(self, command, *, timeout_sec):
        self.calls.append((command, timeout_sec))
        if self.failure:
            raise RuntimeError("simulated Harbor timeout")
        return SimpleNamespace(stdout="/app\n", stderr="", return_code=0)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_binding_and_unknown_command_cannot_dispatch(self):
        trial_id, session, run = uuid4(), uuid4(), uuid4()
        with TemporaryDirectory() as directory:
            path = Path(directory)
            backend = Backend(trial_id, failure=True)
            execution = HarborExecution(backend, trial_id, session, path)
            with self.assertRaises(PermissionError):
                await execution.bind(uuid4(), run, path)
            await execution.bind(session, run, path)
            tool = TerminalCommand(execution)
            foreign = ToolContext(path, session, uuid4(), task_id=str(trial_id))
            with self.assertRaises(PermissionError):
                await tool.execute("bad", {"cmd": "pwd"}, context=foreign)
            self.assertEqual(backend.calls, [])
            current = ToolContext(path, session, run, task_id=str(trial_id))
            outcome = await tool.execute("valid", {"cmd": "pwd"}, context=current)
            self.assertTrue(outcome.outcome_unknown)
            with self.assertRaises(PermissionError):
                await tool.execute("late", {"cmd": "pwd"}, context=current)
            self.assertEqual(backend.calls, [("pwd", 15)])
            await execution.release(run)
            self.assertEqual(execution.evidence()["cleanup"], "harbor_unverified")


def fixture(root):
    task = root / "music-harmony"
    task.mkdir()
    (task / "instruction.md").write_text(
        "Inspect the official task working directory.\n"
    )
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text("FROM scratch\n")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "task.toml").write_text(
        '[task]\nname="terminal-bench/music-harmony"\nversion="1.0.0"\n'
        '[environment]\ndocker_image="example/main@sha256:' + "a" * 64 + '"\n'
        '[verifier]\nenvironment_mode="separate"\n'
        '[verifier.environment]\ndocker_image="example/verifier@sha256:'
        + "b" * 64
        + '"\n'
    )
    files = {
        p.relative_to(task).as_posix(): file_sha256(p)
        for p in task.rglob("*")
        if p.is_file()
    }
    return task, sha256_json(files)


class SlotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name).resolve()
        task, digest = fixture(parent)
        pin = patch.dict(tb2.PACKAGE_BYTES, {"music-harmony": digest})
        pin.start()
        self.addCleanup(pin.stop)
        self.root = parent / "slot"
        tb2.prepare(task, self.root)

    async def test_drift_fails_before_permanent_claim(self):
        (self.root / "music-harmony" / "instruction.md").write_text("Changed")
        with self.assertRaisesRegex(ValueError, "Package bytes differ"):
            await tb2.execute(self.root, None)
        self.assertFalse((self.root / "claim.json").exists())

    async def test_failed_slot_cannot_be_reclaimed(self):
        async def failed(config):
            raise RuntimeError("Simulated job creation failure")

        with self.assertRaises(RuntimeError):
            await tb2.execute(self.root, None, create_job=failed)
        with self.assertRaisesRegex(ValueError, "claim.json already exists"):
            await tb2.execute(self.root, None, create_job=failed)
        self.assertEqual(tb2.report(self.root)["status"], "incomplete")

    @unittest.skipUnless(
        os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Requires test PostgreSQL"
    )
    async def test_runner_persists_bound_terminal_and_rejects_local_tool(self):
        url = os.environ["CHAGENT_TEST_DATABASE_URL"]
        schema = "chagent_test_tb2_" + uuid4().hex
        admin = await AsyncConnection.connect(url, autocommit=True)
        self.addAsyncCleanup(admin.close)
        self.addAsyncCleanup(
            admin.execute,
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)),
        )

        class JobHarness:
            def __init__(self, config):
                self.config = config
                self.hooks = {}
                self.trial_id = uuid4()
                self.backend = Backend(self.trial_id)

            def __len__(self):
                return 1

            def add_hook(self, event, callback):
                self.hooks[event] = callback

            async def run(self):
                job_dir = self.config.jobs_dir / self.config.job_name
                event = SimpleNamespace(
                    trial_id=self.trial_id,
                    trial_name="trial",
                    config=SimpleNamespace(
                        trials_dir=job_dir, task=self.config.tasks[0]
                    ),
                )
                trial_dir = job_dir / "trial"
                logs = trial_dir / "agent"
                logs.mkdir(parents=True)
                await self.hooks[TrialEvent.START](event)
                agent = AgentFactory.create_agent_from_config(
                    self.config.agents[0], logs_dir=logs
                )
                agent.context_id = self.trial_id
                with self_outer.assertRaises(PermissionError):
                    await agent.setup(Backend(uuid4()))
                with self_outer.assertRaises(PermissionError):
                    await agent.setup(
                        Backend(self.trial_id, session_id="trial__verifier__trial")
                    )
                await agent.setup(self.backend)
                context = AgentContext()
                instruction = tb2.Task(self.config.tasks[0].path).instruction
                await agent.run(instruction, self.backend, context)
                with self_outer.assertRaises(PermissionError):
                    await agent.run(instruction, self.backend, context)
                (trial_dir / "result.json").write_text(
                    json.dumps(
                        {
                            "id": str(event.trial_id),
                            "verifier_result": {"rewards": {"reward": 0.0}},
                            "exception_info": None,
                        }
                    )
                )
                await self.hooks[TrialEvent.END](event)

        self_outer = self
        for tool, params in (
            ("exec_command", {"cmd": "cat /workspace/task.txt", "timeout_sec": 3}),
            ("read_file", {"path": "/workspace/task.txt"}),
        ):
            with self.subTest(tool=tool):
                root = self.root
                if tool == "read_file":
                    root = self.root.parent / "other"
                    tb2.prepare(self.root / "music-harmony", root)
                jobs = []

                async def create(config):
                    jobs.append(JobHarness(config))
                    return jobs[0]

                async with _eval_database(url, schema) as database:
                    result = await tb2.execute(
                        root,
                        database,
                        create_job=create,
                        client=FakeModelClient(tool, params),
                    )
                    self.assertEqual(tb2.report(root)["status"], "observed")
                    data = json.loads(result.read_text())
                    self.assertEqual(data["harbor_trial_id"], str(jobs[0].trial_id))
                    self.assertEqual(data["candidate"]["verified_preambles"], 2)
                    self.assertEqual(
                        jobs[0].backend.calls,
                        [(params["cmd"], 3)] if tool == "exec_command" else [],
                    )
                    async with database.connection() as connection:
                        rows = await (
                            await connection.execute(
                                "SELECT request FROM model_calls WHERE run_id = %s ORDER BY iteration",
                                (data["binding"]["run_id"],),
                            )
                        ).fetchall()
                        self.assertEqual(len(rows), 2)
                        for row in rows:
                            _, declarations = prompt_material(row["request"])
                            self.assertEqual(
                                [d["name"] for d in declarations], ["exec_command"]
                            )
