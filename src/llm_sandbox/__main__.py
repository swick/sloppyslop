"""CLI entry point for LLM Sandbox."""

import json
import sys
from pathlib import Path
from typing import Optional

import click

from llm_sandbox.analyzer import ProjectAnalyzer
from llm_sandbox.builtin_subcommands import RunSubcommand
from llm_sandbox.config import (
    AnthropicConfig,
    BuildConfig,
    Config,
    ImageConfig,
    VertexAIConfig,
    get_provider_config,
    load_config,
    load_project_config,
    save_project_config,
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

        # Create provider
        llm_provider = create_llm_provider(provider_name, provider_config)

        # Validate
        result = llm_provider.validate()

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


@cli.command(name="gen-containerfile")
@click.argument("image_name", required=True)
@click.option(
    "--extra-prompt",
    type=str,
    help="Additional instructions to add to the generation prompt",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing Containerfile configuration",
)
def gen_containerfile(image_name: str, extra_prompt: Optional[str], force: bool):
    """Generate a Containerfile for the specified image environment."""
    project_dir = Path.cwd()

    click.echo(f"Generating Containerfile for: {image_name}")
    click.echo(f"Project directory: {project_dir}")

    # Check if project already has a Containerfile configuration
    existing_project_config = load_project_config(project_dir)
    if existing_project_config.image.build is not None and not force:
        click.echo(
            f"Error: Project already has a Containerfile configuration:\n"
            f"  Containerfile: {existing_project_config.image.build.containerfile}\n"
            f"\nUse --force to overwrite the existing configuration.",
            err=True
        )
        sys.exit(1)

    # Load config (merged global + project) and create LLM provider
    config = load_config(project_dir)

    try:
        provider_name, provider_config = get_provider_config(config)
        llm_provider = create_llm_provider(provider_name, provider_config)
    except Exception as e:
        click.echo(f"Error: LLM provider not configured: {e}", err=True)
        click.echo("\nPlease configure your LLM provider first:", err=True)
        click.echo("  llm-sandbox check", err=True)
        sys.exit(1)

    # Create analyzer with LLM provider
    analyzer = ProjectAnalyzer(llm_provider)

    # Generate containerfile
    click.echo("\nGenerating Containerfile with LLM...")
    containerfile_content = analyzer.generate_containerfile(
        project_dir,
        image_name,
        extra_prompt,
    )

    # Show generated content
    click.echo("\n" + "=" * 60)
    click.echo("Generated Containerfile:")
    click.echo("=" * 60)
    click.echo(containerfile_content)
    click.echo("=" * 60)

    # Save containerfile
    config_dir = project_dir / ".llm-sandbox"
    config_dir.mkdir(parents=True, exist_ok=True)
    containerfile_path = config_dir / "Containerfile"
    containerfile_path.write_text(containerfile_content)
    click.echo(f"\n✓ Saved to: {containerfile_path.relative_to(project_dir)}")

    # Load or create project config
    project_config = load_project_config(project_dir)

    # Update image configuration
    build_config = BuildConfig(
        containerfile=".llm-sandbox/Containerfile",
        auto_rebuild=True,
    )
    image_config = ImageConfig(image=image_name, build=build_config)

    # Update only the image field, preserve other settings
    project_config.image = image_config

    # Save updated config
    save_project_config(project_dir, project_config)
    click.echo(f"✓ Updated configuration: .llm-sandbox/config.yaml")
    click.echo(f"  Image name: {image_name}")
    click.echo(f"  Build from: .llm-sandbox/Containerfile")
    click.echo(f"  Auto-rebuild: {build_config.auto_rebuild}")

    click.echo(f"\nNext steps:")
    click.echo(f"  llm-sandbox rebuild           # Rebuild the image")
    click.echo(f"  llm-sandbox run --commit HEAD --prompt 'Your prompt' --schema '{{...}}'")
    click.echo(f"  llm-sandbox run --prompt-file prompt.txt --schema-file schema.json")


@cli.command()
def rebuild():
    """Rebuild the container image from Containerfile."""
    project_dir = Path.cwd()

    click.echo(f"Rebuilding container image")
    click.echo(f"Project directory: {project_dir}")

    # Load config
    config = load_config(project_dir)

    # Create container manager and image manager
    container_manager = ContainerManager()
    image_manager = Image(config.image, project_dir, container_manager)

    try:
        # Force rebuild
        image_tag = image_manager.rebuild()

        click.echo(f"\n✓ Successfully rebuilt image: {image_tag}")
        click.echo(f"\nThe image will be used on the next run.")

    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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

    # Load configuration (project overrides global)
    config = load_config(project_dir)

    # Initialize runner
    runner = SandboxRunner(project_dir, config)

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

        # Run prompt
        return runner.run_prompt(
            commit,
            prompt,
            output_schema,
            branches_to_pull,
            network,
        )

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
        help="Comma-separated list of worktree names to keep as output branches",
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
