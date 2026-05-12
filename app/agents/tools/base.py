"""Tool registry and execution for Pulse agents.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolParam:
    name: str
    param_type: str  # "string", "number", "integer", "boolean", "array", "object"
    description: str
    required: bool = True
    enum: list[str] | None = None


@dataclass(frozen=True)
class Tool:
    """A tool available to an agent."""

    name: str
    description: str
    parameters: list[ToolParam] = field(default_factory=list)
    execute: Callable[..., Awaitable[Any]] = field(repr=False, default=None)

    def to_claude_schema(self) -> dict:
        """Convert to Anthropic Claude tool definition."""
        properties = {}
        required_params = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.param_type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            properties[param.name] = prop
            if param.required:
                required_params.append(param.name)

        schema: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        }
        if required_params:
            schema["input_schema"]["required"] = required_params

        return schema


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str = ""
    duration_ms: int = 0

    def to_content_block(self) -> dict:
        """Format as a Claude tool_result content block."""
        if not self.success:
            content = json.dumps({"error": self.error, "tool": self.tool_name})
        elif isinstance(self.data, str):
            content = self.data
        else:
            content = json.dumps(self.data, default=str)
        return {
            "type": "tool_result",
            "tool_use_id": self.call_id if hasattr(self, "call_id") else "",
            "content": content,
        }


class ToolRegistry:
    """Manages tool registration and execution for an agent."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.call_log: list[dict] = []

    def register(self, tool: Tool) -> None:
        if tool.execute is None:
            raise ValueError(f"Tool '{tool.name}' has no execute function")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_claude_tools(self) -> list[dict]:
        """Return tool definitions in Claude's format."""
        return [tool.to_claude_schema() for tool in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """Execute a tool call and return the result."""
        tool = self._tools.get(call.tool_name)
        if tool is None:
            return ToolResult(
                tool_name=call.tool_name,
                success=False,
                error=f"Unknown tool: {call.tool_name}. Available: {', '.join(self._tools)}",
            )

        expected = {p.name for p in tool.parameters}
        required = {p.name for p in tool.parameters if p.required}
        provided = set(call.arguments.keys())

        missing = required - provided
        if missing:
            return ToolResult(
                tool_name=call.tool_name,
                success=False,
                error=f"Missing required parameters: {', '.join(sorted(missing))}",
            )

        resolved = {}
        for param in tool.parameters:
            if param.name in call.arguments:
                resolved[param.name] = call.arguments[param.name]

        t0 = time.monotonic()
        try:
            result_data = await tool.execute(**resolved)
            elapsed = int((time.monotonic() - t0) * 1000)
            self.call_log.append({
                "tool": call.tool_name,
                "arguments": call.arguments,
                "success": True,
                "duration_ms": elapsed,
            })
            return ToolResult(
                tool_name=call.tool_name,
                success=True,
                data=result_data,
                duration_ms=elapsed,
            )
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.error("Tool '%s' failed after %dms: %s", call.tool_name, elapsed, e)
            self.call_log.append({
                "tool": call.tool_name,
                "arguments": call.arguments,
                "success": False,
                "error": str(e),
                "duration_ms": elapsed,
            })
            return ToolResult(
                tool_name=call.tool_name,
                success=False,
                error=str(e),
                duration_ms=elapsed,
            )


def parse_tool_calls_from_claude(response_content: list) -> list[ToolCall]:
    """Extract ToolCall objects from Claude's response content blocks."""
    calls = []
    for block in response_content:
        if hasattr(block, "type") and block.type == "tool_use":
            calls.append(
                ToolCall(
                    tool_name=block.name,
                    arguments=dict(block.input) if block.input else {},
                    call_id=block.id,
                )
            )
    return calls
