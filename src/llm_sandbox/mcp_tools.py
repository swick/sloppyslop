"""Model Context Protocol (MCP) tools - base classes and definitions."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional


class MCPTool(ABC):
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

    @abstractmethod
    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the tool with given arguments.

        Args:
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        pass


class MCPServer(ABC):
    """Abstract base class for MCP servers."""

    def __init__(self):
        """Initialize MCP server with tools dictionary."""
        self.tools: Dict[str, MCPTool] = {}

    def get_tools(self) -> List[MCPTool]:
        """
        Get list of available MCP tools.

        Returns:
            List of MCP tools
        """
        return list(self.tools.values())

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute MCP tool.

        Args:
            tool_name: Name of tool to execute
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        if tool_name not in self.tools:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
            }

        tool = self.tools[tool_name]
        return tool.execute(arguments)


# Container tools


class ExecuteCommandTool(MCPTool):
    """Tool for executing shell commands in container."""

    def __init__(self, container_manager, container_id: str):
        """
        Initialize execute command tool.

        Args:
            container_manager: Container manager instance
            container_id: Container ID to execute commands in
        """
        super().__init__(
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
        )
        self.container_manager = container_manager
        self.container_id = container_id

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute shell command in container."""
        command = arguments["command"]
        workdir = arguments.get("workdir", "/workspace")

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


class GitCommitTool(MCPTool):
    """Tool for committing files to git worktree."""

    def __init__(self, container_manager, container_id: str):
        """
        Initialize git commit tool.

        Args:
            container_manager: Container manager instance
            container_id: Container ID to execute commands in
        """
        super().__init__(
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
        )
        self.container_manager = container_manager
        self.container_id = container_id

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Commit files with message."""
        files = arguments["files"]
        message = arguments["message"]
        branch = arguments.get("branch")

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


# Local tools


class ReadFileTool(MCPTool):
    """Tool for reading files from the project directory."""

    def __init__(self, project_path: Path):
        """
        Initialize read file tool.

        Args:
            project_path: Path to project directory
        """
        super().__init__(
            name="read_file",
            description="Read contents of a file in the project directory",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file relative to project root",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum number of lines to read (optional)",
                        "default": 1000,
                    },
                },
                "required": ["path"],
            },
        )
        self.project_path = project_path

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Read a file from the project directory."""
        path = arguments["path"]
        max_lines = arguments.get("max_lines", 1000)

        try:
            file_path = self.project_path / path

            # Security check: ensure path is within project directory
            file_path = file_path.resolve()
            if not str(file_path).startswith(str(self.project_path.resolve())):
                return {
                    "success": False,
                    "error": "Path outside project directory",
                }

            if not file_path.exists():
                return {
                    "success": False,
                    "error": "File not found",
                }

            if not file_path.is_file():
                return {
                    "success": False,
                    "error": "Path is not a file",
                }

            # Read file content
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            if len(lines) > max_lines:
                content = "".join(lines[:max_lines])
                content += f"\n... (truncated, {len(lines) - max_lines} more lines)"
            else:
                content = "".join(lines)

            return {
                "success": True,
                "content": content,
                "lines": len(lines),
                "path": path,
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }


class ListDirectoryTool(MCPTool):
    """Tool for listing directory contents."""

    def __init__(self, project_path: Path):
        """
        Initialize list directory tool.

        Args:
            project_path: Path to project directory
        """
        super().__init__(
            name="list_directory",
            description="List files and directories in a path",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to project root (default: root)",
                        "default": ".",
                    },
                },
            },
        )
        self.project_path = project_path

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """List contents of a directory."""
        path = arguments.get("path", ".")

        try:
            dir_path = self.project_path / path

            # Security check: ensure path is within project directory
            dir_path = dir_path.resolve()
            if not str(dir_path).startswith(str(self.project_path.resolve())):
                return {
                    "success": False,
                    "error": "Path outside project directory",
                }

            if not dir_path.exists():
                return {
                    "success": False,
                    "error": "Directory not found",
                }

            if not dir_path.is_dir():
                return {
                    "success": False,
                    "error": "Path is not a directory",
                }

            # List directory contents
            entries = []
            for item in sorted(dir_path.iterdir()):
                rel_path = item.relative_to(self.project_path)
                entries.append({
                    "name": item.name,
                    "path": str(rel_path),
                    "type": "directory" if item.is_dir() else "file",
                })

            return {
                "success": True,
                "path": path,
                "entries": entries,
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }
