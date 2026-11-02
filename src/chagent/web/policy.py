"""The web code capability is narrower than the CLI code capability."""
from chagent.config import Settings
from chagent.tools.builtin import WRITE_TOOLS


def validate_web_settings(settings: Settings) -> None:
    if not settings.code_mode:
        return
    if not settings.readonly_code_available:
        raise ValueError("网页 code 仅支持只读代码分析：需要 CHAGENT_SANDBOX=isolated、"
                         "CHAGENT_SANDBOX_WRITABLE=false、CHAGENT_SANDBOX_NETWORK=false、"
                         "CHAGENT_ALLOW_HOST_COMMANDS=true")


def validate_web_runner(settings: Settings, runner) -> None:
    validate_web_settings(settings)
    if not settings.code_mode:
        return
    sandbox = runner.sandbox
    if (sandbox is None or not sandbox.enabled or sandbox.settings.mode != "isolated"
            or sandbox.settings.writable_workspace or sandbox.settings.network):
        raise ValueError("网页 code 的实际 runner 必须使用 isolated 只读断网沙箱")
    faces = runner.tool_faces or {}
    face = faces.get("code")
    if face is None or not face.allow_host_commands:
        raise ValueError("网页 code 缺少独立工具面或宿主命令权限")
    for executor in faces.values():
        if WRITE_TOOLS & {tool.name for tool in executor.registry.definitions()}:
            raise ValueError("网页工具面不能包含工作区写工具")
    process = face.registry.get("process")
    if process is None or set(process.allowed_actions) - {"read", "stop"}:
        raise ValueError("网页只读 code 的 process 只能 read/stop")
