"""CLI entry point for LLM Sandbox."""

from pathlib import Path

import click

from llm_sandbox.subcommand import discover_subcommands


@click.group()
def cli():
    """LLM Container Sandbox - Safe isolated execution environment for LLM code analysis."""
    pass


def make_subcommand_callback(subcommand_instance):
    """
    Create a callback function for a subcommand.

    Args:
        subcommand_instance: Instance of Subcommand

    Returns:
        Click callback function
    """
    def callback(project_dir, network, verbose, **kwargs):
        # Execute the subcommand (subcommand creates its own runner)
        try:
            subcommand_instance.execute(
                project_dir=project_dir,
                network=network,
                verbose=verbose,
                **kwargs
            )
        except Exception as e:
            output = create_output_service(format="text", verbose=False)
            output.error(str(e))
            sys.exit(1)

    return callback


def register_subcommand_class(subcommand_class):
    """
    Register a single subcommand class.

    Args:
        subcommand_class: Subcommand class to register
    """
    # Create an instance
    subcommand_instance = subcommand_class()
    name = subcommand_class.name

    # Create a click command for this subcommand
    @click.command(name=name, help=subcommand_class.help or f"Subcommand: {name}")
    @click.option(
        "--project-dir",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
        default=Path.cwd(),
        help="Project directory (defaults to current directory)",
    )
    @click.option(
        "--network",
        type=click.Choice(["isolated", "enabled"]),
        help="Network access mode (default: from config)",
    )
    @click.option(
        "--verbose",
        is_flag=True,
        help="Enable verbose output (show tool usage and LLM messages)",
    )
    def subcommand_wrapper(**kwargs):
        pass

    # Set the callback
    subcommand_wrapper.callback = make_subcommand_callback(subcommand_instance)

    # Let the subcommand add its custom arguments or return a Group
    result = subcommand_instance.add_arguments(subcommand_wrapper)

    # Check if the result is a Group (for nested subcommands)
    if isinstance(result, click.Group):
        # It's a group - inject common options into all subcommands
        group = result

        # Store original params from wrapper (project-dir, network, verbose)
        common_params = subcommand_wrapper.params

        # Add common options to all commands in the group
        for cmd_name, cmd in group.commands.items():
            # Prepend common params so they appear first
            cmd.params = common_params + cmd.params

        # Register the group
        cli.add_command(group)
    else:
        # It's a regular command
        cli.add_command(result)


def register_all_subcommands():
    """
    Discover and register all subcommands (built-in and custom).
    """
    subcommands = discover_subcommands(Path.cwd())

    for name, subcommand_class in subcommands.items():
        register_subcommand_class(subcommand_class)


register_all_subcommands()


if __name__ == "__main__":
    cli()
