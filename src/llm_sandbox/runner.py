"""Orchestrates the full LLM sandbox workflow."""

import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    def _cleanup_worktrees(self, output_branches: List[str]) -> None:
        """
        Remove worktrees and delete non-output branches.

        For each worktree created:
        - If name in output_branches: keep branch (already in main repo)
        - If not: delete branch
        Remove all worktrees

        Note: Branches are already in the main repository because
        create_worktree_on_branch() creates them there. No pulling needed.

        Args:
            output_branches: List of worktree names to keep as output branches
        """
        if not self.worktrees_base_dir or not self.instance_id:
            return

        # Build branch mapping: worktree_name -> branch_name
        branch_mapping = {}
        for worktree_name in self.created_worktrees:
            branch_name = f"llm-container/{self.instance_id}/{worktree_name}"
            branch_mapping[worktree_name] = branch_name

        # Print which branches are being kept
        for worktree_name in output_branches:
            if worktree_name in branch_mapping:
                branch_name = branch_mapping[worktree_name]
                print(f"Keeping output branch: {branch_name}")

        # Remove all worktrees
        for worktree_name in self.created_worktrees:
            worktree_path = self.worktrees_base_dir / worktree_name
            if worktree_path.exists():
                try:
                    self.git_ops.remove_worktree(worktree_path)
                except Exception as e:
                    print(f"Warning: Failed to remove worktree {worktree_name}: {e}")

        # Delete non-output branches
        for worktree_name, branch_name in branch_mapping.items():
            if worktree_name not in output_branches:
                try:
                    print(f"Deleting temporary branch: {branch_name}")
                    self.git_ops.delete_branch(branch_name)
                except Exception as e:
                    print(f"Warning: Failed to delete branch {branch_name}: {e}")

        # Remove instance directory
        if self.worktrees_base_dir.exists():
            try:
                shutil.rmtree(self.worktrees_base_dir)
            except Exception as e:
                print(f"Warning: Failed to remove instance directory: {e}")

    def run_prompt(
        self,
        commit: str,
        prompt: str,
        output_schema: Dict[str, Any],
        branches_to_pull: Optional[List[str]] = None,
        network: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute one-shot LLM prompt with structured output.

        Args:
            commit: Git commit/branch/tag to use as base (deprecated, not used in new architecture)
            prompt: User prompt for LLM
            output_schema: JSON schema for structured output
            branches_to_pull: List of worktree names to keep as output branches (optional)
            network: Network mode override (optional)
            verbose: Enable verbose output (optional)

        Returns:
            Structured output from LLM
        """
        if branches_to_pull is None:
            branches_to_pull = []

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
            print(f"Instance ID: {self.instance_id}")

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
            print(f"Container started: {container_id[:12]}")

            # Step 4: Initialize MCP server
            mcp_server = ContainerMCPServer(
                self.container_manager, container_id, self.instance_id, self
            )

            # Step 5: Execute LLM prompt with structured output
            print("Executing LLM prompt...")

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
                print("Cleaning up container...")
                self.container_manager.cleanup(container_id)

            print("Cleaning up worktrees...")
            self._cleanup_worktrees(branches_to_pull)
