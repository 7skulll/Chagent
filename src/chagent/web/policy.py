"""Validate the single programmable web tool surface."""
from pathlib import Path
from chagent.config import Settings
from chagent.tools.builtin import WRITE_TOOLS, EXECUTION_TOOLS


def validate_web_settings(settings: Settings) -> None:
    """The page offers a writable container, with or without approval.

    Everything else about the switches is already settled by
    ``Settings.axes``; what remains here is the fact a configuration cannot
    know — whether this process is actually inside a container.
    """
    axes = settings.axes
    in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    managed = settings.execution_profile == "host-controller"
    if managed and settings.approval != "on-request":
        raise ValueError("宿主主控要求 CHAGENT_APPROVAL=on-request，以便宿主原生操作逐次审批")
    if not axes.programmable or (not managed and (not in_container or not settings.container_declared)) \
            or (managed and in_container):
        raise ValueError("网页执行要求实际容器，或在宿主启用 host-controller 与 Docker 执行后端")


def validate_web_runner(settings: Settings, runner) -> None:
    validate_web_settings(settings)
    sandbox = runner.sandbox
    if sandbox is None or not sandbox.settings.allows_execution or not sandbox.settings.network:
        raise ValueError("实际 runner 必须在容器内使用可写工作区")
    faces = runner.tool_faces or {}
    face = faces.get("work")
    if not getattr(runner, "unified", False) or face is None or faces.get("code") is not face or not face.allow_host_commands:
        raise ValueError("旧会话必须共用一个可执行工具面")
    managed = settings.execution_profile == "host-controller"
    required = WRITE_TOOLS | ({"exec_command", "run_tests"} if managed else EXECUTION_TOOLS) | {"create_file"}
    if not required <= {tool.name for tool in face.registry.definitions()}:
        raise ValueError("统一工具面缺少文件或执行工具")
    if runner.scheduler.policy.mode != settings.approval:
        raise ValueError("实际审批策略与配置不一致")
    if settings.approval != "never" and runner.scheduler.approvals is None:
        raise ValueError("审批配置必须连接审批 broker")
    process = face.registry.get("process")
    if not managed and (process is None or set(process.allowed_actions) != {"list", "read", "write", "stop"}):
        raise ValueError("process 必须支持 list/read/write/stop")
    command = face.registry.get("exec_command")
    tests = face.registry.get("run_tests")
    if (command is None or tests is None or command.sandbox is not sandbox
            or tests.sandbox is not sandbox or (not managed and not command.process_stdin)):
        raise ValueError("网页命令工具必须共用实际沙箱并支持交互输入")
    if managed and (face.registry.get("host_exec") is None or face.registry.get("site") is None):
        raise ValueError("宿主主控必须提供网页审批宿主执行与持续网站服务")
