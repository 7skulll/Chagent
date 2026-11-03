"""Shared work file editing, explicit read-only code, and isolated writable code."""
from chagent.config import Settings
from chagent.tools.builtin import WRITE_TOOLS


def validate_web_settings(settings: Settings) -> None:
    if not settings.code_mode:
        return
    if not (settings.readonly_code_available or settings.trusted_development):
        raise ValueError("网页 code 需要只读代码分析配置，或显式 trusted-development 配置；"
                         "两者均需 isolated、命令断网和宿主命令开启")


def validate_web_runner(settings: Settings, runner) -> None:
    validate_web_settings(settings)
    file_writers = WRITE_TOOLS | {'create_file'}
    work = (runner.tool_faces or {}).get('work')
    if work is not None and file_writers & {tool.name for tool in work.registry.definitions()} != file_writers:
        raise ValueError("网页 work 工具面必须允许工作区文件编辑")
    if not settings.code_mode:
        return
    writable = settings.trusted_development
    sandbox = runner.sandbox
    if (sandbox is None or not sandbox.enabled or sandbox.settings.mode != "isolated"
            or sandbox.settings.writable_workspace != writable or sandbox.settings.network):
        raise ValueError("网页 code 的实际 runner 必须使用匹配读写能力的 isolated 断网沙箱")
    faces = runner.tool_faces or {}
    face = faces.get("code")
    if face is None or not face.allow_host_commands:
        raise ValueError("网页 code 缺少独立工具面或宿主命令权限")
    for mode, executor in faces.items():
        writes = file_writers & {tool.name for tool in executor.registry.definitions()}
        expected = file_writers if mode == "work" or writable else frozenset()
        if writes != expected:
            raise ValueError("网页工具面工作区写工具与配置不一致")
    if writable and runner.scheduler.policy.mode != "never":
        raise ValueError("trusted-development 实际审批策略必须是 never")
    process = face.registry.get("process")
    if process is None or set(process.allowed_actions) != {"read", "stop"}:
        raise ValueError("网页 code 的 process 只能 read/stop")
    command = face.registry.get("exec_command")
    tests = face.registry.get("run_tests")
    if (command is None or tests is None or command.sandbox is not sandbox
            or tests.sandbox is not sandbox or command.process_stdin):
        raise ValueError("网页命令工具必须共用实际沙箱并禁用交互输入")
