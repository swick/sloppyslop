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
from llm_sandbox.mcp_tools import MCPServer

# Re-export for convenience
__all__ = ["SandboxRunner"]


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

        # Get provider config
        self.provider_name, self.provider_config = get_provider_config(config)

        # Public API - Components for tool access
        self.container_manager = ContainerManager()
        self.git_ops = GitOperations(project_path)

        # Public API - Instance state (available after setup())
        self.instance_id: Optional[str] = None
        self.container_id: Optional[str] = None
        self.worktrees_base_dir: Optional[Path] = None
        self.created_worktrees: List[str] = []  # Track worktree names
        self.llm_provider: Optional[LLMProvider] = None

        # Internal state
        self._keep_branches: List[str] = []
        self._network_mode: str = "none"
        self._cleaned_up: bool = False

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

    def _setup_git_symlink(self) -> None:
        """
        Create symlink in container so worktree .git files work correctly.

        Worktree .git files contain: gitdir: /host/path/to/project/.git/worktrees/name
        Inside container, project is at /project, not /host/path/to/project

        Solution: Create symlink /host/path/to/project/.git -> /project/.git
        This way git can follow the path in the .git file.
        """
        if not self.container_id:
            return

        try:
            # Get the absolute path to the project on the host
            host_project_path = str(self.project_path.absolute())

            # Create the project directory structure in the container
            # (everything up to but not including .git)
            exit_code, _, stderr = self.container_manager.exec_command(
                self.container_id,
                f"mkdir -p {host_project_path}",
                workdir="/",
            )

            if exit_code != 0:
                click.echo(f"Warning: Failed to create directory structure for git symlink: {stderr}")
                return

            # Create symlink from host path to /project/.git
            exit_code, _, stderr = self.container_manager.exec_command(
                self.container_id,
                f"ln -sf /project/.git {host_project_path}/.git",
                workdir="/",
            )

            if exit_code != 0:
                click.echo(f"Warning: Failed to create git symlink: {stderr}")

        except Exception as e:
            click.echo(f"Warning: Failed to setup git symlink: {e}")

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

        # Step 1: Copy kept branches to new names BEFORE removing worktrees
        # We create a copy with the target name, then later delete the original
        for branch_name in keep_branches:
            full_branch_name = f"llm-container/{self.instance_id}/{branch_name}"
            if branch_name in self.created_worktrees:
                try:
                    # Check if the branch actually exists
                    branch_exists = False
                    try:
                        self.git_ops.repo.git.rev_parse("--verify", f"refs/heads/{full_branch_name}")
                        branch_exists = True
                    except Exception:
                        pass

                    if not branch_exists:
                        click.echo(f"Warning: Branch {full_branch_name} does not exist, skipping")
                        continue

                    click.echo(f"Keeping branch: {full_branch_name} → {branch_name}")
                    # Create a copy of the branch with the new name (force overwrite if exists)
                    self.git_ops.repo.git.branch("-f", branch_name, full_branch_name)

                except Exception as e:
                    click.echo(f"Warning: Failed to copy branch {full_branch_name}: {e}")
            else:
                click.echo(f"Warning: Branch {branch_name} was not created in this session")

        # Step 2: Now remove all worktrees
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
        self._keep_branches = keep_branches

        # Use network from config if not specified
        if network is None:
            network = self.config.container.network

        # Convert network setting to podman format
        self._network_mode = "none" if network == "isolated" else "bridge"

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
            self._network_mode,
        )

        self.container_manager.start_container(self.container_id)
        click.echo(f"Container started: {self.container_id[:12]}")

        # Create symlink in container so worktree .git files work
        # The .git file in a worktree contains: gitdir: /host/path/.git/worktrees/name
        # We create a symlink: /host/path/.git -> /project/.git
        self._setup_git_symlink()

        # Step 4: Create LLM provider
        self.llm_provider = create_llm_provider(
            self.provider_name,
            self.provider_config,
        )

    def run_agent(
        self,
        prompt: str,
        output_schema: Dict[str, Any],
        mcp_server: MCPServer,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute LLM agent with tools and structured output.

        Must call setup() before calling this method.

        Args:
            prompt: User prompt for LLM
            output_schema: JSON schema for structured output
            mcp_server: MCP server providing tools for the LLM
            verbose: Enable verbose output (optional)

        Returns:
            Structured output from LLM
        """
        if not self.container_id or not self.instance_id or not self.llm_provider:
            raise RuntimeError("Must call setup() before run_agent()")

        # Execute LLM agent with tools
        click.echo("Executing LLM agent...")

        result = self.llm_provider.generate_structured(
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
        # Skip if already cleaned up
        if self._cleaned_up:
            return

        self._cleaned_up = True

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
            self._cleanup_worktrees(self._keep_branches)
        except Exception as e:
            click.echo(f"Warning: Failed to cleanup worktrees: {e}")
