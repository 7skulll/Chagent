"""Validate the single programmable web tool surface."""
from pathlib import Path
from chagent.config import Settings
from chagent.tools.builtin import WRITE_TOOLS, EXECUTION_TOOLS


def validate_web_settings(settings: Settings) -> None:
    if settings.execution_profile == "containerized":
        in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
        if not (settings.container_declared and in_container
                and settings.code_mode and settings.allow_host_commands
                and settings.sandbox == "off" and settings.sandbox_writable
                and settings.approval == "never"):
            raise ValueError("containerized 网页执行要求实际容器、显式声明、可写工作区和无逐次审批")
        return
    if not (settings.trusted_development and settings.code_mode
            and settings.allow_host_commands and settings.sandbox == "isolated"
            and settings.sandbox_writable and not settings.sandbox_network
            and settings.approval == "never"):
        raise ValueError("网页编程需要 trusted-development、code=on、可写 isolated 沙箱、宿主命令开启、命令断网且无需逐次审批")


def validate_web_runner(settings: Settings, runner) -> None:
    validate_web_settings(settings)
    sandbox = runner.sandbox
    if settings.execution_profile == "containerized":
        if sandbox is None or sandbox.settings.mode != "off" or not sandbox.settings.writable_workspace:
            raise ValueError("实际 runner 必须在容器内使用可写工作区")
    elif (sandbox is None or not sandbox.enabled or sandbox.settings.mode != "isolated"
          or not sandbox.settings.writable_workspace or sandbox.settings.network):
        raise ValueError("实际 runner 必须使用可写 isolated 断网沙箱")
    faces = runner.tool_faces or {}
    face = faces.get("work")
    if not getattr(runner, "unified", False) or face is None or faces.get("code") is not face or not face.allow_host_commands:
        raise ValueError("旧会话必须共用一个可执行工具面")
    required = WRITE_TOOLS | EXECUTION_TOOLS | {"create_file"}
    if not required <= {tool.name for tool in face.registry.definitions()}:
        raise ValueError("统一工具面缺少文件或执行工具")
    if runner.scheduler.policy.mode != "never":
        raise ValueError("实际审批策略必须是 never")
    process = face.registry.get("process")
    if process is None or set(process.allowed_actions) != {"read", "stop"}:
        raise ValueError("process 只能 read/stop")
    command = face.registry.get("exec_command")
    tests = face.registry.get("run_tests")
    if (command is None or tests is None or command.sandbox is not sandbox
            or tests.sandbox is not sandbox or command.process_stdin):
        raise ValueError("网页命令工具必须共用实际沙箱并禁用交互输入")
