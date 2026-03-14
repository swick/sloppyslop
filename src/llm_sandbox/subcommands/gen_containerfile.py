"""Generate Containerfile subcommand."""

from pathlib import Path

import click

from llm_sandbox.image import Image
from llm_sandbox.mcp_tools import (
    MCPServer,
    ReadProjectFileTool,
    ListProjectDirectoryTool,
)
from llm_sandbox.subcommand import Subcommand


class GenContainerfileMCPServer(MCPServer):
    """MCP server for Containerfile generation with project exploration tools."""

    def __init__(self, runner):
        """
        Initialize Containerfile generation MCP server.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__()
        # Only need read-only project exploration tools
        self.add_tool(ReadProjectFileTool(runner))
        self.add_tool(ListProjectDirectoryTool(runner))


class GenContainerfileSubcommand(Subcommand):
    """Generate a Containerfile for the project."""

    name = "gen-containerfile"
    help = "Generate a Containerfile based on project dependencies"

    def add_arguments(self, command):
        """Add custom arguments."""
        command.params.append(
            click.Option(
                ["--output", "-o"],
                type=click.Path(path_type=Path),
                default=Path("Containerfile"),
                help="Output path for the Containerfile (default: Containerfile)",
            )
        )
        command.params.append(
            click.Option(
                ["--prompt"],
                type=str,
                help="Additional instructions for Containerfile generation",
            )
        )
        return command

    def execute(self, project_dir: Path, runner, **kwargs):
        """Execute Containerfile generation."""
        output_path = kwargs["output"]
        extra_prompt = kwargs.get("prompt")
        verbose = kwargs["verbose"]

        click.echo(f"Generating Containerfile")
        click.echo(f"Project directory: {project_dir}")
        click.echo(f"Output: {output_path}")

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
        prompt = f"""Analyze the project in /project and generate a Containerfile.

The Containerfile should:
1. Use an appropriate base image (suggested: {Image.DEFAULT_IMAGE})
2. Install necessary dependencies for the project
3. Set up the working environment
4. Be suitable for running code analysis and development tasks"""

        if extra_prompt:
            prompt += f"\n\nAdditional requirements:\n{extra_prompt}"

        prompt += """

Use the available tools to:
1. List the directory structure (use list_project_directory)
2. Read key files (package.json, requirements.txt, pyproject.toml, go.mod, etc.) using read_project_file
3. Understand the project type and dependencies

Containerfile requirements:
- The resulting Containerfile will be used by an LLM to inspect, modify, build, run and test the project
- Install all necessary dependencies
- Set up the working directory as /workspace
- Do not include CMD or ENTRYPOINT (container will be used interactively)
- Do not expose ports
- Use best practices for layer caching

Explore the project thoroughly before generating the Containerfile."""

        # Run in container with default image
        try:
            runner.setup(network="enabled", image=Image.DEFAULT_IMAGE)

            # Create MCP server with project exploration tools
            mcp_server = GenContainerfileMCPServer(runner)

            click.echo("\nGenerating Containerfile with LLM...")
            result = runner.run_agent(
                prompt=prompt,
                output_schema=output_schema,
                mcp_server=mcp_server,
            )

            containerfile_content = result["containerfile"]
            explanation = result.get("explanation", "")

        finally:
            runner.cleanup()

        # Show generated content
        click.echo("\n" + "=" * 60)
        click.echo("Generated Containerfile:")
        click.echo("=" * 60)
        click.echo(containerfile_content)
        click.echo("=" * 60)

        if explanation:
            click.echo("\nExplanation:")
            click.echo(explanation)

        # Save to output path
        output_path.write_text(containerfile_content)
        click.echo(f"\n✓ Saved to: {output_path}")
