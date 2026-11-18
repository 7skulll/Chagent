"""Command-only AgentRunner adapter for a single disposable task/session.

The local directory is empty control metadata, never the task's filesystem.
Collection and topology cleanup remain the controller's responsibility.
"""
from pathlib import Path

from chagent import workspace_manifest
from chagent.tools.base import AgentTool
from chagent.tools.registry import ToolRegistry


class TaskExecution:
    def __init__(self, task, session_id, workspace):
        spec = task.state.get('environment_spec')
        if not isinstance(spec, dict) or spec.get('version') != 1 or spec.get('network') not in {'none', 'bridge', 'internal'}:
            raise ValueError(
                'Unsupported task environment format for AgentRunner. '
                'Create a new task from the frozen snapshot with a version 1 environment specification. '
                'Existing resources can still be recovered with eval.cli task-env cleanup --out <task-directory>.')
        self.task = task
        self.session_id = session_id
        self.workspace = Path(workspace).resolve()
        if any(self.workspace.iterdir()):
            raise ValueError('Task control workspace must be empty')
        self.task_id = task.state['id']
        self.run_id = None
        self.environment = (
            'Boundary: disposable task container. Only exec_command is available. '
            'Task files are in /workspace; the host control directory is not task data. '
            'Commands are one-shot (at most 30 seconds); use shell cd for subdirectories. '
            'No host file tools, persistent processes, skills or cross-task memory are available. '
            f"Network mode: {spec['network']}. "
            'Only declared artifacts are collected by the controller after this run.'
        )

    def evidence(self):
        return {'task_id': self.task_id, 'run_id': str(self.run_id) if self.run_id else None,
                'environment': self.task.state.get('environment'),
                'tool_profile': 'task-command-only-v1',
                'phase': self.task.state['phase'],
                'cleanup': self.task.state.get('run_cleanup', 'not_started')}

    async def bind(self, session_id, run_id, workspace):
        if (session_id != self.session_id or Path(workspace).resolve() != self.workspace
                or self.run_id is not None or self.task.state.get('runner_binding') is not None
                or self.task.state['id'] != self.task_id or self.task.state['phase'] != 'ready'):
            raise PermissionError('Task/session/run binding mismatch or task already consumed')
        await self.task.admit()
        self.run_id = run_id
        self.task.state['runner_binding'] = {'session_id': str(session_id), 'run_id': str(run_id),
                                             'task_id': self.task_id, 'profile': 'task-command-only-v1'}
        self.task.state['run_cleanup'] = 'pending'
        self.task.save()

    def check(self, context):
        if (context is None or context.task_id != self.task_id or context.session_id != self.session_id or context.run_id != self.run_id
                or self.run_id is None or context.workspace.resolve() != self.workspace
                or self.task.state['id'] != self.task_id
                or self.task.state.get('runner_binding', {}).get('run_id') != str(self.run_id)):
            raise PermissionError('Task command binding mismatch')

    async def snapshot(self, run_id):
        if run_id != self.run_id:
            raise PermissionError('Task evidence binding mismatch')
        await self.task.admit()
        source = Path(workspace_manifest.__file__).read_text()
        raw = await self.task.docker('exec', self.task.name, '/app/.venv/bin/python', '-I', '-c',
            source + '\nimport json\nMAX_FILES=10000\nMAX_HASH_BYTES=100*1024**2\n'
            'print(json.dumps(snapshot(Path("/workspace"))))')
        import json
        return {**json.loads(raw), 'task_id': self.task_id,
                'container_id': self.task.state['environment']['container_id']}

    async def release(self, run_id):
        # A rejected second session/run must never release another run's resources.
        if self.run_id != run_id:
            return
        try:
            invocation = self.task.state.get('invocation')
            if invocation:
                await self.task.sandbox.finish_invocation(invocation)
            await self.task.admit()
            await self.task.docker('exec', self.task.name, 'rm', '-rf', '--', f'/tmp/chagent-run-{run_id}')
            self.task.state['run_cleanup'] = 'confirmed'
        except (OSError, RuntimeError):
            self.task.state['run_cleanup'] = 'unknown'
            self.task.state['phase'] = 'execution_interrupted'
            raise
        finally:
            self.task.save()

    def registry(self):
        registry = ToolRegistry()
        registry.register(TaskCommand(self))
        return registry


class TaskCommand(AgentTool):
    def __init__(self, execution):
        self.execution = execution
        super().__init__(name='exec_command', label='Task command',
            description='Run a one-shot /bin/sh command in this task container at /workspace.',
            parameters={'type': 'object', 'properties': {
                'cmd': {'type': 'string', 'minLength': 1, 'maxLength': 32768},
                'timeout_ms': {'type': 'integer', 'minimum': 100, 'maximum': 30000}},
                'required': ['cmd'], 'additionalProperties': False}, requires_run_context=True,
            mutates_workspace=True)

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context=None):
        self.execution.check(context)
        if signal is not None and signal.is_set():
            import asyncio
            raise asyncio.CancelledError()
        return await self.execution.task.run(params['cmd'], timeout=params.get('timeout_ms', 10000) / 1000,
                                             context=context, tool_call_id=tool_call_id)
