"""Project analysis for Containerfile search and generation."""

from pathlib import Path
from typing import List, Optional

from llm_sandbox.image import Image
from llm_sandbox.llm_provider import LLMProvider
from llm_sandbox.mcp_tools import MCPServer, ReadFileTool, ListDirectoryTool


class AnalyzerMCPServer(MCPServer):
    """MCP server for local file operations during project analysis."""

    def __init__(self, project_path: Path):
        """
        Initialize local MCP server.

        Args:
            project_path: Path to project directory
        """
        super().__init__()
        read_file_tool = ReadFileTool(project_path)
        list_directory_tool = ListDirectoryTool(project_path)
        self.tools = {
            read_file_tool.name: read_file_tool,
            list_directory_tool.name: list_directory_tool,
        }


class ProjectAnalyzer:
    """Analyzes projects and manages Containerfiles."""

    def __init__(self, llm_provider: Optional[LLMProvider] = None):
        """
        Initialize project analyzer.

        Args:
            llm_provider: LLM provider instance for generation (optional, required only for generate_containerfile)
        """
        self.llm_provider = llm_provider

    def search_containerfiles(self, project_path: Path) -> List[Path]:
        """
        Search for existing Dockerfile/Containerfile in project.

        Args:
            project_path: Path to project directory

        Returns:
            List of found containerfile paths
        """
        containerfiles = []
        search_names = [
            "Containerfile",
            "Dockerfile",
            "containerfile",
            "dockerfile",
        ]

        # Search in root
        for name in search_names:
            path = project_path / name
            if path.is_file():
                containerfiles.append(path)

        # Search in common subdirectories
        search_dirs = [
            project_path / "docker",
            project_path / "container",
            project_path / ".devcontainer",
            project_path / "build",
        ]

        for search_dir in search_dirs:
            if not search_dir.is_dir():
                continue

            for name in search_names:
                path = search_dir / name
                if path.is_file():
                    containerfiles.append(path)

        return containerfiles

    def generate_containerfile(
        self,
        project_path: Path,
        image_name: str,
        extra_instructions: Optional[str] = None,
    ) -> str:
        """
        Generate Containerfile using LLM based on project analysis.

        The LLM will explore the project using tools to read files and
        understand the project structure before generating the Containerfile.

        Args:
            project_path: Path to project directory
            image_name: Name for the container environment
            extra_instructions: Optional additional requirements/instructions

        Returns:
            Generated Containerfile content

        Raises:
            ValueError: If llm_provider is not configured
        """
        if self.llm_provider is None:
            raise ValueError("LLM provider is required for Containerfile generation")

        # Create local MCP server for file exploration
        mcp_server = AnalyzerMCPServer(project_path)

        # Define output schema for Containerfile generation
        output_schema = {
            "type": "object",
            "properties": {
                "containerfile": {
                    "type": "string",
                    "description": "The complete Containerfile content",
                },
                "explanation": {
                    "type": "string",
                    "description": "Brief explanation of the Containerfile choices",
                },
            },
            "required": ["containerfile", "explanation"],
        }

        # Build the generation prompt
        prompt = f"""Analyze the project in the current directory and generate a Containerfile for an environment named '{image_name}'.

Project path: {project_path}

The Containerfile should:
1. Use an appropriate base image (suggested: {Image.DEFAULT_IMAGE})
2. Install necessary dependencies for the project
3. Set up the working environment
4. Be suitable for running code analysis and development tasks"""

        if extra_instructions:
            prompt += f"\n\nAdditional requirements:\n{extra_instructions}"

        prompt += """

Use the available tools to:
1. List the directory structure
2. Read key files (package.json, requirements.txt, pyproject.toml, go.mod, etc.)
3. Understand the project type and dependencies

Containerfile requirements:
- The resulting Containerfile will be used by an LLM to inspect, modify, build, run and test the project
- Install all necessary dependencies
- Set up the working directory as /workspace
- Do not include CMD or ENTRYPOINT (container will be used interactively)
- Do not expose ports
- Use best practices for layer caching

Explore the project thoroughly before generating the Containerfile."""

        result = self.llm_provider.generate_structured(
            prompt,
            mcp_server,
            output_schema,
            max_iterations=15,
        )

        return result["containerfile"]
