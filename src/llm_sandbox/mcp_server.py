"""Model Context Protocol (MCP) server implementation."""

from pathlib import Path
from typing import Any, Dict, List

from llm_sandbox.container import ContainerManager


class MCPTool:
    """Represents an MCP tool."""

    def __init__(self, name: str, description: str, parameters: Dict[str, Any]):
        """
        Initialize MCP tool.

        Args:
            name: Tool name
            description: Tool description
            parameters: JSON schema for parameters
        """
        self.name = name
        self.description = description
        self.parameters = parameters

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class MCPServer:
    """MCP server for container and git operations."""

    def __init__(self, container_manager: ContainerManager, container_id: str):
        """
        Initialize MCP server.

        Args:
            container_manager: Container manager instance
            container_id: Container ID to execute commands in
        """
        self.container_manager = container_manager
        self.container_id = container_id

    def get_tools(self) -> List[MCPTool]:
        """
        Get list of available MCP tools.

        Returns:
            List of MCP tools
        """
        return [
            MCPTool(
                name="execute_command",
                description="Execute a shell command in the container. No filtering applied - can run any command.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Working directory for command (default: /workspace)",
                            "default": "/workspace",
                        },
                    },
                    "required": ["command"],
                },
            ),
            MCPTool(
                name="git_commit",
                description="Commit files to the worktree. Stages the specified files and creates a commit.",
                parameters={
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of file paths to commit (relative to /workspace)",
                        },
                        "message": {
                            "type": "string",
                            "description": "Commit message",
                        },
                        "branch": {
                            "type": "string",
                            "description": "Optional: Create and switch to a new branch before committing",
                        },
                    },
                    "required": ["files", "message"],
                },
            ),
        ]

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute MCP tool.

        Args:
            tool_name: Name of tool to execute
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        if tool_name == "execute_command":
            return self._execute_command(
                arguments["command"],
                arguments.get("workdir", "/workspace"),
            )
        elif tool_name == "git_commit":
            return self._git_commit(
                arguments["files"],
                arguments["message"],
                arguments.get("branch"),
            )
        else:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
            }

    def _execute_command(self, command: str, workdir: str) -> Dict[str, Any]:
        """Execute shell command in container."""
        exit_code, stdout, stderr = self.container_manager.exec_command(
            self.container_id,
            command,
            workdir,
        )

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _git_commit(
        self, files: List[str], message: str, branch: Optional[str] = None
    ) -> Dict[str, Any]:
        """Commit files with message."""
        commands = []

        # Create branch if specified
        if branch:
            commands.append(f'git checkout -b "{branch}"')

        # Stage files
        files_str = " ".join(f'"{f}"' for f in files)
        commands.append(f"git add {files_str}")

        # Commit changes
        # Escape message for shell
        escaped_message = message.replace('"', '\\"')
        commands.append(f'git commit -m "{escaped_message}"')

        # Execute commands
        command = " && ".join(commands)

        exit_code, stdout, stderr = self.container_manager.exec_command(
            self.container_id,
            command,
            "/workspace",
        )

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "branch": branch,
        }
