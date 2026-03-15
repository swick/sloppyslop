"""Orchestrates the full LLM sandbox workflow."""

import asyncio
import hashlib
import shutil
import uuid
from dataclasses import dataclass
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
__all__ = ["SandboxRunner", "AgentConfig"]


class BackgroundTaskManager:
    """Manages lifecycle of background agent tasks with proper synchronization."""

    def __init__(self):
        """Initialize background task manager."""
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def spawn(self, agent_id: str, coro):
        """
        Spawn background task with proper tracking.

        Args:
            agent_id: Unique agent identifier
            coro: Coroutine to run as background task

        Returns:
            agent_id for tracking

        Raises:
            ValueError: If agent_id already exists
        """
        async with self._lock:
            if agent_id in self._tasks:
                raise ValueError(f"Agent {agent_id} already exists")
            task = asyncio.create_task(coro)
            self._tasks[agent_id] = task
        return agent_id

    async def wait_for(
        self,
        agent_ids: Optional[List[str]] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Wait for specific agents or all agents.

        Args:
            agent_ids: List of agent IDs to wait for (None = all)
            timeout: Optional timeout in seconds

        Returns:
            Dict mapping agent_id to result
        """
        async with self._lock:
            if agent_ids is None:
                agent_ids = list(self._tasks.keys())
            tasks = [self._tasks[aid] for aid in agent_ids if aid in self._tasks]

        # Wait outside lock to avoid deadlock
        if timeout:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout
            )
        else:
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Remove completed tasks
        async with self._lock:
            for agent_id in agent_ids:
                self._tasks.pop(agent_id, None)

        return dict(zip(agent_ids, results))

    async def cancel_all(self):
        """Cancel all background tasks without recursion."""
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()

        # Cancel all tasks but don't propagate cancel to children
        # to avoid recursion depth issues
        for task in tasks:
            if not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass  # Ignore errors during cancellation

        # Wait for all tasks to complete, suppressing CancelledError
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=5.0)
            except asyncio.TimeoutError:
                pass  # Some tasks didn't finish in time, that's ok
            except Exception:
                pass  # Ignore other errors during cleanup

    def get_running_agents(self) -> List[str]:
        """Get list of currently running agent IDs (non-async for compatibility)."""
        return list(self._tasks.keys())


@dataclass
class AgentConfig:
    """Configuration for a single agent in parallel execution."""

    prompt: str
    output_schema: Dict[str, Any]
    mcp_server: MCPServer
    agent_id: Optional[str] = None


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

        # Parallel execution support
        self._agent_llm_providers: Dict[str, LLMProvider] = {}
        # Initialize locks immediately (not in async setup) so they're always available
        self._git_lock = asyncio.Lock()
        self._worktrees_lock = asyncio.Lock()
        self._background_task_manager = BackgroundTaskManager()

        # Review feedback storage (for PR review workflow)
        # Type is List[FeedbackItem] but kept as List[Any] to avoid circular dependency
        self._review_feedback: List[Any] = []

        # Internal state
        self._keep_branches: List[str] = []
        self._network_mode: str = "none"
        self._cleaned_up: bool = False
        self._verbose: bool = False  # Current verbose setting for spawned agents

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async cleanup - properly awaits/cancels all tasks."""
        await self._cleanup_async()
        return False  # Don't suppress exceptions

    def __del__(self):
        """Destructor - ensure cleanup happens."""
        # Only do minimal cleanup - avoid calling cleanup() from __del__
        # as it can cause issues with event loops
        if hasattr(self, '_background_task_manager'):
            # Can't use await in __del__, just mark for cleanup
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

    async def _setup_git_symlink(self) -> None:
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
            exit_code, _, stderr = await self.container_manager.exec_command(
                self.container_id,
                f"mkdir -p {host_project_path}",
                workdir="/",
            )

            if exit_code != 0:
                click.echo(f"Warning: Failed to create directory structure for git symlink: {stderr}")
                return

            # Create symlink from host path to /project/.git
            exit_code, _, stderr = await self.container_manager.exec_command(
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

    async def setup(
        self,
        keep_branches: Optional[List[str]] = None,
        network: Optional[str] = None,
        image: Optional[str] = None,
    ) -> None:
        """
        Setup the sandbox environment: create worktrees dir, get image, start container.

        Args:
            keep_branches: List of branch names to keep (will be renamed from llm-container/{instance_id}/{name} to {name})
            network: Network mode override (optional)
            image: Image name/tag override (optional, uses config if not specified)
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
        if image:
            # Use provided image directly
            image_tag = image
        else:
            # Use image manager to get/build image from config
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
        await self._setup_git_symlink()

        # Step 4: Create LLM provider with system prompt
        base_system_prompt = self._create_base_system_prompt()
        self.llm_provider = create_llm_provider(
            self.provider_name,
            self.provider_config,
            base_system_prompt,
        )

        # Locks are already initialized in __init__()

    def _create_base_system_prompt(self) -> str:
        """
        Create the base system prompt describing the container environment.

        Returns:
            Base system prompt string
        """
        return """"""

    def _create_agent_llm_provider(self, agent_id: str) -> LLMProvider:
        """
        Create LLM provider for agent with isolated conversation.

        Args:
            agent_id: Unique agent identifier

        Returns:
            LLM provider instance
        """
        base_prompt = self._create_base_system_prompt()
        provider = create_llm_provider(
            self.provider_name, self.provider_config, base_prompt
        )
        self._agent_llm_providers[agent_id] = provider
        return provider

    async def _run_single_agent(
        self,
        agent: AgentConfig,
        verbose: bool
    ) -> Dict[str, Any]:
        """
        Run single agent using shared environment.

        Args:
            agent: Agent configuration
            verbose: Enable verbose output

        Returns:
            Agent execution result
        """
        agent_id = agent.agent_id or str(uuid.uuid4())[:8]

        if verbose:
            click.echo(f"\n[Agent {agent_id}] Starting execution...")

        # Create agent-specific LLM provider
        llm_provider = self._create_agent_llm_provider(agent_id)

        # Execute agent (uses shared instance_id, container_id, worktrees_base_dir)
        result = await llm_provider.generate_structured(
            agent.prompt,
            agent.mcp_server,
            agent.output_schema,
            verbose=verbose,
        )

        if verbose:
            click.echo(f"\n[Agent {agent_id}] Completed successfully")

        return result

    async def run_agents(
        self,
        agents: List[AgentConfig],
        verbose: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Execute agents in parallel using shared environment.

        All agents share:
        - Same container
        - Same instance_id
        - Same worktrees_base_dir
        - Same created_worktrees list (synchronized)

        Each agent gets:
        - Separate LLM conversation history
        - Separate MCP server (provided in AgentConfig)

        Must call setup() before calling this method.

        Args:
            agents: List of agent configurations
            verbose: Enable verbose output (optional)

        Returns:
            List of structured outputs from each agent
        """
        if not self.container_id or not self.instance_id:
            raise RuntimeError("Must call setup() before running agents")

        # Store verbose flag so spawned agents can inherit it
        self._verbose = verbose

        try:
            if verbose:
                click.echo(f"\n{'='*60}")
                click.echo(f"Running {len(agents)} agent(s) in parallel")
                click.echo(f"Shared environment:")
                click.echo(f"  - Container ID: {self.container_id[:12]}")
                click.echo(f"  - Instance ID: {self.instance_id}")
                click.echo(f"  - Worktrees dir: {self.worktrees_base_dir}")
                click.echo(f"{'='*60}")

            # Run all agents in parallel
            results = await asyncio.gather(*[
                self._run_single_agent(agent, verbose)
                for agent in agents
            ], return_exceptions=True)

            # Process results, convert exceptions to error dicts
            final_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    agent_id = agents[i].agent_id or f"agent-{i}"
                    if verbose:
                        click.echo(f"\n[Agent {agent_id}] Failed with error: {result}")
                    final_results.append({
                        "success": False,
                        "error": str(result),
                        "agent_id": agent_id,
                    })
                else:
                    final_results.append(result)

            return final_results

        finally:
            # Clear agent LLM providers
            self._agent_llm_providers.clear()
            # Reset verbose flag
            self._verbose = False
            # Don't cancel background agents here - let them run to completion
            # They'll be cleaned up by explicit cleanup() call if needed

    async def _cleanup_async(self) -> None:
        """
        Async cleanup - properly awaits/cancels all tasks.

        Safe to call multiple times.
        """
        # Skip if already cleaned up
        if self._cleaned_up:
            return

        self._cleaned_up = True

        # Cancel background agents with proper async handling
        if self._background_task_manager:
            running_agents = self._background_task_manager.get_running_agents()
            if running_agents:
                click.echo(f"Canceling {len(running_agents)} background agent(s)...")
            await self._background_task_manager.cancel_all()

        # Clear review feedback
        self._review_feedback.clear()

        # Cleanup container (sync is fine)
        if self.container_id:
            try:
                click.echo("Cleaning up container...")
                self.container_manager.cleanup(self.container_id)
            except Exception as e:
                click.echo(f"Warning: Failed to cleanup container: {e}")
            finally:
                self.container_id = None

        # Cleanup worktrees (sync)
        try:
            click.echo("Cleaning up worktrees...")
            self._cleanup_worktrees(self._keep_branches)
        except Exception as e:
            click.echo(f"Warning: Failed to cleanup worktrees: {e}")

    def cleanup(self) -> None:
        """
        Cleanup container and worktrees (sync wrapper for backwards compatibility).

        Safe to call multiple times.

        Note: Prefer using async context manager pattern for proper async cleanup.
        """
        # Skip if already cleaned up
        if self._cleaned_up:
            return

        self._cleaned_up = True

        # Cancel background agents (best effort, can't await in sync context)
        if self._background_task_manager:
            running_agents = self._background_task_manager.get_running_agents()
            if running_agents:
                click.echo(f"Warning: {len(running_agents)} background agent(s) still running - use async context manager for proper cleanup")

        # Clear review feedback
        self._review_feedback.clear()

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
