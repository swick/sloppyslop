"""Orchestrates the full LLM sandbox workflow."""

import asyncio
import hashlib
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_sandbox.config import Config, get_provider_config
from llm_sandbox.container import ContainerManager, Image
from llm_sandbox.events import EventEmitter
from llm_sandbox.git_ops import GitOperations
from llm_sandbox.llm_provider import LLMProvider, create_llm_provider
from llm_sandbox.mcp_tools import MCPServer


# SandboxRunner event types
@dataclass
class InstanceCreated:
    """Event: Instance ID generated."""

    instance_id: str
    timestamp: datetime


@dataclass
class ContainerStarted:
    """Event: Container started."""

    container_id: str
    image: str
    timestamp: datetime


@dataclass
class WorktreeCreated:
    """Event: Worktree created."""

    name: str
    path: str
    branch: str


@dataclass
class WorktreeRemoveFailed:
    """Event: Worktree removal failed."""

    name: str
    error: str


@dataclass
class BranchDeleted:
    """Event: Branch deleted."""

    branch_name: str


@dataclass
class BranchKept:
    """Event: Branch kept (renamed) during cleanup."""

    original_name: str
    new_name: str


@dataclass
class WarningIssued:
    """Event: Generic warning message."""

    message: str
    context: str = ""


@dataclass
class AgentStarted:
    """Event: Agent started execution."""

    agent_id: str


@dataclass
class AgentCompleted:
    """Event: Agent completed successfully."""

    agent_id: str


@dataclass
class AgentFailed:
    """Event: Agent failed with error."""

    agent_id: str
    error: str


@dataclass
class CleanupStarted:
    """Event: Cleanup phase started."""

    component: str  # 'container', 'worktrees', or 'background_agents'


@dataclass
class BackgroundAgentsCanceling:
    """Event: Background agents being canceled."""

    agent_count: int


@dataclass
class BackgroundAgentSpawned:
    """Event: Background agent spawned."""

    agent_id: str
    spawn_depth: int
    tool_count: int


@dataclass
class BackgroundAgentStarting:
    """Event: Background agent starting execution."""

    agent_id: str
    spawn_depth: int


@dataclass
class BackgroundAgentCompleted:
    """Event: Background agent completed successfully."""

    agent_id: str


@dataclass
class BackgroundAgentFailed:
    """Event: Background agent failed."""

    agent_id: str
    error: str


@dataclass
class BackgroundAgentsWaiting:
    """Event: Waiting for background agents to complete."""

    agent_ids: List[str]
    agent_count: int


@dataclass
class BackgroundAgentsAllCompleted:
    """Event: All background agents completed."""

    agent_count: int


# Re-export for convenience
__all__ = ["SandboxRunner", "Agent"]


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

    async def cancel_all(self) -> int:
        """
        Cancel all remaining agents.

        Returns:
            Number of agents that were canceled
        """
        async with self._lock:
            agent_count = len(self._tasks)
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

        return agent_count

    def get_running(self) -> List[str]:
        """Get list of currently running agent IDs (non-async for compatibility)."""
        return list(self._tasks.keys())


@dataclass
class Agent:
    """Represents an agent that can be executed in the sandbox."""

    def __init__(
        self,
        runner: "SandboxRunner",
        prompt: str,
        output_schema: Dict[str, Any],
        mcp_server: MCPServer,
        agent_id: Optional[str] = None
    ):
        """
        Initialize agent.

        Args:
            runner: SandboxRunner instance
            prompt: Task description for the agent
            output_schema: JSON schema for structured output
            mcp_server: MCP server instance with available tools
            agent_id: Optional agent identifier
        """
        self.runner = runner
        self.prompt = prompt
        self.output_schema = output_schema
        self.mcp_server = mcp_server
        self._agent_id = agent_id

    async def execute(self, background: bool = False) -> str:
        """
        Start agent execution.

        Args:
            background: If True, run in background. If False, caller should wait.

        Returns:
            agent_id for tracking

        Raises:
            RuntimeError: If agent already started or runner not setup
        """
        if self._agent_id is not None:
            raise RuntimeError("Agent already started")

        # Validate runner is setup
        if not self.runner.container_id or not self.runner.instance_id:
            raise RuntimeError("Runner not properly initialized")

        # Generate agent_id if not provided
        if not self._agent_id:
            import uuid
            self._agent_id = f"bg-{str(uuid.uuid4())[:8]}" if background else str(uuid.uuid4())[:8]

        # Create execution coroutine
        coro = self._execute(background)

        # Spawn via task manager (tracks both foreground and background)
        await self.runner._background_task_manager.spawn(self._agent_id, coro)

        # Emit spawned event for background agents
        if background:
            spawn_depth = self.mcp_server.spawn_depth if self.mcp_server else 0
            tool_count = len(self.mcp_server.tools) if self.mcp_server else 0
            self.runner.events.emit(BackgroundAgentSpawned(
                agent_id=self._agent_id,
                spawn_depth=spawn_depth,
                tool_count=tool_count
            ))

        return self._agent_id

    async def _execute(self, background: bool) -> Dict[str, Any]:
        """
        Execute the agent with proper event handling.

        Args:
            background: Whether this is a background agent

        Returns:
            Agent execution result
        """
        try:
            # Emit appropriate start event
            if background:
                spawn_depth = self.mcp_server.spawn_depth if self.mcp_server else 0
                self.runner.events.emit(BackgroundAgentStarting(
                    agent_id=self._agent_id,
                    spawn_depth=spawn_depth
                ))
            else:
                self.runner.events.emit(AgentStarted(agent_id=self._agent_id))

            # Create agent-specific LLM provider
            llm_provider = self.runner._create_agent_llm_provider(self._agent_id)

            # Execute agent (uses shared instance_id, container_id, worktrees_base_dir)
            result = await llm_provider.generate_structured(
                self.prompt,
                self.mcp_server,
                self.output_schema,
                verbose=self.runner._verbose,
            )

            # Emit completion event
            if background:
                self.runner.events.emit(BackgroundAgentCompleted(agent_id=self._agent_id))
            else:
                self.runner.events.emit(AgentCompleted(agent_id=self._agent_id))

            return result

        except Exception as e:
            # Emit failure event
            if background:
                self.runner.events.emit(BackgroundAgentFailed(agent_id=self._agent_id, error=str(e)))
            else:
                self.runner.events.emit(AgentFailed(agent_id=self._agent_id, error=str(e)))
            raise

    async def wait(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """
        Wait for agent to complete and return result.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            Agent execution result

        Raises:
            RuntimeError: If execute() not called yet
        """
        if self._agent_id is None:
            raise RuntimeError("Agent not started. Call execute() first.")

        # Wait for this specific agent to complete
        results = await self.runner._background_task_manager.wait_for(
            agent_ids=[self._agent_id],
            timeout=timeout
        )
        result = results[self._agent_id]

        if isinstance(result, Exception):
            raise result

        return result

    @property
    def agent_id(self) -> Optional[str]:
        """Get the agent ID (None if not started yet)."""
        return self._agent_id


class SandboxRunner:
    """Orchestrates one-shot LLM prompt execution in sandbox."""

    def __init__(
        self,
        project_path: Path,
        config: Config,
        verbose: bool = False,
        keep_branches: Optional[List[str]] = None,
        network: Optional[str] = None,
        image: Optional[str] = None,
    ):
        """
        Initialize sandbox runner and setup environment.

        Args:
            project_path: Path to project directory
            config: Merged configuration (global + project overrides)
            verbose: Enable verbose output for all agents
            keep_branches: List of branch names to keep (will be renamed from llm-container/{instance_id}/{name} to {name})
            network: Network mode override (optional, uses config if not specified)
            image: Image name/tag override (optional, uses config if not specified)
        """
        self.project_path = project_path
        self.config = config

        # Get provider config
        self.provider_name, self.provider_config = get_provider_config(config)

        # Event emitter for progress and status updates
        self.events = EventEmitter()

        # Public API - Components for tool access
        self.container_manager = ContainerManager()
        self.git_ops = GitOperations(project_path)

        # Public API - Instance state
        self.created_worktrees: List[str] = []  # Track worktree names

        # Parallel execution support
        self._agent_llm_providers: Dict[str, LLMProvider] = {}
        # Initialize locks immediately so they're always available
        self._git_lock = asyncio.Lock()
        self._worktrees_lock = asyncio.Lock()
        self._background_task_manager = BackgroundTaskManager()

        # Review feedback storage (for PR review workflow)
        # Type is List[FeedbackItem] but kept as List[Any] to avoid circular dependency
        self._review_feedback: List[Any] = []

        # Internal state
        self._cleaned_up: bool = False
        self._verbose: bool = verbose  # Verbose setting for all spawned agents

        # Setup sandbox environment
        if keep_branches is None:
            keep_branches = []
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
        self.events.emit(InstanceCreated(
            instance_id=self.instance_id,
            timestamp=datetime.now()
        ))

        # Step 2: Get container image tag
        if image:
            # Use provided image directly
            image_tag = image
        else:
            # Use image from config or default
            image_tag = self.config.image.image if self.config.image else "registry.fedoraproject.org/fedora-toolbox:44"

        # Step 3: Create and start container
        container_info = self.container_manager.create_container(
            image_tag,
            self.project_path,
            self.worktrees_base_dir,
            self._network_mode,
        )
        self.container_id = container_info.container_id

        self.container_manager.start_container(self.container_id)
        self.events.emit(ContainerStarted(
            container_id=self.container_id,
            image=container_info.image,
            timestamp=datetime.now()
        ))

        # Create symlink in container so worktree .git files work
        self._setup_git_symlink()

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
            exit_code, _, stderr = asyncio.run(self.container_manager.exec_command(
                self.container_id,
                f"mkdir -p {host_project_path}",
                workdir="/",
            ))

            if exit_code != 0:
                self.events.emit(WarningIssued(
                    message=f"Failed to create directory structure for git symlink: {stderr}",
                    context="setup_git_symlink"
                ))
                return

            # Create symlink from host path to /project/.git
            exit_code, _, stderr = asyncio.run(self.container_manager.exec_command(
                self.container_id,
                f"ln -sf /project/.git {host_project_path}/.git",
                workdir="/",
            ))

            if exit_code != 0:
                self.events.emit(WarningIssued(
                    message=f"Failed to create git symlink: {stderr}",
                    context="setup_git_symlink"
                ))

        except Exception as e:
            self.events.emit(WarningIssued(
                message=f"Failed to setup git symlink: {e}",
                context="setup_git_symlink"
            ))

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
                        self.events.emit(WarningIssued(
                            message=f"Branch {full_branch_name} does not exist, skipping",
                            context="cleanup_worktrees"
                        ))
                        continue

                    # Create a copy of the branch with the new name (force overwrite if exists)
                    self.git_ops.repo.git.branch("-f", branch_name, full_branch_name)
                    self.events.emit(BranchKept(
                        original_name=full_branch_name,
                        new_name=branch_name
                    ))

                except Exception as e:
                    self.events.emit(WarningIssued(
                        message=f"Failed to copy branch {full_branch_name}: {e}",
                        context="cleanup_worktrees"
                    ))
            else:
                self.events.emit(WarningIssued(
                    message=f"Branch {branch_name} was not created in this session",
                    context="cleanup_worktrees"
                ))

        # Step 2: Now remove all worktrees
        for worktree_name in self.created_worktrees:
            worktree_path = self.worktrees_base_dir / worktree_name
            if worktree_path.exists():
                try:
                    self.git_ops.remove_worktree(worktree_path)
                except Exception as e:
                    self.events.emit(WorktreeRemoveFailed(
                        name=worktree_name,
                        error=str(e)
                    ))

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
                    self.git_ops.delete_branch(branch_name)
                    self.events.emit(BranchDeleted(branch_name=branch_name))
                except Exception as e:
                    self.events.emit(WarningIssued(
                        message=f"Failed to delete branch {branch_name}: {e}",
                        context="cleanup_worktrees"
                    ))
        except Exception as e:
            self.events.emit(WarningIssued(
                message=f"Failed to list remaining branches: {e}",
                context="cleanup_worktrees"
            ))

        # Remove instance directory
        if self.worktrees_base_dir.exists():
            try:
                shutil.rmtree(self.worktrees_base_dir)
            except Exception as e:
                self.events.emit(WarningIssued(
                    message=f"Failed to remove instance directory: {e}",
                    context="cleanup_worktrees"
                ))

    def _create_agent_llm_provider(self, agent_id: str) -> LLMProvider:
        """
        Create LLM provider for agent with isolated conversation.

        Args:
            agent_id: Unique agent identifier

        Returns:
            LLM provider instance
        """
        provider = create_llm_provider(self.provider_name, self.provider_config)
        self._agent_llm_providers[agent_id] = provider
        return provider

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
            canceled_count = await self._background_task_manager.cancel_all()
            if canceled_count > 0:
                self.events.emit(BackgroundAgentsCanceling(agent_count=canceled_count))

        # Clear review feedback
        self._review_feedback.clear()

        # Cleanup container (sync is fine)
        if self.container_id:
            try:
                self.events.emit(CleanupStarted(component='container'))
                self.container_manager.cleanup(self.container_id)
            except Exception as e:
                self.events.emit(WarningIssued(
                    message=f"Failed to cleanup container: {e}",
                    context="cleanup"
                ))
            finally:
                self.container_id = None

        # Cleanup worktrees (sync)
        try:
            self.events.emit(CleanupStarted(component='worktrees'))
            self._cleanup_worktrees(self._keep_branches)
        except Exception as e:
            self.events.emit(WarningIssued(
                message=f"Failed to cleanup worktrees: {e}",
                context="cleanup"
            ))