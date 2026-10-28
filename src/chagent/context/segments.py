"""Closed model/tool exchanges suitable for safe active-run cut boundaries."""

from chagent.domain.messages import AssistantMessage, SystemMessage, ToolCall, ToolResultMessage, UserMessage


def exchanges(history):
    """Validate and group an assistant response with its entire tool-result batch."""
    groups, batch, pending = [], [], {}
    for item in history:
        message = item.message
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, ToolResultMessage):
            if pending.get(message.tool_call_id) != message.tool_name:
                raise ValueError("Orphan or mismatched tool result in exchange")
            pending.pop(message.tool_call_id)
            batch.append(item)
            if not pending:
                groups.append(batch)
                batch = []
        else:
            if pending:
                raise ValueError("Cannot cut an unfinished tool batch")
            if isinstance(message, UserMessage):
                continue
            if not isinstance(message, AssistantMessage):
                raise ValueError("Unsupported exchange message")
            calls = [block for block in message.content if isinstance(block, ToolCall)]
            if calls:
                if message.stop_reason != "toolUse" or len({call.id for call in calls}) != len(calls):
                    raise ValueError("Invalid tool batch")
                pending = {call.id: call.name for call in calls}
                batch = [item]
            else:
                groups.append([item])
    if pending:
        raise ValueError("Cannot compact an unfinished tool batch")
    return groups
