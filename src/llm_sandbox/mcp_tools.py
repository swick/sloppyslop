"""Model Context Protocol (MCP) tools - base classes and definitions."""

import asyncio
import glob
import json
import re
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from llm_sandbox.runner import Agent


class MCPTool(ABC):
    """Represents an MCP tool."""

    def __init__(self, name: str, description: str, parameters: Dict[str, Any], inheritable: bool = True):
        """
        Initialize MCP tool.

        Args:
            name: Tool name
            description: Tool description
            parameters: JSON schema for parameters
            inheritable: Whether this tool can be inherited by spawned child agents (default: True)
        """
        self.name = name
        self.description = description
        self.parameters = parameters
        self.inheritable = inheritable

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    @abstractmethod
    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """
        Execute the tool with given arguments.

        Args:
            arguments: Tool arguments
            agent: Agent instance (provides access to runner, mcp_server, spawn_depth, etc.)
                   May be None when called directly outside agent context.

        Returns:
            Tool execution result
        """
        pass


class MCPServer(ABC):
    """Abstract base class for MCP servers."""

    def __init__(self):
        """Initialize MCP server with tools dictionary."""
        self.tools: Dict[str, MCPTool] = {}
        self.agent: Optional["Agent"] = None  # Set by Agent.__init__

    def add_tool(self, tool: MCPTool) -> None:
        """
        Add a tool to the server.

        Args:
            tool: MCP tool instance to add
        """
        self.tools[tool.name] = tool

    def add_tools(self, tools: List[MCPTool]) -> None:
        """
        Add multiple tools to the server.

        Args:
            tools: List of MCP tool instances to add
        """
        for tool in tools:
            self.add_tool(tool)

    def get_tools(self) -> List[MCPTool]:
        """
        Get list of available MCP tools.

        Returns:
            List of MCP tools
        """
        return list(self.tools.values())

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
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
        return await tool.execute(arguments, self.agent)


# Container tools


class ExecuteCommandTool(MCPTool):
    """Tool for executing shell commands in container."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize execute command tool.

        Args:
            runner: SandboxRunner instance
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
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Execute shell command in container."""
        command = arguments["command"]
        workdir = arguments.get("workdir", "/worktrees")

        exit_code, stdout, stderr = await self.runner.container_manager.exec_command(
            self.runner.container_id,
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

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize git commit tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="git_commit",
            description="Commit files to a worktree. Stages the specified files and creates a commit. Branch parameter is REQUIRED and must match pattern llm-container/{instance_id}/{worktree_name} (worktree_name can contain slashes)",
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
                        "description": "Branch name (REQUIRED, must match llm-container/{instance_id}/{worktree_name}, where worktree_name can contain slashes)",
                    },
                },
                "required": ["files", "message", "branch"],
            },
        )
        self.runner = runner

    def _validate_branch_pattern(self, branch: str) -> bool:
        """Validate branch matches required pattern."""
        pattern = rf"^llm-container/{re.escape(self.runner.instance_id)}/[a-zA-Z0-9_/-]+$"
        return bool(re.match(pattern, branch))

    def _derive_worktree_path(self, branch: str) -> str:
        """Derive worktree path from branch name."""
        # Extract worktree name from branch: llm-container/{instance_id}/{worktree_name}
        # worktree_name can contain slashes (e.g., foo/bar)
        prefix = f"llm-container/{self.runner.instance_id}/"
        if branch.startswith(prefix):
            worktree_name = branch[len(prefix):]
            return f"/worktrees/{worktree_name}"
        return "/worktrees"

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Commit files with message (blocking)."""
        files = arguments["files"]
        message = arguments["message"]
        branch = arguments["branch"]

        # Validate branch pattern
        if not self._validate_branch_pattern(branch):
            return {
                "success": False,
                "error": f"Invalid branch name: {branch}. Must match pattern: llm-container/{self.runner.instance_id}/<worktree_name>",
            }

        # Extract worktree name from branch
        prefix = f"llm-container/{self.runner.instance_id}/"
        if not branch.startswith(prefix):
            return {
                "success": False,
                "error": f"Branch does not match expected prefix: {prefix}",
            }
        worktree_name = branch[len(prefix):]

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
            # Just call the blocking git operation
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

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize checkout commit tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="checkout_commit",
            description="Create a new worktree from any commit. Creates worktree at /worktrees/{worktree_name} with branch llm-container/{instance_id}/{worktree_name}. Worktree names can contain slashes for hierarchy (e.g., 'feature/foo')",
            parameters={
                "type": "object",
                "properties": {
                    "commit": {
                        "type": "string",
                        "description": "Commit hash/branch/tag to checkout",
                    },
                    "worktree_name": {
                        "type": "string",
                        "description": "Name for worktree (optional, auto-generated if omitted). Can contain slashes for hierarchy (e.g., 'feature/foo'). Must match [a-zA-Z0-9_/-]+",
                    },
                },
                "required": ["commit"],
            },
        )
        self.runner = runner

    def _validate_worktree_name(self, name: str) -> bool:
        """Validate worktree name matches allowed pattern."""
        return bool(re.match(r"^[a-zA-Z0-9_/-]+$", name))

    def _generate_worktree_name(self) -> str:
        """Generate a unique worktree name."""
        short_uuid = str(uuid.uuid4())[:8]
        return f"wt-{short_uuid}"

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Create worktree from commit (blocking)."""
        commit = arguments["commit"]
        worktree_name = arguments.get("worktree_name")

        # Auto-generate name if not provided
        if not worktree_name:
            worktree_name = self._generate_worktree_name()

        # Validate worktree name
        if not self._validate_worktree_name(worktree_name):
            return {
                "success": False,
                "error": f"Invalid worktree name: {worktree_name}. Must match [a-zA-Z0-9_/-]+ (can contain slashes for hierarchy)",
            }

        # Check that worktrees base directory exists
        if not self.runner.worktrees_base_dir:
            return {
                "success": False,
                "error": "Worktrees base directory not initialized",
            }

        # Use worktrees lock for atomic check-then-act operation
        async with self.runner._worktrees_lock:
            # Check for duplicates inside lock
            if worktree_name in self.runner.created_worktrees:
                return {
                    "success": False,
                    "error": f"Worktree '{worktree_name}' already exists in this session",
                }

            # Generate branch name
            branch_name = f"llm-container/{self.runner.instance_id}/{worktree_name}"

            # Get host worktree path
            host_worktree_path = self.runner.worktrees_base_dir / worktree_name

            try:
                # Use git lock for the actual git operation (run in thread to avoid blocking)
                async with self.runner._git_lock:
                    await asyncio.to_thread(
                        self.runner.git_ops.create_worktree_on_branch,
                        commit,
                        host_worktree_path,
                        branch_name,
                    )

                # Track created worktree (still inside worktrees lock)
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


class ReadFileTool(MCPTool):
    """Tool for reading files from a worktree."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize read file tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="read_file",
            description="Read the content of a file in a worktree. Only works within checked-out worktrees, not in the read-only /project directory.",
            parameters={
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "Worktree name (must be already created with checkout_commit)",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path relative to worktree root",
                    },
                },
                "required": ["worktree", "path"],
            },
        )
        self.runner = runner

    def _validate_and_resolve_path(self, worktree: str, path: str) -> Tuple[bool, str, Path]:
        """
        Validate worktree and resolve file path.

        Returns:
            Tuple of (success, error_message, resolved_path)
        """
        if not self.runner.worktrees_base_dir:
            return False, "Worktrees base directory not initialized", Path()

        # Check worktree exists
        if worktree not in self.runner.created_worktrees:
            return False, f"Worktree '{worktree}' does not exist. Use checkout_commit first.", Path()

        worktree_path = self.runner.worktrees_base_dir / worktree
        if not worktree_path.exists():
            return False, f"Worktree directory does not exist: {worktree}", Path()

        # Resolve path and check it's within worktree
        try:
            file_path = (worktree_path / path).resolve()
            worktree_path_resolved = worktree_path.resolve()

            # Security check: ensure path is within worktree
            if not str(file_path).startswith(str(worktree_path_resolved)):
                return False, "Path escapes worktree directory", Path()

            return True, "", file_path
        except Exception as e:
            return False, f"Invalid path: {str(e)}", Path()

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Read file from worktree (blocking)."""
        worktree = arguments["worktree"]
        path = arguments["path"]

        # Validate and resolve path
        success, error, file_path = self._validate_and_resolve_path(worktree, path)
        if not success:
            return {"success": False, "error": error}

        # Check file exists
        if not file_path.exists():
            return {"success": False, "error": f"File not found: {path}"}

        if not file_path.is_file():
            return {"success": False, "error": f"Path is not a file: {path}"}

        # Read file (blocking)
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            return {
                "success": True,
                "content": content,
                "path": path,
                "size": len(content),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to read file: {str(e)}"}


class WriteFileTool(MCPTool):
    """Tool for writing files to a worktree."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize write file tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="write_file",
            description="Create or overwrite a file in a worktree. Only works within checked-out worktrees.",
            parameters={
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "Worktree name (must be already created with checkout_commit)",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path relative to worktree root",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["worktree", "path", "content"],
            },
        )
        self.runner = runner

    def _validate_and_resolve_path(self, worktree: str, path: str) -> Tuple[bool, str, Path]:
        """Validate worktree and resolve file path."""
        if not self.runner.worktrees_base_dir:
            return False, "Worktrees base directory not initialized", Path()

        if worktree not in self.runner.created_worktrees:
            return False, f"Worktree '{worktree}' does not exist. Use checkout_commit first.", Path()

        worktree_path = self.runner.worktrees_base_dir / worktree
        if not worktree_path.exists():
            return False, f"Worktree directory does not exist: {worktree}", Path()

        try:
            file_path = (worktree_path / path).resolve()
            worktree_path_resolved = worktree_path.resolve()

            if not str(file_path).startswith(str(worktree_path_resolved)):
                return False, "Path escapes worktree directory", Path()

            return True, "", file_path
        except Exception as e:
            return False, f"Invalid path: {str(e)}", Path()

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Write file to worktree (blocking)."""
        worktree = arguments["worktree"]
        path = arguments["path"]
        content = arguments["content"]

        # Validate and resolve path
        success, error, file_path = self._validate_and_resolve_path(worktree, path)
        if not success:
            return {"success": False, "error": error}

        # Create parent directories if needed
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"success": False, "error": f"Failed to create directory: {str(e)}"}

        # Write file (blocking)
        try:
            file_path.write_text(content, encoding="utf-8")
            return {
                "success": True,
                "path": path,
                "size": len(content),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to write file: {str(e)}"}


class EditFileTool(MCPTool):
    """Tool for editing files in a worktree by replacing line ranges."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize edit file tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="edit_file",
            description="Make targeted edits to a file in a worktree by replacing line ranges. Can edit multiple ranges in one operation. Only works within checked-out worktrees.",
            parameters={
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "Worktree name (must be already created with checkout_commit)",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path relative to worktree root",
                    },
                    "edits": {
                        "type": "array",
                        "description": "List of edits to apply. Applied from bottom to top to maintain line numbers.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_line": {
                                    "type": "integer",
                                    "description": "Starting line number (1-indexed, inclusive)",
                                },
                                "end_line": {
                                    "type": "integer",
                                    "description": "Ending line number (1-indexed, inclusive). Use same as start_line for single line.",
                                },
                                "new_text": {
                                    "type": "string",
                                    "description": "New text to replace the line range. Can be empty to delete lines, can contain newlines for multiple lines.",
                                },
                            },
                            "required": ["start_line", "end_line", "new_text"],
                        },
                    },
                },
                "required": ["worktree", "path", "edits"],
            },
        )
        self.runner = runner

    def _validate_and_resolve_path(self, worktree: str, path: str) -> Tuple[bool, str, Path]:
        """Validate worktree and resolve file path."""
        if not self.runner.worktrees_base_dir:
            return False, "Worktrees base directory not initialized", Path()

        if worktree not in self.runner.created_worktrees:
            return False, f"Worktree '{worktree}' does not exist. Use checkout_commit first.", Path()

        worktree_path = self.runner.worktrees_base_dir / worktree
        if not worktree_path.exists():
            return False, f"Worktree directory does not exist: {worktree}", Path()

        try:
            file_path = (worktree_path / path).resolve()
            worktree_path_resolved = worktree_path.resolve()

            if not str(file_path).startswith(str(worktree_path_resolved)):
                return False, "Path escapes worktree directory", Path()

            return True, "", file_path
        except Exception as e:
            return False, f"Invalid path: {str(e)}", Path()

    def _validate_edits(self, edits: List[Dict[str, Any]], total_lines: int) -> Tuple[bool, str]:
        """
        Validate edits don't overlap and are within file bounds.

        Returns:
            Tuple of (success, error_message)
        """
        if not edits:
            return False, "No edits provided"

        # Validate each edit
        for i, edit in enumerate(edits):
            start = edit["start_line"]
            end = edit["end_line"]

            if start < 1:
                return False, f"Edit {i+1}: start_line must be >= 1"

            if end < start:
                return False, f"Edit {i+1}: end_line ({end}) must be >= start_line ({start})"

            if start > total_lines:
                return False, f"Edit {i+1}: start_line ({start}) exceeds file length ({total_lines} lines)"

            if end > total_lines:
                return False, f"Edit {i+1}: end_line ({end}) exceeds file length ({total_lines} lines)"

        # Check for overlaps
        ranges = [(edit["start_line"], edit["end_line"]) for edit in edits]
        ranges_sorted = sorted(ranges)

        for i in range(len(ranges_sorted) - 1):
            if ranges_sorted[i][1] >= ranges_sorted[i + 1][0]:
                return False, f"Overlapping edits detected: lines {ranges_sorted[i]} and {ranges_sorted[i+1]}"

        return True, ""

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Edit file in worktree by replacing line ranges (blocking)."""
        worktree = arguments["worktree"]
        path = arguments["path"]
        edits = arguments["edits"]

        # Validate and resolve path
        success, error, file_path = self._validate_and_resolve_path(worktree, path)
        if not success:
            return {"success": False, "error": error}

        # Check file exists
        if not file_path.exists():
            return {"success": False, "error": f"File not found: {path}"}

        if not file_path.is_file():
            return {"success": False, "error": f"Path is not a file: {path}"}

        # Read file (blocking)
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
        except Exception as e:
            return {"success": False, "error": f"Failed to read file: {str(e)}"}

        # Validate edits
        valid, error = self._validate_edits(edits, len(lines))
        if not valid:
            return {"success": False, "error": error}

        # Sort edits by start_line in descending order
        sorted_edits = sorted(edits, key=lambda e: e["start_line"], reverse=True)

        # Apply edits
        for edit in sorted_edits:
            start_idx = edit["start_line"] - 1
            end_idx = edit["end_line"]

            new_text = edit["new_text"]
            new_lines = new_text.split("\n") if new_text else []

            lines[start_idx:end_idx] = new_lines

        # Reconstruct content
        new_content = "\n".join(lines)

        # Write back (blocking)
        try:
            file_path.write_text(new_content, encoding="utf-8")
            return {
                "success": True,
                "path": path,
                "edits_applied": len(edits),
                "old_lines": len(content.split("\n")),
                "new_lines": len(lines),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to write file: {str(e)}"}


class GlobTool(MCPTool):
    """Tool for finding files matching a glob pattern in a worktree."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize glob tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="glob",
            description="Find files matching a glob pattern in a worktree. Only works within checked-out worktrees. Examples: '*.py', '**/*.js', 'src/**/*.ts'",
            parameters={
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "Worktree name (must be already created with checkout_commit)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g., '*.py', '**/*.js')",
                    },
                },
                "required": ["worktree", "pattern"],
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Find files matching pattern in worktree (blocking)."""
        worktree = arguments["worktree"]
        pattern = arguments["pattern"]

        if not self.runner.worktrees_base_dir:
            return {"success": False, "error": "Worktrees base directory not initialized"}

        if worktree not in self.runner.created_worktrees:
            return {"success": False, "error": f"Worktree '{worktree}' does not exist. Use checkout_commit first."}

        worktree_path = self.runner.worktrees_base_dir / worktree
        if not worktree_path.exists():
            return {"success": False, "error": f"Worktree directory does not exist: {worktree}"}

        # Find matching files (blocking)
        try:
            matches = []
            worktree_path_resolved = worktree_path.resolve()

            # Use glob with recursive support
            for file_path in worktree_path.glob(pattern):
                # Security check
                if not str(file_path.resolve()).startswith(str(worktree_path_resolved)):
                    continue

                # Get relative path
                rel_path = file_path.relative_to(worktree_path)
                matches.append({
                    "path": str(rel_path),
                    "type": "directory" if file_path.is_dir() else "file",
                })

            return {
                "success": True,
                "matches": matches,
                "count": len(matches),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to search files: {str(e)}"}


class GrepTool(MCPTool):
    """Tool for searching file contents using regex in a worktree."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize grep tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="grep",
            description="Search file contents using regex in a worktree. Only works within checked-out worktrees. Uses ripgrep for fast searching.",
            parameters={
                "type": "object",
                "properties": {
                    "worktree": {
                        "type": "string",
                        "description": "Worktree name (must be already created with checkout_commit)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to limit files searched (e.g., '*.py')",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Case-sensitive search (default: true)",
                        "default": True,
                    },
                },
                "required": ["worktree", "pattern"],
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Search file contents in worktree (blocking)."""
        worktree = arguments["worktree"]
        pattern = arguments["pattern"]
        file_pattern = arguments.get("file_pattern")
        case_sensitive = arguments.get("case_sensitive", True)

        if not self.runner.worktrees_base_dir:
            return {"success": False, "error": "Worktrees base directory not initialized"}

        if worktree not in self.runner.created_worktrees:
            return {"success": False, "error": f"Worktree '{worktree}' does not exist. Use checkout_commit first."}

        worktree_path = self.runner.worktrees_base_dir / worktree
        if not worktree_path.exists():
            return {"success": False, "error": f"Worktree directory does not exist: {worktree}"}

        # Build ripgrep command
        try:
            cmd = ["rg", "--json", pattern]

            if not case_sensitive:
                cmd.append("-i")

            if file_pattern:
                cmd.extend(["-g", file_pattern])

            # Run ripgrep (blocking)
            result = subprocess.run(
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )

            # Parse JSON output
            matches = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "match":
                        match_data = data["data"]
                        matches.append({
                            "file": match_data["path"]["text"],
                            "line_number": match_data["line_number"],
                            "line": match_data["lines"]["text"].rstrip(),
                        })
                except Exception:
                    continue

            return {
                "success": True,
                "matches": matches,
                "count": len(matches),
            }

        except FileNotFoundError:
            # ripgrep not installed, fallback to Python regex
            return self._fallback_grep(worktree_path, pattern, file_pattern, case_sensitive)
        except Exception as e:
            return {"success": False, "error": f"Failed to search: {str(e)}"}

    def _fallback_grep(
        self,
        worktree_path: Path,
        pattern: str,
        file_pattern: Optional[str],
        case_sensitive: bool,
    ) -> Dict[str, Any]:
        """Fallback grep using Python when ripgrep is not available."""
        try:
            import re as regex_module

            flags = 0 if case_sensitive else regex_module.IGNORECASE
            compiled_pattern = regex_module.compile(pattern, flags)

            matches = []
            glob_pattern = file_pattern if file_pattern else "**/*"

            for file_path in worktree_path.glob(glob_pattern):
                if not file_path.is_file():
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    for line_num, line in enumerate(content.split("\n"), 1):
                        if compiled_pattern.search(line):
                            matches.append({
                                "file": str(file_path.relative_to(worktree_path)),
                                "line_number": line_num,
                                "line": line.rstrip(),
                            })
                except Exception:
                    continue

            return {
                "success": True,
                "matches": matches,
                "count": len(matches),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to search: {str(e)}"}


# Project-level tools (work on /project directory, used before worktrees exist)


class SpawnAgentTool(MCPTool):
    """Tool for spawning a sub-agent in the background to handle a specific task."""

    def __init__(self, runner: "SandboxRunner", inheritable: bool = True):
        """
        Initialize spawn agent tool.

        Args:
            runner: SandboxRunner instance
            inheritable: Whether this tool can be inherited by spawned child agents (default: True)
        """
        super().__init__(
            name="spawn_agent",
            description="Spawn a sub-agent in the background to handle a specific task. The spawned agent will inherit tools from the parent agent. Returns immediately with an agent_id. Use wait_for_agents to retrieve the result later. This allows parallel execution of multiple independent tasks.",
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear description of the task for the sub-agent to complete",
                    },
                    "output_schema": {
                        "type": "object",
                        "description": "JSON schema defining the expected output structure from the sub-agent",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional identifier for this agent (auto-generated if not provided)",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of tool names to provide to the sub-agent. Must be a subset of tools available to the parent agent. If not specified, the sub-agent inherits all parent tools.",
                    },
                },
                "required": ["task", "output_schema"],
            },
            inheritable=inheritable,
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Spawn sub-agent in background."""
        task = arguments["task"]
        output_schema = arguments["output_schema"]
        agent_id = arguments.get("agent_id", str(uuid.uuid4())[:8])
        requested_tools = arguments.get("tools")

        try:
            if agent is None:
                # Fallback: no parent agent available, return error
                return {
                    "success": False,
                    "error": "Cannot spawn agent: parent agent not available",
                }

            # Get parent's tools from agent's MCP server
            parent_tools = agent.mcp_server.tools

            # Determine which tools to give the child agent
            if requested_tools is not None:
                # Validate that requested tools are a subset of parent tools
                invalid_tools = [t for t in requested_tools if t not in parent_tools]
                if invalid_tools:
                    return {
                        "success": False,
                        "error": f"Invalid tools requested: {invalid_tools}. Must be a subset of parent tools: {list(parent_tools.keys())}",
                    }

                # Validate that requested tools are inheritable
                non_inheritable = [name for name in requested_tools if not parent_tools[name].inheritable]
                if non_inheritable:
                    return {
                        "success": False,
                        "error": f"Tools are not inheritable: {non_inheritable}. Cannot pass non-inheritable tools to child agents.",
                    }

                # Add only the requested tools
                tools_to_add = [parent_tools[name] for name in requested_tools]
            else:
                # Inherit all inheritable parent tools
                tools_to_add = [tool for tool in parent_tools.values() if tool.inheritable]

            # Create MCP server with inherited tools from parent
            child_mcp_server = MCPServer()
            child_mcp_server.add_tools(tools_to_add)

            # Create and spawn agent in background
            from llm_sandbox import Agent

            child_agent = Agent(
                runner=self.runner,
                prompt=task,
                output_schema=output_schema,
                mcp_server=child_mcp_server,
                agent_id=agent_id,
                parent=agent
            )
            spawned_agent_id = await child_agent.execute()

            # Get list of tool names provided to child
            child_tool_names = list(child_mcp_server.tools.keys())

            return {
                "success": True,
                "agent_id": spawned_agent_id,
                "status": "spawned",
                "spawn_depth": child_agent.spawn_depth,
                "tools": child_tool_names,
                "message": f"Agent '{spawned_agent_id}' spawned in background at depth {child_agent.spawn_depth} with {len(child_tool_names)} tools. Use wait_for_agents to retrieve result.",
            }

        except ValueError as e:
            # Agent already exists
            return {
                "success": False,
                "error": str(e),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to spawn sub-agent: {str(e)}",
            }


class WaitForAgentsTool(MCPTool):
    """Tool for waiting for background agents to complete and retrieving their results."""

    def __init__(self, runner: "SandboxRunner", inheritable: bool = True):
        """
        Initialize wait for agents tool.

        Args:
            runner: SandboxRunner instance
            inheritable: Whether this tool can be inherited by spawned child agents (default: True)
        """
        super().__init__(
            name="wait_for_agents",
            description="Wait for one or more background agents (that you spawned) to complete and retrieve their results. Only allows waiting for direct children to prevent circular dependencies.",
            parameters={
                "type": "object",
                "properties": {
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of agent IDs to wait for (required - must be agents you spawned)",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (optional, default 300s)",
                    },
                },
                "required": ["agent_ids"],
            },
            inheritable=inheritable,
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], agent: Optional["Agent"]) -> Dict[str, Any]:
        """Wait for background agents to complete."""
        agent_ids = arguments.get("agent_ids")
        timeout = arguments.get("timeout", 300.0)  # Default 5 minute timeout

        try:
            # Require agent_ids
            if not agent_ids:
                return {
                    "success": False,
                    "error": "agent_ids is required - specify which agents to wait for",
                }

            # Require calling agent
            if not agent:
                return {
                    "success": False,
                    "error": "No calling agent context available",
                }

            # Look up agents by ID
            agents_to_wait = []
            for agent_id in agent_ids:
                child_agent = self.runner._agents.get(agent_id)
                if child_agent:
                    agents_to_wait.append(child_agent)

            if not agents_to_wait:
                return {
                    "success": False,
                    "error": f"No agents found with IDs: {agent_ids}",
                }

            # Wait for agents (validates parent-child relationship inside)
            results = await agent.wait_for_agents(
                agents=agents_to_wait,
                timeout=timeout
            )

            # Process results to handle exceptions
            processed_results = {}
            for agent_id, result in results.items():
                if isinstance(result, Exception):
                    processed_results[agent_id] = {
                        "success": False,
                        "error": str(result),
                    }
                else:
                    processed_results[agent_id] = result

            return {
                "success": True,
                "results": processed_results,
                "completed_count": len(processed_results),
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Timeout after {timeout} seconds waiting for agents: {agent_ids}",
                "remaining_agents": self.runner.get_running_agents(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to wait for agents: {str(e)}",
            }



