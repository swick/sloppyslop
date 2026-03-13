"""Model Context Protocol (MCP) tools - base classes and definitions."""

import re
import uuid
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
                        "description": "Working directory for command (default: /worktrees)",
                        "default": "/worktrees",
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
        workdir = arguments.get("workdir", "/worktrees")

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

    def __init__(
        self,
        container_manager,
        container_id: str,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize git commit tool.

        Args:
            container_manager: Container manager instance (unused, kept for compatibility)
            container_id: Container ID (unused, kept for compatibility)
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for host git operations
        """
        super().__init__(
            name="git_commit",
            description="Commit files to a worktree. Stages the specified files and creates a commit. Branch parameter is REQUIRED and must match pattern llm-container/{instance_id}/{worktree_name}",
            parameters={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to commit (relative to worktree)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name (REQUIRED, must match llm-container/{instance_id}/{worktree_name})",
                    },
                },
                "required": ["files", "message", "branch"],
            },
        )
        self.instance_id = instance_id
        self.runner = runner

    def _validate_branch_pattern(self, branch: str) -> bool:
        """Validate branch matches required pattern."""
        pattern = rf"^llm-container/{re.escape(self.instance_id)}/[a-zA-Z0-9_-]+$"
        return bool(re.match(pattern, branch))

    def _derive_worktree_path(self, branch: str) -> str:
        """Derive worktree path from branch name."""
        # Extract worktree name from branch: llm-container/{instance_id}/{worktree_name}
        parts = branch.split("/")
        if len(parts) >= 3:
            worktree_name = parts[-1]
            return f"/worktrees/{worktree_name}"
        return "/worktrees"

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Commit files with message."""
        files = arguments["files"]
        message = arguments["message"]
        branch = arguments["branch"]

        # Validate branch pattern
        if not self._validate_branch_pattern(branch):
            return {
                "success": False,
                "error": f"Invalid branch name: {branch}. Must match pattern: llm-container/{self.instance_id}/<worktree_name>",
            }

        # Extract worktree name from branch
        worktree_name = branch.split("/")[-1]

        # Get host worktree path
        if not self.runner.worktrees_base_dir:
            return {
                "success": False,
                "error": "Worktrees base directory not initialized",
            }

        host_worktree_path = self.runner.worktrees_base_dir / worktree_name

        if not host_worktree_path.exists():
            return {
                "success": False,
                "error": f"Worktree does not exist: {worktree_name}",
            }

        try:
            # Use GitOperations to commit files
            self.runner.git_ops.commit_files(
                host_worktree_path,
                files,
                message,
            )

            return {
                "success": True,
                "branch": branch,
                "worktree_path": str(host_worktree_path),
                "message": "Files committed successfully",
            }

        except RuntimeError as e:
            # Git command failed
            error_msg = str(e)
            return {
                "success": False,
                "error": error_msg,
                "branch": branch,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
            }


class CheckoutCommitTool(MCPTool):
    """Tool for creating new worktrees from commits."""

    def __init__(
        self,
        container_manager,
        container_id: str,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize checkout commit tool.

        Args:
            container_manager: Container manager instance (unused, kept for compatibility)
            container_id: Container ID (unused, kept for compatibility)
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for git operations and tracking worktrees
        """
        super().__init__(
            name="checkout_commit",
            description="Create a new worktree from any commit. Creates worktree at /worktrees/{worktree_name} with branch llm-container/{instance_id}/{worktree_name}",
            parameters={
                "type": "object",
                "properties": {
                    "commit": {
                        "type": "string",
                        "description": "Commit hash/branch/tag to checkout",
                    },
                    "worktree_name": {
                        "type": "string",
                        "description": "Name for worktree (optional, auto-generated if omitted). Must match [a-zA-Z0-9_-]+",
                    },
                },
                "required": ["commit"],
            },
        )
        self.instance_id = instance_id
        self.runner = runner

    def _validate_worktree_name(self, name: str) -> bool:
        """Validate worktree name matches allowed pattern."""
        return bool(re.match(r"^[a-zA-Z0-9_-]+$", name))

    def _generate_worktree_name(self) -> str:
        """Generate a unique worktree name."""
        short_uuid = str(uuid.uuid4())[:8]
        return f"wt-{short_uuid}"

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Create worktree from commit."""
        commit = arguments["commit"]
        worktree_name = arguments.get("worktree_name")

        # Auto-generate name if not provided
        if not worktree_name:
            worktree_name = self._generate_worktree_name()

        # Validate worktree name
        if not self._validate_worktree_name(worktree_name):
            return {
                "success": False,
                "error": f"Invalid worktree name: {worktree_name}. Must match [a-zA-Z0-9_-]+",
            }

        # Check for duplicates
        if worktree_name in self.runner.created_worktrees:
            return {
                "success": False,
                "error": f"Worktree '{worktree_name}' already exists in this session",
            }

        # Check that worktrees base directory exists
        if not self.runner.worktrees_base_dir:
            return {
                "success": False,
                "error": "Worktrees base directory not initialized",
            }

        # Generate branch name
        branch_name = f"llm-container/{self.instance_id}/{worktree_name}"

        # Get host worktree path
        host_worktree_path = self.runner.worktrees_base_dir / worktree_name

        try:
            # Use GitOperations to create worktree on host
            self.runner.git_ops.create_worktree_on_branch(
                commit,
                host_worktree_path,
                branch_name,
            )

            # Track created worktree
            self.runner.created_worktrees.append(worktree_name)

            return {
                "success": True,
                "worktree_name": worktree_name,
                "worktree_path": f"/worktrees/{worktree_name}",
                "branch_name": branch_name,
                "commit": commit,
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to create worktree: {str(e)}",
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
            # This is actually a TOCTOU issue, but we assume that the
            # project_path doesn't have symlinks pointing outside of it and also
            # that the container cannot modify them. We have to exclude
            # ".llm-sandbox" from accepted paths
            if ".llm-sandbox" in path:
                return {
                    "success": False,
                    "error": "Permission denied",
                }

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
            # This is actually a TOCTOU issue, but we assume that the
            # project_path doesn't have symlinks pointing outside of it and also
            # that the container cannot modify them. We have to exclude
            # ".llm-sandbox" from accepted paths
            if ".llm-sandbox" in path:
                return {
                    "success": False,
                    "error": "Permission denied",
                }

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
