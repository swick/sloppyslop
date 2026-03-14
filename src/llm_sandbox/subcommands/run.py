"""Run subcommand for llm-sandbox."""

import json
import sys
from pathlib import Path

import click

from llm_sandbox.subcommand import Subcommand
from llm_sandbox.mcp_tools import (
    MCPServer,
    ExecuteCommandTool,
    CheckoutCommitTool,
    GitCommitTool,
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadProjectFileTool,
    ListProjectDirectoryTool,
)


class RunMCPServer(MCPServer):
    """MCP server with all built-in tools for run subcommand."""

    def __init__(self, container_manager, container_id, instance_id, runner, project_path):
        """
        Initialize run MCP server with all built-in tools.

        Args:
            container_manager: Container manager instance
            container_id: Container ID
            instance_id: Instance ID
            runner: SandboxRunner instance
            project_path: Path to project directory
        """
        super().__init__()
        # Add all built-in tools
        self.add_tool(ExecuteCommandTool(container_manager, container_id))
        self.add_tool(CheckoutCommitTool(container_manager, container_id, instance_id, runner))
        self.add_tool(GitCommitTool(container_manager, container_id, instance_id, runner))
        self.add_tool(ReadFileTool(instance_id, runner))
        self.add_tool(WriteFileTool(instance_id, runner))
        self.add_tool(EditFileTool(instance_id, runner))
        self.add_tool(GlobTool(instance_id, runner))
        self.add_tool(GrepTool(instance_id, runner))
        self.add_tool(ReadProjectFileTool(project_path))
        self.add_tool(ListProjectDirectoryTool(project_path))


class RunSubcommand(Subcommand):
    """Run one-shot LLM prompt in isolated container."""

    name = "run"
    help = "Run one-shot LLM prompt in isolated container"

    def add_arguments(self, command: click.Command) -> click.Command:
        """Add arguments for run command."""
        command.params.append(
            click.Option(
                ["--keep-branch"],
                multiple=True,
                help="Branch name to keep as output (can be specified multiple times). Branch will be renamed from llm-container/{instance_id}/{name} to {name}",
            )
        )
        command.params.append(
            click.Option(
                ["--prompt"],
                type=str,
                help="Prompt text (use --prompt-file for file input)",
            )
        )
        command.params.append(
            click.Option(
                ["--prompt-file"],
                type=click.Path(exists=True, dir_okay=False, path_type=Path),
                help="File containing the prompt",
            )
        )
        command.params.append(
            click.Option(
                ["--schema"],
                type=str,
                help="JSON schema string for structured output",
            )
        )
        command.params.append(
            click.Option(
                ["--schema-file"],
                type=click.Path(exists=True, dir_okay=False, path_type=Path),
                help="JSON schema file for structured output",
            )
        )
        return command

    def execute(self, project_dir: Path, runner, **kwargs):
        """
        Execute the run command.
        """
        keep_branch = kwargs.get("keep_branch", ())
        prompt = kwargs.get("prompt")
        prompt_file = kwargs.get("prompt_file")
        schema = kwargs.get("schema")
        schema_file = kwargs.get("schema_file")
        network = kwargs["network"]
        verbose = kwargs["verbose"]

        # Validate prompt input
        if not prompt and not prompt_file:
            click.echo("Error: Either --prompt or --prompt-file must be provided", err=True)
            sys.exit(1)

        if prompt and prompt_file:
            click.echo("Error: Cannot use both --prompt and --prompt-file", err=True)
            sys.exit(1)

        # Load prompt from file if specified
        if prompt_file:
            prompt = prompt_file.read_text()

        # Validate schema input
        if not schema and not schema_file:
            click.echo("Error: Either --schema or --schema-file must be provided", err=True)
            sys.exit(1)

        if schema and schema_file:
            click.echo("Error: Cannot use both --schema and --schema-file", err=True)
            sys.exit(1)

        # Load output schema
        if schema:
            try:
                output_schema = json.loads(schema)
            except json.JSONDecodeError as e:
                click.echo(f"Error: Invalid JSON schema: {e}", err=True)
                sys.exit(1)
        else:
            with open(schema_file) as f:
                output_schema = json.load(f)

        # Run the sandbox
        try:
            runner.setup(
                keep_branches=list(keep_branch) if keep_branch else [],
                network=network,
            )
            # Create MCP server with all built-in tools
            mcp_server = RunMCPServer(
                runner.container_manager,
                runner.container_id,
                runner.instance_id,
                runner,
                project_dir,
            )
            result = runner.run_prompt(
                prompt=prompt,
                output_schema=output_schema,
                mcp_server=mcp_server,
                verbose=verbose,
            )
        finally:
            runner.cleanup()

        # Output result as JSON
        click.echo("\n" + "=" * 60)
        click.echo("Result:")
        click.echo("=" * 60)
        click.echo(json.dumps(result, indent=2))
