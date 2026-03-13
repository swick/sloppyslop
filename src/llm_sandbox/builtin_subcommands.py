"""Built-in subcommands for llm-sandbox."""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from llm_sandbox.subcommand import Subcommand


class RunSubcommand(Subcommand):
    """Run one-shot LLM prompt in isolated container."""

    name = "run"
    help = "Run one-shot LLM prompt in isolated container"

    def add_arguments(self, command: click.Command) -> click.Command:
        """Add arguments for run command."""
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

    def execute(self, project_dir: Path, run_sandbox, **kwargs):
        """
        Execute the run command.

        Note: commit, network, and keep_branch are provided as kwargs
        but are already configured in run_sandbox function.
        """
        prompt = kwargs.get("prompt")
        prompt_file = kwargs.get("prompt_file")
        schema = kwargs.get("schema")
        schema_file = kwargs.get("schema_file")

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

        # Run the sandbox (commit, network, and keep_branch already configured)
        result = run_sandbox(
            prompt=prompt,
            output_schema=output_schema,
        )

        # Output result as JSON
        click.echo("\n" + "=" * 60)
        click.echo("Result:")
        click.echo("=" * 60)
        click.echo(json.dumps(result, indent=2))
