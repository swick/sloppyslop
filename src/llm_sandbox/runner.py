"""Orchestrates the full LLM sandbox workflow."""

import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from llm_sandbox.config import Config, get_provider_config
from llm_sandbox.container import ContainerManager
from llm_sandbox.git_ops import GitOperations
from llm_sandbox.image import Image
from llm_sandbox.llm_provider import LLMProvider, create_llm_provider
from llm_sandbox.mcp_tools import (
    MCPServer,
    ExecuteCommandTool,
    GitCommitTool,
    CheckoutCommitTool,
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadProjectFileTool,
    ListProjectDirectoryTool,
)


class ContainerMCPServer(MCPServer):
    """MCP server for container and git operations."""

    def __init__(
        self,
        container_manager: ContainerManager,
        container_id: str,
        instance_id: str,
        runner: "SandboxRunner",
        project_path: Path,
        custom_tools: Optional[List] = None,
    ):
        """
        Initialize container MCP server.

        Args:
            container_manager: Container manager instance
            container_id: Container ID to execute commands in
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for tracking worktrees
            project_path: Path to project directory for read-only access
            custom_tools: Optional list of custom MCP tools to add
        """
        super().__init__()
        execute_command_tool = ExecuteCommandTool(container_manager, container_id)
        checkout_commit_tool = CheckoutCommitTool(
            container_manager, container_id, instance_id, runner
        )
        git_commit_tool = GitCommitTool(
            container_manager, container_id, instance_id, runner
        )
        read_file_tool = ReadFileTool(instance_id, runner)
        write_file_tool = WriteFileTool(instance_id, runner)
        edit_file_tool = EditFileTool(instance_id, runner)
        glob_tool = GlobTool(instance_id, runner)
        grep_tool = GrepTool(instance_id, runner)
        read_project_file_tool = ReadProjectFileTool(project_path)
        list_project_directory_tool = ListProjectDirectoryTool(project_path)

        self.tools = {
            execute_command_tool.name: execute_command_tool,
            checkout_commit_tool.name: checkout_commit_tool,
            git_commit_tool.name: git_commit_tool,
            read_file_tool.name: read_file_tool,
            write_file_tool.name: write_file_tool,
            edit_file_tool.name: edit_file_tool,
            glob_tool.name: glob_tool,
            grep_tool.name: grep_tool,
            read_project_file_tool.name: read_project_file_tool,
            list_project_directory_tool.name: list_project_directory_tool,
        }

        # Add custom tools if provided
        if custom_tools:
            for tool in custom_tools:
                self.tools[tool.name] = tool


class SandboxRunner:
    """Orchestrates one-shot LLM prompt execution in sandbox."""

    def __init__(self, project_path: Path, config: Config):
        """
        Initialize sandbox runner.

        Args:
            project_path: Path to project directory
            config: Merged configuration (global + project overrides)
        """
        self.project_path = project_path
        self.config = config

        # Initialize components
        self.container_manager = ContainerManager()
        self.git_ops = GitOperations(project_path)

        # Get provider config
        self.provider_name, self.provider_config = get_provider_config(config)

        # Instance tracking for dynamic worktrees
        self.instance_id: Optional[str] = None
        self.worktrees_base_dir: Optional[Path] = None
        self.created_worktrees: List[str] = []  # Track worktree names

        # Runtime state
        self.container_id: Optional[str] = None
        self.keep_branches: List[str] = []
        self.network_mode: str = "none"

    def __del__(self):
        """Destructor - ensure cleanup happens."""
        try:
            self.cleanup()
        except Exception:
            # Ignore errors during destruction
            pass

    def _generate_instance_id(self) -> str:
        """
        Generate unique instance ID (timestamp + UUID).

        Returns:
            Instance ID string (format: YYYYMMDD-HHMMSS-uuid)
        """
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short_uuid = str(uuid.uuid4())[:8]
        return f"{timestamp}-{short_uuid}"

    def _cleanup_worktrees(self, keep_branches: List[str]) -> None:
        """
        Remove worktrees and cleanup branches.

        For branches to keep: rename from llm-container/{instance_id}/{name} to {name}
        All remaining llm-container/{instance_id}/* branches are deleted
        All worktrees are removed

        Args:
            keep_branches: List of branch names to keep (without llm-container prefix)
        """
        if not self.worktrees_base_dir or not self.instance_id:
            return

        # Build branch mapping: branch_name -> full_branch_name
        branch_mapping = {}
        for worktree_name in self.created_worktrees:
            branch_name = f"llm-container/{self.instance_id}/{worktree_name}"
            branch_mapping[worktree_name] = branch_name

        # Rename kept branches (remove llm-container/{instance_id}/ prefix)
        for branch_name in keep_branches:
            full_branch_name = f"llm-container/{self.instance_id}/{branch_name}"
            if branch_name in self.created_worktrees:
                try:
                    click.echo(f"Keeping branch: {full_branch_name} → {branch_name}")
                    # Rename branch by creating new branch at same commit and deleting old one
                    # Use -f to force overwrite if target branch already exists
                    self.git_ops.repo.git.branch("-f", branch_name, full_branch_name)
                    self.git_ops.delete_branch(full_branch_name)
                except Exception as e:
                    click.echo(f"Warning: Failed to rename branch {full_branch_name}: {e}")
            else:
                click.echo(f"Warning: Branch {branch_name} was not created in this session")

        # Remove all worktrees
        for worktree_name in self.created_worktrees:
            worktree_path = self.worktrees_base_dir / worktree_name
            if worktree_path.exists():
                try:
                    self.git_ops.remove_worktree(worktree_path)
                except Exception as e:
                    click.echo(f"Warning: Failed to remove worktree {worktree_name}: {e}")

        # Delete ALL remaining llm-container/{instance_id}/* branches
        instance_prefix = f"llm-container/{self.instance_id}/"
        try:
            remaining_branches = [
                ref.name
                for ref in self.git_ops.repo.refs
                if ref.name.startswith(instance_prefix)
            ]
            for branch_name in remaining_branches:
                try:
                    click.echo(f"Deleting temporary branch: {branch_name}")
                    self.git_ops.delete_branch(branch_name)
                except Exception as e:
                    click.echo(f"Warning: Failed to delete branch {branch_name}: {e}")
        except Exception as e:
            click.echo(f"Warning: Failed to list remaining branches: {e}")

        # Remove instance directory
        if self.worktrees_base_dir.exists():
            try:
                shutil.rmtree(self.worktrees_base_dir)
            except Exception as e:
                click.echo(f"Warning: Failed to remove instance directory: {e}")

    def setup(
        self,
        keep_branches: Optional[List[str]] = None,
        network: Optional[str] = None,
    ) -> None:
        """
        Setup the sandbox environment: create worktrees dir, get image, start container.

        Args:
            keep_branches: List of branch names to keep (will be renamed from llm-container/{instance_id}/{name} to {name})
            network: Network mode override (optional)
        """
        if keep_branches is None:
            keep_branches = []

        # Store for cleanup
        self.keep_branches = keep_branches

        # Use network from config if not specified
        if network is None:
            network = self.config.container.network

        # Convert network setting to podman format
        self.network_mode = "none" if network == "isolated" else "bridge"

        # Step 1: Generate instance ID and create empty worktrees directory
        self.instance_id = self._generate_instance_id()
        self.worktrees_base_dir = (
            self.project_path / ".llm-sandbox" / "worktrees" / self.instance_id
        )
        self.worktrees_base_dir.mkdir(parents=True, exist_ok=True)
        click.echo(f"Instance ID: {self.instance_id}")

        # Step 2: Get container image (build if necessary)
        image_manager = Image(
            self.config.image,
            self.project_path,
            self.container_manager,
        )
        image_tag = image_manager.get_image()

        # Step 3: Create and start container
        self.container_id = self.container_manager.create_container(
            image_tag,
            self.project_path,
            self.worktrees_base_dir,
            self.network_mode,
        )

        self.container_manager.start_container(self.container_id)
        click.echo(f"Container started: {self.container_id[:12]}")

    def run_prompt(
        self,
        prompt: str,
        output_schema: Dict[str, Any],
        verbose: bool = False,
        custom_tools: Optional[List] = None,
    ) -> Dict[str, Any]:
        """
        Execute LLM prompt with structured output.

        Must call setup() before calling this method.

        Args:
            prompt: User prompt for LLM
            output_schema: JSON schema for structured output
            verbose: Enable verbose output (optional)
            custom_tools: Optional list of custom MCP tools to add (optional)

        Returns:
            Structured output from LLM
        """
        if not self.container_id or not self.instance_id:
            raise RuntimeError("Must call setup() before run_prompt()")

        # Initialize MCP server with custom tools
        mcp_server = ContainerMCPServer(
            self.container_manager,
            self.container_id,
            self.instance_id,
            self,
            self.project_path,
            custom_tools=custom_tools,
        )

        # Execute LLM prompt with structured output
        click.echo("Executing LLM prompt...")

        llm_provider = create_llm_provider(
            self.provider_name,
            self.provider_config,
        )

        result = llm_provider.generate_structured(
            prompt,
            mcp_server,
            output_schema,
            verbose=verbose,
        )

        return result

    def cleanup(self) -> None:
        """
        Cleanup container and worktrees.

        Safe to call multiple times.
        """
        # Cleanup container
        if self.container_id:
            try:
                click.echo("Cleaning up container...")
                self.container_manager.cleanup(self.container_id)
            except Exception as e:
                click.echo(f"Warning: Failed to cleanup container: {e}")
            finally:
                self.container_id = None

        # Cleanup worktrees
        try:
            click.echo("Cleaning up worktrees...")
            self._cleanup_worktrees(self.keep_branches)
        except Exception as e:
            click.echo(f"Warning: Failed to cleanup worktrees: {e}")
