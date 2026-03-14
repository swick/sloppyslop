"""CLI entry point for LLM Sandbox."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import click

from llm_sandbox.subcommands import RunSubcommand, GenContainerfileSubcommand, PRReviewSubcommand
from llm_sandbox.config import (
    AnthropicConfig,
    Config,
    VertexAIConfig,
    get_provider_config,
    load_config,
)
from llm_sandbox.container import ContainerManager
from llm_sandbox.image import Image
from llm_sandbox.llm_provider import create_llm_provider
from llm_sandbox.runner import SandboxRunner
from llm_sandbox.subcommand import discover_subcommands


@click.group()
def cli():
    """LLM Container Sandbox - Safe isolated execution environment for LLM code analysis."""
    pass


@cli.command()
@click.option(
    "--provider",
    type=str,
    help="Provider to test (defaults to default_provider from config)",
)
def check(provider: Optional[str]):
    """Check LLM provider configuration and connectivity."""
    import sys

    click.echo("Checking LLM provider configuration...\n")

    # Load config (merged global + project)
    config = load_config(Path.cwd())

    try:
        # Get provider config
        provider_name, provider_config = get_provider_config(config, provider)

        click.echo(f"Provider: {provider_name}")
        click.echo(f"Model: {provider_config.model}")

        if isinstance(provider_config, VertexAIConfig):
            click.echo(f"Region: {provider_config.region}")
            click.echo(f"Project ID: {provider_config.project_id}")
        elif isinstance(provider_config, AnthropicConfig):
            click.echo(f"API Key Env: {provider_config.api_key_env}")
        else:
            raise ValueError(f"Unknown provider config type: {type(provider_config)}")

        click.echo("\nValidating provider...")

        # Create provider with simple system prompt for validation
        llm_provider = create_llm_provider(
            provider_name,
            provider_config,
            base_system_prompt="You are a helpful AI assistant.",
        )

        # Validate
        result = asyncio.run(llm_provider.validate())

        if result["success"]:
            click.echo(f"✓ {result['message']}")
            if "details" in result and "response_id" in result["details"]:
                click.echo(f"  Response ID: {result['details']['response_id']}")
            sys.exit(0)
        else:
            click.echo(f"✗ {result['message']}", err=True)
            if "details" in result:
                details = result["details"]
                if "error_type" in details:
                    click.echo(f"  Error Type: {details['error_type']}", err=True)
                if "error_message" in details:
                    click.echo(f"  Error: {details['error_message']}", err=True)
                if "guidance" in details:
                    click.echo(f"  Suggestion: {details['guidance']}", err=True)
            sys.exit(1)

    except ValueError as e:
        click.echo(f"✗ Configuration error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"✗ Unexpected error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    help="Force rebuild even if image is up-to-date",
)
def build(force: bool):
    """Build the container image from Containerfile."""
    project_dir = Path.cwd()

    click.echo(f"Building container image")
    click.echo(f"Project directory: {project_dir}")

    # Load config
    config = load_config(project_dir)

    # Create container manager and image manager
    container_manager = ContainerManager()
    image_manager = Image(config.image, project_dir, container_manager)

    try:
        # Build image
        image_tag = image_manager.build(force=force)

        click.echo(f"\n✓ Successfully built image: {image_tag}")
        click.echo(f"\nThe image will be used on the next run.")

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
def cleanup():
    """Clean up all llm-sandbox worktrees and llm-container branches."""
    import shutil
    from llm_sandbox.git_ops import GitOperations

    project_dir = Path.cwd()

    click.echo("Cleaning up llm-sandbox worktrees and branches")
    click.echo(f"Project directory: {project_dir}")

    try:
        git_ops = GitOperations(project_dir)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Find all worktrees under .llm-sandbox/worktrees/
    worktrees_base = project_dir / ".llm-sandbox" / "worktrees"

    if worktrees_base.exists():
        click.echo(f"\nRemoving worktrees from: {worktrees_base}")

        # Iterate through instance directories
        for instance_dir in worktrees_base.iterdir():
            if instance_dir.is_dir():
                click.echo(f"  Instance: {instance_dir.name}")

                # Find all worktree directories recursively (they might be nested)
                # A directory is a worktree if it has a .git file
                for worktree_path in instance_dir.rglob("*"):
                    if worktree_path.is_dir() and (worktree_path / ".git").exists():
                        try:
                            # Get relative path from instance dir for display
                            rel_path = worktree_path.relative_to(instance_dir)
                            click.echo(f"    Removing worktree: {rel_path}")
                            git_ops.remove_worktree(worktree_path)
                        except Exception as e:
                            click.echo(f"    Warning: Failed to remove {rel_path}: {e}", err=True)

        # Remove the entire worktrees directory
        try:
            shutil.rmtree(worktrees_base)
            click.echo(f"✓ Removed worktrees directory")
        except Exception as e:
            click.echo(f"Warning: Failed to remove worktrees directory: {e}", err=True)
    else:
        click.echo("\nNo worktrees directory found")

    # Find and delete all llm-container/* branches
    click.echo("\nDeleting llm-container branches")

    try:
        # Get all branches
        branches = [ref.name for ref in git_ops.repo.refs if ref.name.startswith("llm-container/")]

        if branches:
            for branch_name in branches:
                try:
                    click.echo(f"  Deleting branch: {branch_name}")
                    git_ops.delete_branch(branch_name)
                except Exception as e:
                    click.echo(f"  Warning: Failed to delete {branch_name}: {e}", err=True)

            click.echo(f"✓ Deleted {len(branches)} branch(es)")
        else:
            click.echo("  No llm-container branches found")

    except Exception as e:
        click.echo(f"Error listing branches: {e}", err=True)
        sys.exit(1)

    click.echo("\n✓ Cleanup complete")


def make_subcommand_callback(subcommand_instance):
    """
    Create a callback function for a subcommand.

    Args:
        subcommand_instance: Instance of Subcommand

    Returns:
        Click callback function
    """
    def callback(project_dir, network, verbose, **kwargs):
        # Load configuration (project overrides global)
        config = load_config(project_dir)

        # Create SandboxRunner instance
        runner = SandboxRunner(project_dir, config)

        # Execute the subcommand
        try:
            subcommand_instance.execute(
                project_dir=project_dir,
                runner=runner,
                network=network,
                verbose=verbose,
                **kwargs
            )
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
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

    # Let the subcommand add its custom arguments
    subcommand_wrapper = subcommand_instance.add_arguments(subcommand_wrapper)

    # Register with CLI
    cli.add_command(subcommand_wrapper)


def register_builtin_subcommands():
    """Register built-in subcommands."""
    register_subcommand_class(RunSubcommand)
    register_subcommand_class(GenContainerfileSubcommand)
    register_subcommand_class(PRReviewSubcommand)


def register_custom_subcommands():
    """
    Discover and register custom subcommands from config directories.
    """
    subcommands = discover_subcommands(Path.cwd())

    for name, subcommand_class in subcommands.items():
        register_subcommand_class(subcommand_class)


register_builtin_subcommands()
register_custom_subcommands()


if __name__ == "__main__":
    cli()
