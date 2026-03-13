"""Model Context Protocol (MCP) tools - base classes and definitions."""

import glob
import json
import re
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
        self.instance_id = instance_id
        self.runner = runner

    def _validate_branch_pattern(self, branch: str) -> bool:
        """Validate branch matches required pattern."""
        pattern = rf"^llm-container/{re.escape(self.instance_id)}/[a-zA-Z0-9_/-]+$"
        return bool(re.match(pattern, branch))

    def _derive_worktree_path(self, branch: str) -> str:
        """Derive worktree path from branch name."""
        # Extract worktree name from branch: llm-container/{instance_id}/{worktree_name}
        # worktree_name can contain slashes (e.g., foo/bar)
        prefix = f"llm-container/{self.instance_id}/"
        if branch.startswith(prefix):
            worktree_name = branch[len(prefix):]
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
        # worktree_name can contain slashes (e.g., foo/bar)
        prefix = f"llm-container/{self.instance_id}/"
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
        self.instance_id = instance_id
        self.runner = runner

    def _validate_worktree_name(self, name: str) -> bool:
        """Validate worktree name matches allowed pattern."""
        return bool(re.match(r"^[a-zA-Z0-9_/-]+$", name))

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
                "error": f"Invalid worktree name: {worktree_name}. Must match [a-zA-Z0-9_/-]+ (can contain slashes for hierarchy)",
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


class ReadFileTool(MCPTool):
    """Tool for reading files from a worktree."""

    def __init__(
        self,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize read file tool.

        Args:
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for worktree access
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
        self.instance_id = instance_id
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

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Read file from worktree."""
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

        # Read file
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

    def __init__(
        self,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize write file tool.

        Args:
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for worktree access
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
        self.instance_id = instance_id
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

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Write file to worktree."""
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

        # Write file
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

    def __init__(
        self,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize edit file tool.

        Args:
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for worktree access
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
        self.instance_id = instance_id
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

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Edit file in worktree by replacing line ranges."""
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

        # Read file
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
        except Exception as e:
            return {"success": False, "error": f"Failed to read file: {str(e)}"}

        # Validate edits
        valid, error = self._validate_edits(edits, len(lines))
        if not valid:
            return {"success": False, "error": error}

        # Sort edits by start_line in descending order (apply from bottom to top)
        # This ensures line numbers remain valid as we edit
        sorted_edits = sorted(edits, key=lambda e: e["start_line"], reverse=True)

        # Apply edits
        for edit in sorted_edits:
            start_idx = edit["start_line"] - 1  # Convert to 0-indexed
            end_idx = edit["end_line"]  # This is exclusive for slicing

            new_text = edit["new_text"]
            new_lines = new_text.split("\n") if new_text else []

            # Replace the line range with new lines
            lines[start_idx:end_idx] = new_lines

        # Reconstruct content
        new_content = "\n".join(lines)

        # Write back
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

    def __init__(
        self,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize glob tool.

        Args:
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for worktree access
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
        self.instance_id = instance_id
        self.runner = runner

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Find files matching pattern in worktree."""
        worktree = arguments["worktree"]
        pattern = arguments["pattern"]

        if not self.runner.worktrees_base_dir:
            return {"success": False, "error": "Worktrees base directory not initialized"}

        if worktree not in self.runner.created_worktrees:
            return {"success": False, "error": f"Worktree '{worktree}' does not exist. Use checkout_commit first."}

        worktree_path = self.runner.worktrees_base_dir / worktree
        if not worktree_path.exists():
            return {"success": False, "error": f"Worktree directory does not exist: {worktree}"}

        # Find matching files
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

    def __init__(
        self,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize grep tool.

        Args:
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for worktree access
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
        self.instance_id = instance_id
        self.runner = runner

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Search file contents in worktree."""
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

            # Run ripgrep
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


class ReadProjectFileTool(MCPTool):
    """Tool for reading files from the read-only project directory."""

    def __init__(self, project_path: Path):
        """
        Initialize read project file tool.

        Args:
            project_path: Path to project directory
        """
        super().__init__(
            name="read_project_file",
            description="Read contents of a file from the read-only /project directory. Use this to explore the original project before creating worktrees.",
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
            # Security: exclude .llm-sandbox directory
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


class ListProjectDirectoryTool(MCPTool):
    """Tool for listing directory contents in the project."""

    def __init__(self, project_path: Path):
        """
        Initialize list project directory tool.

        Args:
            project_path: Path to project directory
        """
        super().__init__(
            name="list_project_directory",
            description="List files and directories in the read-only /project directory. Use this to explore the project structure before creating worktrees.",
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
            # Security: exclude .llm-sandbox directory
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


