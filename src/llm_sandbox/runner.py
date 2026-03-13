"""Orchestrates the full LLM sandbox workflow."""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_sandbox.config import GlobalConfig, ProjectConfig, get_provider_config
from llm_sandbox.container import ContainerManager
from llm_sandbox.git_ops import GitOperations
from llm_sandbox.llm_provider import LLMProvider, create_llm_provider
from llm_sandbox.mcp_tools import MCPServer, ExecuteCommandTool, GitCommitTool
from llm_sandbox.worktree import WorktreeManager


class ContainerMCPServer(MCPServer):
    """MCP server for container and git operations."""

    def __init__(self, container_manager: ContainerManager, container_id: str):
        """
        Initialize container MCP server.

        Args:
            container_manager: Container manager instance
            container_id: Container ID to execute commands in
        """
        super().__init__()
        execute_command_tool = ExecuteCommandTool(container_manager, container_id)
        git_commit_tool = GitCommitTool(container_manager, container_id)
        self.tools = {
            execute_command_tool.name: execute_command_tool,
            git_commit_tool.name: git_commit_tool,
        }


class SandboxRunner:
    """Orchestrates one-shot LLM prompt execution in sandbox."""

    def __init__(self, project_path: Path, global_config: GlobalConfig, project_config: ProjectConfig):
        """
        Initialize sandbox runner.

        Args:
            project_path: Path to project directory
            global_config: Global configuration
            project_config: Project configuration
        """
        self.project_path = project_path
        self.global_config = global_config
        self.project_config = project_config

        # Initialize components
        self.container_manager = ContainerManager()
        self.worktree_manager = WorktreeManager(project_path)
        self.git_ops = GitOperations(project_path)

        # Get provider config
        self.provider_name, self.provider_config = get_provider_config(global_config)

    def run_prompt(
        self,
        commit: str,
        prompt: str,
        output_schema: Dict[str, Any],
        branches_to_pull: Optional[List[str]] = None,
        network: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute one-shot LLM prompt with structured output.

        Args:
            commit: Git commit/branch/tag to use as base
            prompt: User prompt for LLM
            output_schema: JSON schema for structured output
            branches_to_pull: List of branches to pull from worktree (optional)
            network: Network mode override (optional)

        Returns:
            Structured output from LLM
        """
        if branches_to_pull is None:
            branches_to_pull = []

        # Use network from config if not specified
        if network is None:
            network = self.global_config.container.network

        # Convert network setting to podman format
        network_mode = "none" if network == "isolated" else "bridge"

        container_id = None
        worktree_path = None

        try:
            # Step 1: Create worktree from commit
            commit_hash = self.worktree_manager.get_commit_hash(commit)
            worktree_dir = self.project_path / ".llm-sandbox" / "worktrees" / commit_hash
            worktree_path = self.worktree_manager.create_worktree(commit, worktree_dir)

            # Step 2: Build/use cached container image
            image_tag = self.project_config.image_tag
            # Containerfile path is relative to project directory
            containerfile_path = self.project_path / self.project_config.containerfile

            if not self.container_manager.image_exists(image_tag):
                print(f"Building container image: {image_tag}")
                self.container_manager.build_image(
                    containerfile_path,
                    self.project_path,
                    image_tag,
                )
            else:
                print(f"Using cached image: {image_tag}")

            # Step 3: Create and start container
            container_id = self.container_manager.create_container(
                image_tag,
                self.project_path,
                worktree_path,
                network_mode,
            )

            self.container_manager.start_container(container_id)
            print(f"Container started: {container_id[:12]}")

            # Step 4: Initialize MCP server
            mcp_server = ContainerMCPServer(self.container_manager, container_id)

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
            )

            # Step 6: Pull specified branches from worktree to main repo
            if branches_to_pull:
                print(f"Pulling branches: {', '.join(branches_to_pull)}")
                self.git_ops.pull_branches(
                    worktree_path,
                    branches_to_pull,
                    self.project_path,
                )

            return result

        finally:
            # Step 7: Cleanup
            if container_id:
                print("Cleaning up container...")
                self.container_manager.cleanup(container_id)

            if worktree_path:
                print("Cleaning up worktree...")
                self.worktree_manager.remove_worktree(worktree_path)
