"""Local tools for project analysis (not in container)."""

from pathlib import Path
from typing import Any, Dict, List

from llm_sandbox.mcp_server import MCPTool


class LocalToolServer:
    """Tool server for local file operations during project analysis."""

    def __init__(self, project_path: Path):
        """
        Initialize local tool server.

        Args:
            project_path: Path to project directory
        """
        self.project_path = project_path

    def get_tools(self) -> List[MCPTool]:
        """
        Get list of available local tools.

        Returns:
            List of MCPTool instances
        """
        return [
            MCPTool(
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
            ),
            MCPTool(
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
            ),
        ]

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a local tool.

        Args:
            tool_name: Name of tool to execute
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        if tool_name == "read_file":
            return self._read_file(
                arguments["path"],
                arguments.get("max_lines", 1000),
            )
        elif tool_name == "list_directory":
            return self._list_directory(
                arguments.get("path", "."),
            )
        else:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
            }

    def _read_file(self, path: str, max_lines: int) -> Dict[str, Any]:
        """Read a file from the project directory."""
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

    def _list_directory(self, path: str) -> Dict[str, Any]:
        """List contents of a directory."""
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
