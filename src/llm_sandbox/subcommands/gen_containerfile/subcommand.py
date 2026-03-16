"""Generate Containerfile subcommand."""

import asyncio
from pathlib import Path

import click

from llm_sandbox import AgentConfig
from llm_sandbox.config import load_config
from llm_sandbox.container import Image
from llm_sandbox.event_handlers import wire_up_all_events
from llm_sandbox.output import create_output_service
from llm_sandbox.runner import SandboxRunner
from llm_sandbox.mcp_tools import (
    MCPServer,
    CheckoutCommitTool,
    ReadFileTool,
    GlobTool,
    GrepTool,
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
        # Tools for exploring project via worktree
        self.add_tool(CheckoutCommitTool(runner))
        self.add_tool(ReadFileTool(runner))
        self.add_tool(GlobTool(runner))
        self.add_tool(GrepTool(runner))


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

    def execute(self, project_dir: Path, **kwargs):
        """Execute Containerfile generation."""
        output_path = kwargs["output"]
        extra_prompt = kwargs.get("prompt")
        verbose = kwargs["verbose"]

        # Create output service
        output = create_output_service(format="text", verbose=verbose)

        # Load config and create runner
        config = load_config(project_dir)
        runner = SandboxRunner(project_dir, config)

        # Wire up all event handlers
        wire_up_all_events(runner, output)

        output.info(f"Generating Containerfile")
        output.info(f"Project directory: {project_dir}")
        output.info(f"Output: {output_path}")

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
        prompt = f"""Analyze the project and generate a Containerfile.

The Containerfile should:
1. Use an appropriate base image (suggested: {Image.DEFAULT_IMAGE})
2. Install necessary dependencies for the project
3. Set up the working environment
4. Be suitable for running code analysis and development tasks"""

        if extra_prompt:
            prompt += f"\n\nAdditional requirements:\n{extra_prompt}"

        prompt += """

Use the available tools to explore the project:
1. First, use checkout_commit to create a worktree from HEAD (e.g., worktree_name: "analysis")
2. Use glob to find key files (package.json, requirements.txt, pyproject.toml, go.mod, etc.)
3. Use read_file to examine these files
4. Use grep to search for specific patterns if needed
5. Understand the project type and dependencies

Containerfile requirements:
- The resulting Containerfile will be used by an LLM to inspect, modify, build, run and test the project
- Install all necessary dependencies
- Set up the working directory as /workspace
- Do not include CMD or ENTRYPOINT (container will be used interactively)
- Do not expose ports
- Use best practices for layer caching

Explore the project thoroughly before generating the Containerfile."""

        # Run using async context manager pattern
        result = asyncio.run(self._execute_async(
            runner,
            prompt,
            output_schema,
            verbose,
            output
        ))

        containerfile_content = result["containerfile"]
        explanation = result.get("explanation", "")

        # Show generated content
        output.info("\n" + "=" * 60)
        output.info("Generated Containerfile:")
        output.info("=" * 60)
        output.info(containerfile_content)
        output.info("=" * 60)

        if explanation:
            output.info("\nExplanation:")
            output.info(explanation)

        # Save to output path
        output_path.write_text(containerfile_content)
        output.success(f"Saved to: {output_path}")

    async def _execute_async(self, runner, prompt, output_schema, verbose, output):
        """Async execution of Containerfile generation."""
        async with runner:
            await runner.setup(network="enabled", image=Image.DEFAULT_IMAGE)

            # Create MCP server with project exploration tools
            mcp_server = GenContainerfileMCPServer(runner)

            output.info("\nGenerating Containerfile with LLM...")

            # Create agent config and run
            agent = AgentConfig(
                prompt=prompt,
                output_schema=output_schema,
                mcp_server=mcp_server,
            )
            results = await runner.run_agents([agent], verbose=verbose)
            return results[0]
