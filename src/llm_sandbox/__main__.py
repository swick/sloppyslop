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
    load_global_config,
    load_project_config,
    merge_configs,
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
    "--provider",
    type=str,
    help="Provider to test (defaults to default_provider from config)",
)
def check(provider: Optional[str]):
    """Check LLM provider configuration and connectivity."""
    import sys

    click.echo("Checking LLM provider configuration...\n")

    # Load global config
    global_config = load_global_config()

    try:
        # Get provider config
        provider_name, provider_config = get_provider_config(global_config, provider)

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


@cli.command()
@click.option(
    "--project-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
    help="Project directory (defaults to current directory)",
)
@click.option(
    "--generate",
    is_flag=True,
    help="Generate Containerfile using LLM (requires configured provider)",
)
def containerfile(project_dir: Path, generate: bool):
    """Set up Containerfile for the project (find existing or generate new)."""
    click.echo(f"Setting up Containerfile for: {project_dir}")

    # Check if already initialized
    config_dir = project_dir / ".llm-sandbox"
    if config_dir.exists() and (config_dir / "config.yaml").exists():
        click.echo("Project already initialized!")
        if not click.confirm("Reconfigure?"):
            return

    # Search for existing containerfiles (without LLM)
    click.echo("\nSearching for existing Containerfile/Dockerfile...")
    analyzer = ProjectAnalyzer(None)  # Search without LLM
    found_containerfiles = analyzer.search_containerfiles(project_dir)

    containerfile_path = None
    llm_provider = None

    # Build options list
    options = []
    if generate or not found_containerfiles:
        # Check if we can generate
        try:
            global_config = load_global_config()
            provider_name, provider_config = get_provider_config(global_config)
            llm_provider = create_llm_provider(provider_name, provider_config)
            options.append(("generate", "Generate new Containerfile with LLM"))
        except Exception as e:
            if generate:
                click.echo(f"Error: Cannot generate - LLM provider not configured: {e}", err=True)
                sys.exit(1)

    options.append(("custom", "Specify custom path"))

    for containerfile in found_containerfiles:
        rel_path = containerfile.relative_to(project_dir)
        options.append(("existing", f"Use existing: {rel_path}", containerfile))

    # If only one existing file found and not forcing generation, use it
    if len(found_containerfiles) == 1 and not generate:
        containerfile_path = found_containerfiles[0]
        rel_path = containerfile_path.relative_to(project_dir)
        click.echo(f"\nFound Containerfile: {rel_path}")
        if click.confirm("Use this Containerfile?", default=True):
            pass  # Use it
        else:
            containerfile_path = None

    # Interactive selection if not already decided
    while containerfile_path is None:
        click.echo("\nOptions:")
        for i, opt in enumerate(options, 1):
            if opt[0] == "existing":
                click.echo(f"  {i}. {opt[1]}")
            else:
                click.echo(f"  {i}. {opt[1]}")

        # Auto-select if --generate flag and generation is option 1
        if generate and options[0][0] == "generate":
            choice = 1
            click.echo(f"\nUsing option {choice} (--generate flag)")
        else:
            # Default to first existing file if available, otherwise custom path
            default_choice = next(
                (i + 1 for i, opt in enumerate(options) if opt[0] == "existing"),
                next((i + 1 for i, opt in enumerate(options) if opt[0] == "custom"), 1)
            )

            choice = click.prompt(
                "Select an option",
                type=int,
                default=default_choice,
            )

        # Validate choice
        if choice < 1 or choice > len(options):
            click.echo(f"Invalid choice. Please select a number between 1 and {len(options)}.")
            continue

        selected_option = options[choice - 1]

        if selected_option[0] == "generate":
            # Generate containerfile
            click.echo("\nGenerating Containerfile...")
            analyzer = ProjectAnalyzer(llm_provider)
            containerfile_content = analyzer.generate_containerfile(project_dir)

            # Show preview
            click.echo("\n" + "=" * 60)
            click.echo("Generated Containerfile:")
            click.echo("=" * 60)
            click.echo(containerfile_content)
            click.echo("=" * 60)

            if not click.confirm("\nUse this Containerfile?", default=True):
                continue

            # Save containerfile
            containerfile_path = project_dir / ".llm-sandbox" / "Containerfile"
            containerfile_path.parent.mkdir(parents=True, exist_ok=True)
            containerfile_path.write_text(containerfile_content)
            click.echo(f"Saved to: {containerfile_path.relative_to(project_dir)}")

        elif selected_option[0] == "custom":
            # Specify custom path
            custom_path = click.prompt(
                "Enter path to Containerfile (relative to project dir)",
                type=str,
            )
            custom_file = project_dir / custom_path
            if not custom_file.exists() or not custom_file.is_file():
                click.echo(f"File not found: {custom_path}")
                continue
            containerfile_path = custom_file

        else:  # existing
            containerfile_path = selected_option[2]

    # Get relative path
    rel_containerfile_path = str(containerfile_path.relative_to(project_dir))

    # Create project config with build configuration
    build_config = BuildConfig(
        containerfile=rel_containerfile_path,
        auto_rebuild=True,
    )
    image_config = ImageConfig(build=build_config)
    project_config = Config(image=image_config)

    save_project_config(project_dir, project_config)
    click.echo(f"\n✓ Configuration saved to: .llm-sandbox/config.yaml")
    click.echo(f"  Containerfile: {rel_containerfile_path}")
    click.echo(f"  Auto-rebuild: {build_config.auto_rebuild}")

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

    # Load and merge configurations (project overrides global)
    global_config = load_global_config()
    project_config = load_project_config(project_dir)
    merged_config = merge_configs(global_config, project_config)

    # Initialize runner
    runner = SandboxRunner(project_dir, merged_config)

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
