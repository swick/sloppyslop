"""CLI entry point for LLM Sandbox."""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from llm_sandbox.analyzer import ProjectAnalyzer
from llm_sandbox.builtin_subcommands import RunSubcommand
from llm_sandbox.config import (
    GlobalConfig,
    ProjectConfig,
    get_provider_config,
    load_global_config,
    load_project_config,
    save_project_config,
)
from llm_sandbox.llm_provider import create_llm_provider
from llm_sandbox.runner import SandboxRunner
from llm_sandbox.subcommand import discover_subcommands


@click.group()
def cli():
    """LLM Container Sandbox - Safe isolated execution environment for LLM code analysis."""
    pass


@cli.command()
@click.option(
    "--project-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Project directory (defaults to current directory)",
)
@click.option(
    "--containerfile",
    type=str,
    help="Path to Containerfile (relative to project dir). If not specified, uses .llm-sandbox/Containerfile",
)
def init(project_dir: Path, containerfile: Optional[str]):
    """Initialize project configuration."""
    click.echo(f"Initializing LLM Sandbox in: {project_dir}")

    # Determine containerfile path
    if containerfile:
        containerfile_rel_path = containerfile
        click.echo(f"Using custom Containerfile path: {containerfile_rel_path}")
    else:
        containerfile_rel_path = ".llm-sandbox/Containerfile"

    # Check if already initialized
    config_dir = project_dir / ".llm-sandbox"
    if config_dir.exists() and (config_dir / "config.yaml").exists():
        click.echo("Project already initialized!")
        if not click.confirm("Reinitialize?"):
            return

    # Load global config for API key
    global_config = load_global_config()

    try:
        provider_name, provider_config = get_provider_config(global_config)
        llm_provider = create_llm_provider(provider_name, provider_config)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Initialize analyzer
    analyzer = ProjectAnalyzer(llm_provider)

    containerfile_content = None
    containerfile_source = None

    # Check if user specified a custom containerfile path
    if containerfile:
        custom_path = project_dir / containerfile
        if custom_path.exists() and custom_path.is_file():
            # Use the specified existing file
            click.echo(f"\nUsing existing Containerfile: {containerfile}")
            containerfile_content = custom_path.read_text()
            containerfile_source = containerfile
        else:
            # Will save generated/selected containerfile to this path
            click.echo(f"\nWill save Containerfile to: {containerfile}")

    # Search for existing containerfiles if we don't have one yet
    if containerfile_content is None:
        click.echo("\nSearching for existing Containerfile/Dockerfile...")
        found_containerfiles = analyzer.search_containerfiles(project_dir)
    else:
        found_containerfiles = []

    if found_containerfiles and containerfile_content is None:
        click.echo(f"\nFound {len(found_containerfiles)} containerfile(s):")
        for i, path in enumerate(found_containerfiles, 1):
            rel_path = path.relative_to(project_dir)
            click.echo(f"  {i}. {rel_path}")

        click.echo("  0. Generate new Containerfile with LLM")

        choice = click.prompt(
            "Select a containerfile or generate new",
            type=int,
            default=0,
        )

        if choice > 0 and choice <= len(found_containerfiles):
            # Use selected containerfile
            selected_path = found_containerfiles[choice - 1]
            containerfile_content = selected_path.read_text()
            containerfile_source = str(selected_path.relative_to(project_dir))
            click.echo(f"\nUsing: {containerfile_source}")
        else:
            # Generate new
            choice = 0

    elif containerfile_content is None:
        click.echo("No existing containerfiles found.")
        if not click.confirm("\nGenerate Containerfile with LLM?", default=True):
            click.echo("Initialization cancelled.")
            sys.exit(0)
        choice = 0

    # Generate containerfile if needed
    if choice == 0 or containerfile_content is None:
        click.echo("\nGenerating Containerfile...")
        try:
            containerfile_content = analyzer.generate_containerfile(project_dir)
            containerfile_source = "generated"

            # Show preview
            click.echo("\n" + "=" * 60)
            click.echo("Generated Containerfile:")
            click.echo("=" * 60)
            click.echo(containerfile_content)
            click.echo("=" * 60)

            if not click.confirm("\nUse this Containerfile?", default=True):
                click.echo("Initialization cancelled.")
                sys.exit(0)

        except Exception as e:
            click.echo(f"Error generating Containerfile: {e}", err=True)
            sys.exit(1)

    # Save containerfile
    click.echo("\nSaving configuration...")
    if containerfile:
        # Save to custom path
        containerfile_path = project_dir / containerfile
        containerfile_path.parent.mkdir(parents=True, exist_ok=True)
        containerfile_path.write_text(containerfile_content)
        final_containerfile_rel_path = containerfile
        click.echo(f"Saved Containerfile to: {final_containerfile_rel_path}")
    else:
        # Save to default .llm-sandbox/Containerfile
        containerfile_path = analyzer.save_containerfile(containerfile_content, project_dir)
        final_containerfile_rel_path = str(containerfile_path.relative_to(project_dir))
        click.echo(f"Saved Containerfile to: {final_containerfile_rel_path}")

    # Create project config
    project_name = project_dir.name
    project_config = ProjectConfig(
        containerfile=final_containerfile_rel_path,  # Path relative to project dir
        image_tag=f"llm-sandbox-{project_name}",
    )

    save_project_config(project_dir, project_config)
    click.echo(f"Saved config to: .llm-sandbox/config.yaml")

    click.echo("\n✓ Initialization complete!")
    click.echo(f"\nNext steps:")
    click.echo(f"  llm-sandbox run --commit HEAD --prompt 'Your prompt' --schema '{{...}}'")
    click.echo(f"  llm-sandbox run --prompt-file prompt.txt --schema-file schema.json")


def create_run_sandbox_function(
    project_dir: Path,
    commit: str,
    network: Optional[str],
    pull_branches: Optional[str],
):
    """
    Create a run_sandbox function for use by subcommands.

    This function is pre-configured with common options from the command line.

    Args:
        project_dir: Project directory
        commit: Git commit/branch/tag to use
        network: Network mode override
        pull_branches: Comma-separated list of branches to pull

    Returns:
        Function that can run the sandbox
    """
    # Parse branches to pull
    branches_to_pull = []
    if pull_branches:
        branches_to_pull = [b.strip() for b in pull_branches.split(",")]

    def run_sandbox(
        prompt: str,
        output_schema: dict,
    ) -> dict:
        """
        Run the sandbox and return structured output.

        Args:
            prompt: User prompt for LLM
            output_schema: JSON schema for structured output

        Returns:
            Structured output from LLM

        Note:
            The commit, network, and branches_to_pull are already configured
            from command line options.
        """
        # Load configurations
        global_config = load_global_config()
        project_config = load_project_config(project_dir)

        # Initialize runner
        runner = SandboxRunner(project_dir, global_config, project_config)

        # Run prompt
        result = runner.run_prompt(
            commit,
            prompt,
            output_schema,
            branches_to_pull,
            network,
        )

        return result

    return run_sandbox


def make_subcommand_callback(subcommand_instance):
    """
    Create a callback function for a subcommand.

    Args:
        subcommand_instance: Instance of Subcommand

    Returns:
        Click callback function
    """
    def callback(project_dir, commit, network, pull_branches, **kwargs):
        # Create run_sandbox function pre-configured with common options
        run_sandbox = create_run_sandbox_function(
            project_dir,
            commit,
            network,
            pull_branches,
        )

        # Execute the subcommand
        try:
            subcommand_instance.execute(
                project_dir=project_dir,
                run_sandbox=run_sandbox,
                commit=commit,
                network=network,
                pull_branches=pull_branches,
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
        "--commit",
        type=str,
        default="HEAD",
        help="Git commit/branch/tag to use (default: HEAD)",
    )
    @click.option(
        "--network",
        type=click.Choice(["isolated", "enabled"]),
        help="Network access mode (default: from config)",
    )
    @click.option(
        "--pull-branches",
        help="Comma-separated list of branches to pull from worktree",
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


def register_custom_subcommands(project_dir: Optional[Path] = None):
    """
    Discover and register custom subcommands from config directories.

    Args:
        project_dir: Project directory (defaults to current directory)
    """
    if project_dir is None:
        project_dir = Path.cwd()

    subcommands = discover_subcommands(project_dir)

    for name, subcommand_class in subcommands.items():
        register_subcommand_class(subcommand_class)


# Register built-in subcommands (always available)
register_builtin_subcommands()

# Register custom subcommands
# Try to register from current directory, but don't fail if not a project
try:
    register_custom_subcommands(Path.cwd())
except Exception:
    # If current directory is not a project, only load global subcommands
    try:
        register_custom_subcommands(None)
    except Exception:
        pass


if __name__ == "__main__":
    cli()
