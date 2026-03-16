"""CLI entry point for LLM Sandbox."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import click

from llm_sandbox.subcommands import RunSubcommand, GenContainerfileSubcommand, ReviewSubcommand
from llm_sandbox.config import (
    AnthropicConfig,
    Config,
    VertexAIConfig,
    get_provider_config,
    load_config,
)
from llm_sandbox.container import ContainerManager
from llm_sandbox.event_handlers import wire_up_container_events, wire_up_image_events
from llm_sandbox.image import Image
from llm_sandbox.llm_provider import create_llm_provider
from llm_sandbox.output import create_output_service
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
    from llm_sandbox.output import create_output_service

    output = create_output_service(format="text", verbose=False)

    output.info("Checking LLM provider configuration...\n")

    # Load config (merged global + project)
    config = load_config(Path.cwd())

    try:
        # Get provider config
        provider_name, provider_config = get_provider_config(config, provider)

        output.info(f"Provider: {provider_name}")
        output.info(f"Model: {provider_config.model}")

        if isinstance(provider_config, VertexAIConfig):
            output.info(f"Region: {provider_config.region}")
            output.info(f"Project ID: {provider_config.project_id}")
        elif isinstance(provider_config, AnthropicConfig):
            output.info(f"API Key Env: {provider_config.api_key_env}")
        else:
            raise ValueError(f"Unknown provider config type: {type(provider_config)}")

        output.info("\nValidating provider...")

        # Create provider with simple system prompt for validation
        llm_provider = create_llm_provider(
            provider_name,
            provider_config,
            base_system_prompt="You are a helpful AI assistant.",
        )

        # Validate
        result = asyncio.run(llm_provider.validate())

        if result["success"]:
            output.success(result['message'])
            if "details" in result and "response_id" in result["details"]:
                output.info(f"  Response ID: {result['details']['response_id']}")
            sys.exit(0)
        else:
            output.error(result['message'])
            if "details" in result:
                details = result["details"]
                if "error_type" in details:
                    output.error(f"  Error Type: {details['error_type']}")
                if "error_message" in details:
                    output.error(f"  Error: {details['error_message']}")
                if "guidance" in details:
                    output.info(f"  Suggestion: {details['guidance']}")
            sys.exit(1)

    except ValueError as e:
        output.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        output.error(f"Unexpected error: {e}")
        sys.exit(1)


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    help="Force rebuild even if image is up-to-date",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
def build(force: bool, verbose: bool):
    """Build the container image from Containerfile."""
    project_dir = Path.cwd()

    # Create output service
    output = create_output_service(format="text", verbose=verbose)

    output.info(f"Building container image")
    output.info(f"Project directory: {project_dir}")

    # Load config
    config = load_config(project_dir)

    # Create container manager and image manager
    container_manager = ContainerManager()

    # Wire up event handlers for progress display
    wire_up_container_events(container_manager, output)

    image_manager = Image(config.image, project_dir, container_manager)
    wire_up_image_events(image_manager, output)

    try:
        # Build image
        image_tag = image_manager.build(force=force)

        output.success(f"Successfully built image: {image_tag}")
        output.info(f"\nThe image will be used on the next run.")

    except RuntimeError as e:
        output.error(str(e))
        sys.exit(1)


@cli.command()
def cleanup():
    """Clean up all llm-sandbox worktrees and llm-container branches."""
    import shutil
    from llm_sandbox.git_ops import GitOperations
    from llm_sandbox.output import create_output_service

    output = create_output_service(format="text", verbose=False)
    project_dir = Path.cwd()

    output.info("Cleaning up llm-sandbox worktrees and branches")
    output.info(f"Project directory: {project_dir}")

    try:
        git_ops = GitOperations(project_dir)
    except ValueError as e:
        output.error(str(e))
        sys.exit(1)

    # Find all worktrees under .llm-sandbox/worktrees/
    worktrees_base = project_dir / ".llm-sandbox" / "worktrees"

    if worktrees_base.exists():
        output.info(f"\nRemoving worktrees from: {worktrees_base}")

        # Iterate through instance directories
        for instance_dir in worktrees_base.iterdir():
            if instance_dir.is_dir():
                output.info(f"  Instance: {instance_dir.name}")

                # Find all worktree directories recursively (they might be nested)
                # A directory is a worktree if it has a .git file
                for worktree_path in instance_dir.rglob("*"):
                    if worktree_path.is_dir() and (worktree_path / ".git").exists():
                        try:
                            # Get relative path from instance dir for display
                            rel_path = worktree_path.relative_to(instance_dir)
                            output.info(f"    Removing worktree: {rel_path}")
                            git_ops.remove_worktree(worktree_path)
                        except Exception as e:
                            output.warning(f"    Failed to remove {rel_path}: {e}")

        # Remove the entire worktrees directory
        try:
            shutil.rmtree(worktrees_base)
            output.success("Removed worktrees directory")
        except Exception as e:
            output.warning(f"Failed to remove worktrees directory: {e}")
    else:
        output.info("\nNo worktrees directory found")

    # Find and delete all llm-container/* branches
    output.info("\nDeleting llm-container branches")

    try:
        # Get all branches
        branches = [ref.name for ref in git_ops.repo.refs if ref.name.startswith("llm-container/")]

        if branches:
            for branch_name in branches:
                try:
                    output.info(f"  Deleting branch: {branch_name}")
                    git_ops.delete_branch(branch_name)
                except Exception as e:
                    output.warning(f"  Failed to delete {branch_name}: {e}")

            output.success(f"Deleted {len(branches)} branch(es)")
        else:
            output.info("  No llm-container branches found")

    except Exception as e:
        output.error(f"Error listing branches: {e}")
        sys.exit(1)

    output.success("\nCleanup complete")


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


def register_builtin_subcommands():
    """Register built-in subcommands."""
    register_subcommand_class(RunSubcommand)
    register_subcommand_class(GenContainerfileSubcommand)
    register_subcommand_class(ReviewSubcommand)


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
