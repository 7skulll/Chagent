"""One command-only Chagent run inside a Harbor-owned trial."""

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from harbor.agents.base import BaseAgent
from harbor.models.trial.paths import TrialPaths

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_bound_runner
from chagent.domain.context import ContextPolicy
from chagent.domain.messages import TextContent
from chagent.models.base import Model
from chagent.storage.repositories import ConversationRepository
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.registry import ToolRegistry
from eval.budgets import BudgetClient
from eval.benchmarks.terminal_bench.live_policy import LiveDiagnosticBudget
from eval.integrity import publish
from eval.manifests import sha256_json, sha256_text

PROFILE = "chagent-terminal-v1"
PROMPT = "Complete the official task using the terminal in the Harbor main environment."
ENVIRONMENT = (
    "Only exec_command is available. It runs in the official main environment "
    "with the task's default working directory and user. Treat paths as remote. "
    "Use the task's declared output paths."
)
MODEL = Model("tb2-scripted", "synthetic", "openai-chat", 16384, 512)
LIMITS = {
    "command_timeout_sec": 15,
    "wall_seconds": 120,
    "output_bytes_per_stream": 4096,
    "max_turns": 4,
    "max_tool_calls": 2,
}
ACTIVE_SLOT = ContextVar("tb2_active_slot", default=None)


class TerminalCommand(AgentTool):
    def __init__(self, execution=None):
        self.execution = execution
        super().__init__(
            name="exec_command",
            label="Harbor command",
            description="Run a one-shot command in the official Harbor main environment.",
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "timeout_sec": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": LIMITS["command_timeout_sec"],
                    },
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
            requires_run_context=True,
            mutates_workspace=True,
        )

    async def execute(
        self, tool_call_id, params, signal=None, on_update=None, *, context=None
    ):
        execution = self.execution
        execution.check(context)
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        seconds = params.get("timeout_sec", LIMITS["command_timeout_sec"])
        try:
            async with asyncio.timeout(seconds):
                result = await execution.backend.exec(
                    params["cmd"], timeout_sec=seconds
                )
        except asyncio.CancelledError:
            await execution.contain_interrupted_command("cancelled")
            raise
        except (OSError, RuntimeError):
            await execution.contain_interrupted_command("timeout_or_backend_error")
            return AgentToolResult(
                [TextContent("Harbor command outcome unknown.")],
                is_error=True,
                terminate=True,
                outcome_unknown=True,
            )
        stdout, out_truncated = _bounded(result.stdout)
        stderr, err_truncated = _bounded(result.stderr)
        details = {
            "exit_code": result.return_code,
            "stdout_truncated": out_truncated,
            "stderr_truncated": err_truncated,
        }
        suffix = "\n[Output truncated]" if out_truncated or err_truncated else ""
        message = f"Exit code {result.return_code}\nstdout:\n{stdout}\nstderr:\n{stderr}{suffix}"
        return AgentToolResult(
            [TextContent(message)], details=details, is_error=result.return_code != 0
        )


def _bounded(text):
    data = (text or "").encode("utf-8")
    limit = LIMITS["output_bytes_per_stream"]
    return data[:limit].decode("utf-8", errors="ignore"), len(data) > limit


class HarborExecution:
    environment = ENVIRONMENT

    def __init__(self, backend, trial_id, session_id, workspace):
        self.backend = backend
        self.task_id = str(trial_id)
        self.session_id = session_id
        self.workspace = workspace.resolve()
        self.run_id = None
        self.released = False
        self.uncertain = False
        self.interruption = None

    async def contain_interrupted_command(self, reason):
        self.uncertain = True
        self.interruption = {"reason": reason, "main_stop": "unverified"}

        # Harbor otherwise attempts recovery collection before teardown.
        # Stop the main service through Harbor before returning to that path.
        async def stop_main():
            try:
                async with asyncio.timeout(30):
                    await self.backend.stop_service("main")
            except (OSError, RuntimeError) as error:
                self.interruption["stop_error"] = type(error).__name__
            else:
                self.interruption["main_stop"] = "returned"

        stop = asyncio.create_task(stop_main())
        cancelled = False
        while not stop.done():
            try:
                await asyncio.shield(stop)
            except asyncio.CancelledError:
                cancelled = True
        stop.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def bind(self, session_id, run_id, workspace):
        if (
            session_id != self.session_id
            or workspace.resolve() != self.workspace
            or self.run_id is not None
            or str(self.backend.context_id) != self.task_id
        ):
            raise PermissionError("Harbor task/session/run binding mismatch")
        self.run_id = run_id

    def check(self, context):
        if (
            context is None
            or context.task_id != self.task_id
            or context.session_id != self.session_id
            or context.run_id != self.run_id
            or self.run_id is None
            or self.released
            or self.uncertain
            or context.workspace.resolve() != self.workspace
            or str(self.backend.context_id) != self.task_id
        ):
            raise PermissionError(
                "Harbor command binding mismatch or command outcome unknown"
            )

    async def snapshot(self, run_id):
        if run_id != self.run_id:
            raise PermissionError("Harbor snapshot binding mismatch")
        return None

    async def release(self, run_id):
        if run_id != self.run_id:
            raise PermissionError("Harbor release binding mismatch")
        self.released = True

    def evidence(self):
        return {
            "task_id": self.task_id,
            "harbor_trial_id": self.task_id,
            "run_id": str(self.run_id),
            "tool_profile": PROFILE,
            "adapter_released": self.released,
            "command_outcome_unknown": self.uncertain,
            **(
                {"interruption": self.interruption}
                if self.interruption is not None
                else {}
            ),
            "cleanup": "harbor_unverified",
        }

    def registry(self):
        registry = ToolRegistry()
        registry.register(TerminalCommand(self))
        return registry


class Slot:
    def __init__(self, root, plan, database, client, *, model=MODEL):
        self.root = root
        self.plan = plan
        self.database = database
        self.client = client
        self.model = model
        self.trial_id = self.agent_dir = self.backend = self.environment_session = None
        self.started = False

    async def on_start(self, event):
        jobs = self.root / "harbor" / "tb2"
        if (
            self.trial_id is not None
            or event.config.trials_dir.resolve() != jobs
            or event.config.task.path.resolve() != self.root / self.plan["task"]
            or Path(event.trial_name).name != event.trial_name
            or event.trial_name in {"", ".", ".."}
        ):
            raise PermissionError("Unexpected Harbor trial for the claimed slot")
        self.agent_dir = TrialPaths(jobs / event.trial_name).agent_dir.resolve()
        self.trial_id = event.trial_id
        self.environment_session = f"{event.trial_name}__env"
        publish(
            self.root / "harbor-binding.json",
            {
                "slot_id": self.plan["slot_id"],
                "harbor_trial_id": str(self.trial_id),
                "trial_name": event.trial_name,
            },
        )

    def check(self, agent, backend):
        if (
            self.trial_id is None
            or self.trial_id != agent.context_id
            or self.trial_id != backend.context_id
            or backend.session_id != self.environment_session
            or agent.logs_dir.resolve() != self.agent_dir
        ):
            raise PermissionError("Harbor agent/environment binding mismatch")


class ChagentTerminalAgent(BaseAgent):
    @staticmethod
    def name():
        return PROFILE

    def version(self):
        return "1.0.0"

    async def setup(self, environment):
        slot = ACTIVE_SLOT.get()
        if slot is None or slot.backend is not None or self.extra_env:
            raise PermissionError(
                "No new controlled Harbor slot or unexpected agent environment"
            )
        slot.check(self, environment)
        slot.backend = environment

    async def run(self, instruction, environment, context):
        slot = ACTIVE_SLOT.get()
        if (
            slot is None
            or slot.started
            or slot.backend is not environment
            or sha256_text(instruction) != slot.plan["instruction_sha256"]
        ):
            raise PermissionError(
                "Harbor slot already used or command environment/instructions differ"
            )
        slot.check(self, environment)
        slot.started = True
        profile = slot.plan["profile"]
        limits = profile["limits"]
        live = slot.plan.get("live")
        if live:
            terms = SimpleNamespace(budget=live["budget"], pricing=live["pricing"])
            guard = LiveDiagnosticBudget(slot.client, terms, profile, PROMPT)
        else:
            budget = {**limits, "total_cost_usd": 1}
            terms = SimpleNamespace(budget=budget, pricing=None)
            guard = BudgetClient(
                slot.client, terms, terms, synthetic=True, expected_preamble=PROMPT
            )
        with TemporaryDirectory(prefix="chagent-tb2-control-") as control:
            workspace = Path(control)
            session_id = await ConversationRepository(slot.database).create_session(
                workspace
            )
            execution = HarborExecution(
                environment, slot.trial_id, session_id, workspace
            )
            runner = create_bound_runner(
                slot.database,
                slot.model,
                guard,
                execution=execution,
                max_turns=limits["max_turns"],
                timeout=limits["wall_seconds"],
                wall_timeout=limits["wall_seconds"],
                context_policy=ContextPolicy(output_tokens=slot.model.max_tokens),
            )
            runner.context.instructions = PROMPT

            def on_started(run_id):
                binding = {
                    "slot_id": slot.plan["slot_id"],
                    "harbor_trial_id": str(slot.trial_id),
                    "session_id": str(session_id),
                    "run_id": str(run_id),
                    "profile": PROFILE,
                }
                publish(slot.root / "run-binding.json", binding)
                context.metadata = binding

            end_reason = None
            try:
                result = await runner.run(
                    session_id, instruction, on_started=on_started
                )
                end_reason = result.end_reason
                if (
                    execution.uncertain
                    or end_reason != "model_stop"
                    or guard.violation
                    or guard.unknown_cost
                ):
                    raise RuntimeError(
                        "Chagent candidate did not complete its controlled run"
                    )
            except RunFailedError as error:
                end_reason = error.end_reason
                raise
            except asyncio.CancelledError:
                end_reason = "user_cancelled"
                raise
            finally:
                publish(
                    slot.root / "candidate.json",
                    {
                        "end_reason": end_reason,
                        "execution": execution.evidence(),
                        "model_calls": guard.model_calls,
                        "tool_calls": guard.tool_calls,
                        "verified_preambles": guard.verified_requests,
                        "candidate_cost": guard.cost(),
                        **(
                            {
                                "usage_calls": guard.usage_calls,
                                "usage": guard.token_totals,
                                "budget_violation": guard.violation,
                                "known_cost_usd": round(guard.spent, 9),
                            }
                            if live
                            else {}
                        ),
                    },
                )


def profile_identity(model=MODEL, limits=LIMITS):
    return {
        "name": PROFILE,
        "model": asdict(model),
        "limits": limits,
        "prompt_sha256": sha256_json(
            {"instructions": PROMPT, "environment": ENVIRONMENT}
        ),
        "tools_sha256": sha256_json([asdict(TerminalCommand().declaration())]),
    }
