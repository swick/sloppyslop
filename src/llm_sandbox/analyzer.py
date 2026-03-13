"""Project analysis for Containerfile search and generation."""

from pathlib import Path
from typing import List, Optional

from llm_sandbox.llm_provider import LLMProvider
from llm_sandbox.local_tools import LocalToolServer


class ProjectAnalyzer:
    """Analyzes projects and manages Containerfiles."""

    def __init__(self, llm_provider: LLMProvider):
        """
        Initialize project analyzer.

        Args:
            llm_provider: LLM provider instance for generation
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

    def generate_containerfile(self, project_path: Path) -> str:
        """
        Generate Containerfile using LLM based on project analysis.

        The LLM will explore the project using tools to read files and
        understand the project structure before generating the Containerfile.

        Args:
            project_path: Path to project directory

        Returns:
            Generated Containerfile content
        """
        # Create local tool server for file exploration
        tool_server = LocalToolServer(project_path)

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

        # Generate containerfile using structured output
        prompt = f"""Analyze the project in the current directory and generate an appropriate Containerfile.

Project path: {project_path}

Use the available tools to:
1. List the directory structure
2. Read key files (package.json, requirements.txt, pyproject.toml, go.mod, etc.)
3. Understand the project type and dependencies

Then generate a Containerfile with these requirements:
- Use an appropriate base image for the project type
- Install all necessary dependencies
- Set up the working directory as /workspace
- Do not include CMD or ENTRYPOINT (container will be used interactively)
- Keep it minimal and focused on build dependencies
- Do not expose ports unless absolutely necessary
- Use best practices for layer caching

Explore the project thoroughly before generating the Containerfile."""

        result = self.llm_provider.generate_structured(
            prompt,
            tool_server,
            output_schema,
            max_iterations=15,
        )

        return result["containerfile"]
