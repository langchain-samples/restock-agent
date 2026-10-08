"""Keep shell, private files and delegated agents outside the model's purchase path."""

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from managed_deepagents._managed_tools import _ManagedRuntime


class RestockToolPolicy(AgentMiddleware):
    def __init__(self, tools):
        self.allowed = {tool.name for tool in tools} | {"write_todos", "read_file"}

    async def awrap_model_call(self, request, handler):
        tools = [tool for tool in request.tools if getattr(tool, "name", None) in self.allowed]
        return await handler(request.override(tools=tools))

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        allowed = call["name"] in self.allowed
        if call["name"] == "read_file":
            # Exactly this deploy-owned instruction file, never a sandbox path,
            # symlink, private output, session file or arbitrary user memory.
            allowed = call.get("args", {}).get("file_path") == "/skills/restock/SKILL.md"
        if not allowed:
            return ToolMessage(
                content='{"status":"not_allowed","reason":"use_restock_tools"}',
                tool_call_id=call["id"],
                name=call["name"],
            )
        # MDA 0.8.1 augments middleware requests with a runtime proxy, which
        # ToolNode's typed ToolRuntime validation rejects. Restore the original
        # runtime for dispatch; MDA's tool wrapper injects its trusted extras
        # after validation. Preserve its original caller config, not a copy.
        runtime = request.runtime
        while isinstance(runtime, _ManagedRuntime):
            runtime = object.__getattribute__(runtime, "_managed_runtime")
        return await handler(request.override(runtime=runtime))
