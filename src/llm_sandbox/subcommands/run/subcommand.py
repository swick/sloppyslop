"""Run subcommand for llm-sandbox."""

import asyncio
import json
import sys
from pathlib import Path

import click

from llm_sandbox.config import load_config
from llm_sandbox.container import ContainerManager, DEFAULT_IMAGE
from llm_sandbox.event_handlers import wire_up_runner_events, create_image_pull_callback
from llm_sandbox.output import create_output_service
from llm_sandbox.runner import SandboxRunner
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
    SpawnAgentTool,
    WaitForAgentsTool,
    AskUserTool,
)


class RunMCPServer(MCPServer):
    """MCP server with all built-in tools for run subcommand."""

    def __init__(self, runner, interactive=False):
        """
        Initialize run MCP server with all built-in tools.

        Args:
            runner: SandboxRunner instance
            interactive: Whether to enable interactive mode (adds AskUserTool)
        """
        super().__init__()
        self.add_tool(ExecuteCommandTool(runner))
        self.add_tool(CheckoutCommitTool(runner))
        self.add_tool(GitCommitTool(runner))
        self.add_tool(ReadFileTool(runner))
        self.add_tool(WriteFileTool(runner))
        self.add_tool(EditFileTool(runner))
        self.add_tool(GlobTool(runner))
        self.add_tool(GrepTool(runner))
        self.add_tool(SpawnAgentTool(runner))
        self.add_tool(WaitForAgentsTool(runner))

        # Add AskUserTool for interactive mode
        if interactive:
            self.add_tool(AskUserTool())


class RunSubcommand(Subcommand):
    """Run LLM agent in isolated container."""

    name = "run"
    help = "Run LLM agent in isolated container"

    def execute(self, **kwargs):
        """Not used - subcommands handle execution."""
        pass

    def add_arguments(self, command: click.Command) -> click.Command:
        """Create subcommands for run."""
        # Create a group for run subcommands
        @click.group(name="run", help="Run LLM agent in isolated container")
        def run_group():
            pass

        # Add singleshot subcommand
        @run_group.command(name="singleshot", help="Execute a single prompt with structured output")
        @click.option(
            "--keep-branch",
            multiple=True,
            help="Branch name to keep as output (can be specified multiple times)",
        )
        @click.option(
            "--prompt",
            type=str,
            help="Prompt text (required unless --prompt-file is used)",
        )
        @click.option(
            "--prompt-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="File containing the prompt",
        )
        @click.option(
            "--schema",
            type=str,
            help="JSON schema string for structured output (required unless --schema-file is used)",
        )
        @click.option(
            "--schema-file",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="JSON schema file for structured output",
        )
        def singleshot(**kwargs):
            self.execute_singleshot(**kwargs)

        # Add interactive subcommand
        @run_group.command(name="interactive", help="Start an interactive conversation with the agent")
        @click.option(
            "--keep-branch",
            multiple=True,
            help="Branch name to keep as output (can be specified multiple times)",
        )
        def interactive(**kwargs):
            self.execute_interactive(**kwargs)

        return run_group

    def execute_singleshot(self, **kwargs):
        """
        Execute single-shot command with structured output.
        """
        keep_branch = kwargs.get("keep_branch", ())
        prompt = kwargs.get("prompt")
        prompt_file = kwargs.get("prompt_file")
        schema = kwargs.get("schema")
        schema_file = kwargs.get("schema_file")
        network = kwargs.get("network")
        verbose = kwargs.get("verbose", False)
        project_dir = kwargs.get("project_dir", Path.cwd())

        # Create output service (quiet unless verbose)
        output = create_output_service(format="text", verbose=verbose)

        # Load config
        config = load_config(project_dir)

        # Determine image tag
        image = kwargs.get("image")
        if image:
            image_tag = image
        elif config.image and config.image.image:
            image_tag = config.image.image
        else:
            image_tag = DEFAULT_IMAGE

        # Pull image if needed
        container_manager = ContainerManager()
        if not container_manager.image_exists(image_tag):
            pull_callback = create_image_pull_callback(output)
            container_manager.pull_image(image_tag, progress_callback=pull_callback)

        # Create runner
        runner = SandboxRunner(
            project_dir,
            config,
            verbose=verbose,
            keep_branches=list(keep_branch) if keep_branch else [],
            network=network,
        )

        # Wire up event handlers (only if verbose)
        if verbose:
            wire_up_runner_events(runner, output)

        # Display instance and container info (only if verbose)
        if verbose:
            output.info(f"Instance ID: {runner.instance_id}")
            output.success(f"Container started: {runner.container_id[:12]}")

        # Validate prompt input
        if prompt and prompt_file:
            output.error("Cannot use both --prompt and --prompt-file")
            sys.exit(1)

        # Load prompt from file if specified
        if prompt_file:
            prompt = prompt_file.read_text()
        elif not prompt:
            output.error("Either --prompt or --prompt-file must be provided")
            sys.exit(1)

        # Validate schema input
        if schema and schema_file:
            output.error("Cannot use both --schema and --schema-file")
            sys.exit(1)

        # Load output schema
        if schema:
            try:
                output_schema = json.loads(schema)
            except json.JSONDecodeError as e:
                output.error(f"Invalid JSON schema: {e}")
                sys.exit(1)
        elif schema_file:
            with open(schema_file) as f:
                output_schema = json.load(f)
        else:
            output.error("Either --schema or --schema-file must be provided")
            sys.exit(1)

        # Run the sandbox using async context manager pattern
        try:
            result = asyncio.run(self._execute_singleshot_async(
                runner,
                prompt,
                output_schema,
                verbose,
                output
            ))

            # Output result as JSON (only JSON in non-verbose mode)
            if verbose:
                output.info("\n" + "=" * 60)
                output.info("Result:")
                output.info("=" * 60)
            print(json.dumps(result, indent=2))

        except Exception as e:
            if verbose:
                output.error(f"Execution failed: {e}")
            else:
                # In non-verbose mode, output error as JSON to stderr
                import sys as _sys
                _sys.stderr.write(json.dumps({"error": str(e)}, indent=2) + "\n")
            sys.exit(1)

    async def _execute_singleshot_async(
        self,
        runner,
        prompt,
        output_schema,
        verbose,
        output
    ):
        """Async execution of singleshot command."""
        async with runner:
            # Create MCP server with ask_user tool for clarification
            mcp_server = RunMCPServer(runner, interactive=True)

            # Create and execute agent
            from llm_sandbox import Agent

            agent = Agent(
                runner=runner,
                prompt=prompt,
                output_schema=output_schema,
                mcp_server=mcp_server,
            )
            await agent.execute()
            return await agent.wait()

    def execute_interactive(self, **kwargs):
        """
        Execute interactive conversation mode.
        """
        keep_branch = kwargs.get("keep_branch", ())
        network = kwargs.get("network")
        verbose = kwargs.get("verbose", False)
        project_dir = kwargs.get("project_dir", Path.cwd())

        # Create output service
        output = create_output_service(format="text", verbose=verbose)

        # Load config
        config = load_config(project_dir)

        # Determine image tag
        image = kwargs.get("image")
        if image:
            image_tag = image
        elif config.image and config.image.image:
            image_tag = config.image.image
        else:
            image_tag = DEFAULT_IMAGE

        # Pull image if needed
        container_manager = ContainerManager()
        if not container_manager.image_exists(image_tag):
            pull_callback = create_image_pull_callback(output)
            container_manager.pull_image(image_tag, progress_callback=pull_callback)

        # Create runner
        runner = SandboxRunner(
            project_dir,
            config,
            verbose=verbose,
            keep_branches=list(keep_branch) if keep_branch else [],
            network=network,
        )

        # Wire up event handlers
        wire_up_runner_events(runner, output)

        # Display welcome message
        output.info("\n" + "=" * 60)
        output.success("Interactive Mode")
        output.info("=" * 60)
        output.info(f"Instance ID: {runner.instance_id}")
        output.info(f"Container ID: {runner.container_id[:12]}")
        output.info("\nYou can execute commands and ask questions.")
        output.info("Each command is independent (no conversation history).")
        output.info("\nType 'exit' or 'quit' to end the session.")
        output.info("=" * 60 + "\n")

        # Run interactive loop
        try:
            asyncio.run(self._execute_interactive_async(runner, output, verbose))
        except KeyboardInterrupt:
            output.info("\n\nSession interrupted by user.")
        except Exception as e:
            output.error(f"Execution failed: {e}")
            sys.exit(1)

    async def _execute_interactive_async(self, runner, output, verbose):
        """Async execution of interactive conversation."""
        async with runner:
            # Create MCP server without ask_user tool (already interactive via the loop)
            mcp_server = RunMCPServer(runner, interactive=False)

            # Start conversation loop
            while True:
                # Get user input
                try:
                    user_message = click.prompt("\nYou", type=str)
                except (EOFError, KeyboardInterrupt):
                    break

                # Skip empty messages
                if not user_message or not user_message.strip():
                    output.warning("Please enter a message.")
                    continue

                # Check for exit commands
                if user_message.lower() in ('exit', 'quit', 'q'):
                    output.info("\nEnding session. Goodbye!")
                    break

                # Use simple conversational schema
                output_schema = {
                    "type": "object",
                    "properties": {
                        "response": {
                            "type": "string",
                            "description": "Your response to the user"
                        }
                    },
                    "required": ["response"]
                }

                try:
                    # Create and execute agent for this turn
                    from llm_sandbox import Agent

                    agent = Agent(
                        runner=runner,
                        prompt=user_message,
                        output_schema=output_schema,
                        mcp_server=mcp_server,
                    )

                    await agent.execute()
                    result = await agent.wait()

                    # Display agent response
                    if isinstance(result, dict) and "response" in result:
                        output.success(f"\nAgent: {result['response']}")
                    else:
                        output.info(f"\nAgent: {json.dumps(result, indent=2)}")

                except Exception as e:
                    output.error(f"\nAgent error: {e}")
                    if verbose:
                        import traceback
                        output.verbose(traceback.format_exc())
                    output.info("You can continue with a new message or type 'exit' to quit.")
