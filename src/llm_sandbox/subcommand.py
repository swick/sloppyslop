"""Base class for custom subcommands."""

import asyncio
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import click


class Subcommand(ABC):
    """
    Base class for custom subcommands.

    Subcommand modules should define a class that inherits from this
    and implements the required methods.

    Example:
        import asyncio
        from llm_sandbox import Agent
        from llm_sandbox.config import load_config
        from llm_sandbox.runner import SandboxRunner
        from llm_sandbox.mcp_tools import MCPServer, ReadFileTool, ExecuteCommandTool

        class AnalyzeMCPServer(MCPServer):
            def __init__(self, runner):
                super().__init__()
                self.add_tool(ReadFileTool(runner))
                self.add_tool(ExecuteCommandTool(runner))

        class MySubcommand(Subcommand):
            name = "analyze"
            help = "Analyze the project"

            def add_arguments(self, command):
                # Add custom options (common options like --network and --verbose are automatic)
                command.params.append(
                    click.Option(["--depth"], type=int, default=3, help="Analysis depth")
                )
                return command

            def execute(self, project_dir, **kwargs):
                # Common options available in kwargs:
                # - network: from --network
                # - verbose: from --verbose

                depth = kwargs.get("depth", 3)
                network = kwargs["network"]
                verbose = kwargs["verbose"]

                # Load config
                config = load_config(project_dir)

                # Pull image if needed
                from llm_sandbox.container import ContainerManager, DEFAULT_IMAGE
                image_tag = config.image.image if config.image and config.image.image else DEFAULT_IMAGE
                container_manager = ContainerManager()
                if not container_manager.image_exists(image_tag):
                    container_manager.pull_image(image_tag)

                # Create runner (setup happens in constructor)
                runner = SandboxRunner(
                    project_dir,
                    config,
                    verbose=verbose,
                    keep_branches=["my-branch"],
                    network=network,
                )

                # Run agent with async context manager for cleanup
                async def main():
                    async with runner:
                        mcp_server = AnalyzeMCPServer(runner)

                        # Option 1: Using Agent class (recommended)
                        from llm_sandbox import Agent

                        agent = Agent(
                            runner=runner,
                            prompt=f"Analyze this project with depth {depth}",
                            output_schema={"type": "object", ...},
                            mcp_server=mcp_server,
                        )
                        await agent.execute()  # Start execution
                        result = await agent.wait()  # Wait for result

                        # Note: Background agents are spawned as children via SpawnAgentTool
                        # which automatically sets spawn_depth based on parent depth

                        output.info(f"Analysis complete: {result}")

                asyncio.run(main())
    """

    name: str = None  # Subcommand name (e.g., "analyze")
    help: str = None  # Help text for the subcommand

    def add_arguments(self, command: click.Command) -> click.Command:
        """
        Add custom arguments to the command.

        Args:
            command: Click command object to modify

        Returns:
            Modified command object (can also return a click.Group for nested subcommands)
        """
        return command

    @abstractmethod
    def execute(
        self,
        project_dir: Path,
        **kwargs
    ) -> Any:
        """
        Execute the subcommand.

        Args:
            project_dir: Project directory path
            **kwargs: Arguments from CLI including:
                - network: Network mode from --network
                - verbose: Verbose flag from --verbose
                - Any custom arguments added by add_arguments()

        Returns:
            Any value (typically None or a result dict)

        Note:
            Subcommands should create their own SandboxRunner instance.
            Setup happens automatically in the constructor:
                from llm_sandbox.config import load_config
                from llm_sandbox.container import ContainerManager, DEFAULT_IMAGE
                from llm_sandbox.runner import SandboxRunner

                config = load_config(project_dir)

                # Pull image if needed
                image_tag = config.image.image if config.image and config.image.image else DEFAULT_IMAGE
                container_manager = ContainerManager()
                if not container_manager.image_exists(image_tag):
                    container_manager.pull_image(image_tag)

                runner = SandboxRunner(
                    project_dir,
                    config,
                    verbose=verbose,
                    network=network,
                )

            Best practice is to use the async context manager pattern for cleanup:
                async with runner:
                    # ... create mcp_server ...
                    from llm_sandbox import Agent
                    agent = Agent(
                        runner=runner,
                        prompt="...",
                        output_schema={...},
                        mcp_server=mcp_server
                    )
                    await agent.execute()
                    result = await agent.wait()
        """
        pass


def discover_subcommands(project_dir: Optional[Path] = None) -> Dict[str, type]:
    """
    Discover subcommand modules from config directories.

    Searches in order:
    1. Project-level: {project_dir}/.llm-sandbox/subcommands/*.py
    2. Global: $XDG_CONFIG_HOME/llm-sandbox/subcommands/*.py

    Args:
        project_dir: Project directory (optional)

    Returns:
        Dict mapping subcommand names to their classes
    """
    import importlib.util
    import os
    import sys

    subcommands = {}
    search_paths = []

    # Add global config subcommands directory
    xdg_config = os.getenv("XDG_CONFIG_HOME")
    if xdg_config:
        global_dir = Path(xdg_config) / "llm-sandbox" / "subcommands"
    else:
        global_dir = Path.home() / ".config" / "llm-sandbox" / "subcommands"

    if global_dir.exists():
        search_paths.append(global_dir)

    # Add project-level subcommands directory
    if project_dir:
        project_subcommands_dir = project_dir / ".llm-sandbox" / "subcommands"
        if project_subcommands_dir.exists():
            search_paths.append(project_subcommands_dir)

    # Load subcommand modules
    for search_path in search_paths:
        for file_path in search_path.glob("*.py"):
            if file_path.name.startswith("_"):
                continue

            module_name = f"llm_sandbox_subcommand_{file_path.stem}"

            try:
                # Load module
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)

                    # Find Subcommand classes in module
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, Subcommand)
                            and attr is not Subcommand
                            and hasattr(attr, "name")
                            and attr.name
                        ):
                            subcommands[attr.name] = attr

            except Exception as e:
                # Note: Can't use OutputService here - this runs at import time
                # before any OutputService exists (during module initialization)
                sys.stderr.write(
                    f"Warning: Failed to load subcommand from {file_path}: {e}\n"
                )

    return subcommands
