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
from llm_sandbox.mcp_tools import MCPServer, ExecuteCommandTool, GitCommitTool, CheckoutCommitTool


class ContainerMCPServer(MCPServer):
    """MCP server for container and git operations."""

    def __init__(
        self,
        container_manager: ContainerManager,
        container_id: str,
        instance_id: str,
        runner: "SandboxRunner",
    ):
        """
        Initialize container MCP server.

        Args:
            container_manager: Container manager instance
            container_id: Container ID to execute commands in
            instance_id: Unique instance ID for this run
            runner: Reference to SandboxRunner for tracking worktrees
        """
        super().__init__()
        execute_command_tool = ExecuteCommandTool(container_manager, container_id)
        checkout_commit_tool = CheckoutCommitTool(
            container_manager, container_id, instance_id, runner
        )
        git_commit_tool = GitCommitTool(
            container_manager, container_id, instance_id, runner
        )
        self.tools = {
            execute_command_tool.name: execute_command_tool,
            checkout_commit_tool.name: checkout_commit_tool,
            git_commit_tool.name: git_commit_tool,
        }


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

    def run_prompt(
        self,
        prompt: str,
        output_schema: Dict[str, Any],
        keep_branches: Optional[List[str]] = None,
        network: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute one-shot LLM prompt with structured output.

        Args:
            prompt: User prompt for LLM
            output_schema: JSON schema for structured output
            keep_branches: List of branch names to keep (will be renamed from llm-container/{instance_id}/{name} to {name})
            network: Network mode override (optional)
            verbose: Enable verbose output (optional)

        Returns:
            Structured output from LLM
        """
        if keep_branches is None:
            keep_branches = []

        # Use network from config if not specified
        if network is None:
            network = self.config.container.network

        # Convert network setting to podman format
        network_mode = "none" if network == "isolated" else "bridge"

        container_id = None

        try:
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
            container_id = self.container_manager.create_container(
                image_tag,
                self.project_path,
                self.worktrees_base_dir,
                network_mode,
            )

            self.container_manager.start_container(container_id)
            click.echo(f"Container started: {container_id[:12]}")

            # Step 4: Initialize MCP server
            mcp_server = ContainerMCPServer(
                self.container_manager, container_id, self.instance_id, self
            )

            # Step 5: Execute LLM prompt with structured output
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

        finally:
            # Step 6: Cleanup
            if container_id:
                click.echo("Cleaning up container...")
                self.container_manager.cleanup(container_id)

            click.echo("Cleaning up worktrees...")
            self._cleanup_worktrees(keep_branches)
