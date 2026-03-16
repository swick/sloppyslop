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
from llm_sandbox.container import ContainerManager, DEFAULT_IMAGE, Image
from llm_sandbox.events import EventEmitter
from llm_sandbox.git_ops import GitOperations
from llm_sandbox.llm_provider import LLMProvider, create_llm_provider
from llm_sandbox.mcp_tools import MCPServer


# SandboxRunner event types
@dataclass
class BranchKept:
    """Event: Branch kept (renamed) during cleanup."""

    original_name: str
    new_name: str


@dataclass
class AgentStarted:
    """Event: Agent started execution."""

    agent: "Agent"


@dataclass
class AgentCompleted:
    """Event: Agent completed successfully."""

    agent: "Agent"


@dataclass
class AgentFailed:
    """Event: Agent failed with error."""

    agent: "Agent"
    error: str


@dataclass
class AgentCancelled:
    """Event: Agent cancelled during cleanup."""

    agent: "Agent"


@dataclass
class Warning:
    """Event: Warning message."""

    message: str
    context: str = ""


# Re-export for convenience
__all__ = ["SandboxRunner", "Agent"]


@dataclass
class Agent:
    """Represents an agent that can be executed in the sandbox."""

    MAX_SPAWN_DEPTH = 3  # Maximum agent nesting depth to prevent infinite chains

    def __init__(
        self,
        runner: "SandboxRunner",
        prompt: str,
        output_schema: Dict[str, Any],
        mcp_server: MCPServer,
        agent_id: Optional[str] = None,
        parent: Optional["Agent"] = None
    ):
        """
        Initialize agent.

        Args:
            runner: SandboxRunner instance
            prompt: Task description for the agent
            output_schema: JSON schema for structured output
            mcp_server: MCP server instance with available tools
            agent_id: Optional agent identifier
            parent: Optional parent agent (for spawned child agents)

        Raises:
            RuntimeError: If spawn depth exceeds MAX_SPAWN_DEPTH
        """
        self.runner = runner
        self.prompt = prompt
        self.output_schema = output_schema
        self.mcp_server = mcp_server
        self.parent = parent
        self._execution_started = False  # Track execution state
        self._llm_provider: Optional[LLMProvider] = None  # Created on execute
        self.events = EventEmitter()  # Agent-specific event emitter

        # Calculate spawn depth from parent
        self.spawn_depth = 0 if parent is None else parent.spawn_depth + 1

        # Check spawn depth limit to prevent infinite chains
        if self.spawn_depth >= self.MAX_SPAWN_DEPTH:
            raise RuntimeError(
                f"Agent spawn depth {self.spawn_depth} exceeds maximum {self.MAX_SPAWN_DEPTH}. "
                f"This likely indicates an infinite agent spawn loop."
            )

        # Give tools access to this agent
        self.mcp_server.agent = self

        # Generate agent_id immediately if not provided
        if agent_id is None:
            agent_id = str(uuid.uuid4())[:8]
        self._agent_id = agent_id

        # Task tracking
        self._task: Optional[asyncio.Task] = None
        self._result: Any = None

    async def execute(self) -> str:
        """
        Start agent execution.

        Returns:
            agent_id for tracking

        Raises:
            RuntimeError: If agent already started or runner not setup
        """
        # Validate not already started
        if self._execution_started:
            raise RuntimeError("Agent already started")
        self._execution_started = True

        # Validate runner is setup
        if not self.runner.container_id or not self.runner.instance_id:
            raise RuntimeError("Runner not properly initialized")

        # Check for duplicate agent_id
        if self._agent_id in self.runner._agents:
            raise ValueError(f"Agent {self._agent_id} already exists")

        # Register agent
        self.runner._agents[self._agent_id] = self

        # Wrap execution to capture result
        async def _wrapped_execute():
            try:
                result = await self._execute()
                self._result = result
                return result
            except Exception as e:
                self._result = e
                raise

        # Create task in TaskGroup
        if not self.runner._task_group:
            raise RuntimeError("Runner not in async context. Use 'async with runner:'")
        self._task = self.runner._task_group.create_task(_wrapped_execute())

        return self._agent_id

    async def _execute(self) -> Dict[str, Any]:
        """
        Execute the agent with proper event handling.

        Returns:
            Agent execution result
        """
        try:
            # Emit start event
            self.runner.events.emit(AgentStarted(agent=self))

            # Create agent-specific LLM provider
            self._llm_provider = create_llm_provider(
                self.runner.provider_name,
                self.runner.provider_config
            )

            # Execute agent (uses shared instance_id, container_id, worktrees_base_dir)
            result = await self._llm_provider.generate_structured(
                self.prompt,
                self,
                self.output_schema,
                self.events,
                verbose=self.runner._verbose,
            )

            # Emit completion event
            self.runner.events.emit(AgentCompleted(agent=self))

            return result

        except Exception as e:
            # Emit failure event
            self.runner.events.emit(AgentFailed(agent=self, error=str(e)))
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
        if not self._task:
            raise RuntimeError("Agent not started. Call execute() first.")

        # Wait for task to complete using Event (avoids await chain and recursive cancel)
        completion_event = asyncio.Event()

        def on_task_done(task):
            completion_event.set()

        self._task.add_done_callback(on_task_done)

        # Wait for completion event (not the task itself!)
        if timeout:
            await asyncio.wait_for(completion_event.wait(), timeout=timeout)
        else:
            await completion_event.wait()

        # Return stored result
        if isinstance(self._result, Exception):
            raise self._result

        return self._result

    async def wait_for_agents(
        self,
        agents: List["Agent"],
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Wait for child agents to complete.

        Args:
            agents: List of Agent objects to wait for (must be direct children)
            timeout: Optional timeout in seconds

        Returns:
            Dict mapping agent_id to result

        Raises:
            ValueError: If any agent is not a direct child
        """
        # Validate all agents are direct children
        for agent in agents:
            if agent.parent != self:
                raise ValueError(f"Agent {agent.agent_id} is not a child of {self.agent_id}")

        # Get tasks for agents
        tasks = []
        agent_ids = []
        for agent in agents:
            if agent._task:
                tasks.append(agent._task)
                agent_ids.append(agent.agent_id)

        # Wait for tasks to complete using Event (avoids await chain and recursive cancel)
        if tasks:
            completion_event = asyncio.Event()
            completed_count = 0

            def on_task_done(task):
                nonlocal completed_count
                completed_count += 1
                if completed_count == len(tasks):
                    completion_event.set()

            # Add callback to each task
            for task in tasks:
                task.add_done_callback(on_task_done)

            # Wait for completion event (not the tasks themselves!)
            if timeout:
                await asyncio.wait_for(completion_event.wait(), timeout=timeout)
            else:
                await completion_event.wait()

        # Collect results
        results = {}
        for agent in agents:
            result = agent._result
            if isinstance(result, Exception):
                results[agent.agent_id] = result
            else:
                results[agent.agent_id] = result

        return results

    @property
    def agent_id(self) -> str:
        """Get the agent ID (always available after construction)."""
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
        self._git_lock = asyncio.Lock()
        self._worktrees_lock = asyncio.Lock()
        self._agents: Dict[str, Agent] = {}  # Track agents by ID
        self._task_group: Optional[asyncio.TaskGroup] = None

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

        # Step 2: Get container image tag
        if image:
            # Use provided image directly
            image_tag = image
        elif self.config.image and self.config.image.image:
            # Use image from config if specified
            image_tag = self.config.image.image
        else:
            # Use default image
            image_tag = DEFAULT_IMAGE

        # Store image tag for reference
        self.image_tag = image_tag

        # Step 3: Create and start container
        container_info = self.container_manager.create_container(
            image_tag,
            self.project_path,
            self.worktrees_base_dir,
            self._network_mode,
        )
        self.container_id = container_info.container_id

        self.container_manager.start_container(self.container_id)

        # Create symlink in container so worktree .git files work
        self._setup_git_symlink()

    async def __aenter__(self):
        """Async context manager entry."""
        # Enter TaskGroup context
        tg_context = asyncio.TaskGroup()
        self._task_group = await tg_context.__aenter__()
        self._tg_context = tg_context
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async cleanup - properly awaits/cancels all tasks."""
        # Emit cancelled events for any remaining agents
        for agent in list(self._agents.values()):
            self.events.emit(AgentCancelled(agent=agent))

        # Exit TaskGroup context (handles task cancellation automatically)
        try:
            await self._tg_context.__aexit__(exc_type, exc_val, exc_tb)
        except* Exception:
            pass  # Suppress ExceptionGroup from task cancellations

        # Clean up container and worktrees
        await self._cleanup_async()
        return False  # Don't suppress exceptions

    def __del__(self):
        """Destructor - minimal cleanup."""
        # TaskGroup handles task cleanup automatically in __aexit__
        # Can't use await in __del__, so nothing to do here
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

        Raises:
            RuntimeError: If directory creation or symlink creation fails
        """
        if not self.container_id:
            raise RuntimeError("Container ID not set")

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
            raise RuntimeError(f"Failed to create directory structure for git symlink: {stderr}")

        # Create symlink from host path to /project/.git
        exit_code, _, stderr = asyncio.run(self.container_manager.exec_command(
            self.container_id,
            f"ln -sf /project/.git {host_project_path}/.git",
            workdir="/",
        ))

        if exit_code != 0:
            raise RuntimeError(f"Failed to create git symlink: {stderr}")

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
                        self.events.emit(Warning(f"Branch {full_branch_name} does not exist, skipping", "cleanup_worktrees"))
                        continue

                    # Create a copy of the branch with the new name (force overwrite if exists)
                    self.git_ops.repo.git.branch("-f", branch_name, full_branch_name)
                    self.events.emit(BranchKept(
                        original_name=full_branch_name,
                        new_name=branch_name
                    ))

                except Exception as e:
                    self.events.emit(Warning(f"Failed to copy branch {full_branch_name}: {e}", "cleanup_worktrees"))
            else:
                self.events.emit(Warning(f"Branch {branch_name} was not created in this session", "cleanup_worktrees"))

        # Step 2: Now remove all worktrees
        for worktree_name in self.created_worktrees:
            worktree_path = self.worktrees_base_dir / worktree_name
            if worktree_path.exists():
                try:
                    self.git_ops.remove_worktree(worktree_path)
                except Exception as e:
                    self.events.emit(Warning(f"Failed to remove worktree {worktree_name}: {e}", "cleanup_worktrees"))

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
                except Exception as e:
                    self.events.emit(Warning(f"Failed to delete branch {branch_name}: {e}", "cleanup_worktrees"))
        except Exception as e:
            self.events.emit(Warning(f"Failed to list remaining branches: {e}", "cleanup_worktrees"))

        # Remove instance directory
        if self.worktrees_base_dir.exists():
            try:
                shutil.rmtree(self.worktrees_base_dir)
            except Exception as e:
                self.events.emit(Warning(f"Failed to remove instance directory: {e}", "cleanup_worktrees"))

    async def _cleanup_async(self) -> None:
        """
        Async cleanup - properly awaits/cancels all tasks.

        Safe to call multiple times.
        Note: Task cancellation is handled by TaskGroup's __aexit__
        """
        # Skip if already cleaned up
        if self._cleaned_up:
            return

        self._cleaned_up = True

        # Clear review feedback
        self._review_feedback.clear()

        # Cleanup container (sync is fine)
        if self.container_id:
            try:
                self.container_manager.cleanup(self.container_id)
            except Exception as e:
                self.events.emit(Warning(f"Failed to cleanup container: {e}", "cleanup"))
            finally:
                self.container_id = None

        # Cleanup worktrees (sync)
        try:
            self._cleanup_worktrees(self._keep_branches)
        except Exception as e:
            self.events.emit(Warning(f"Failed to cleanup worktrees: {e}", "cleanup"))

    def get_running_agents(self) -> List[str]:
        """Get list of currently running agent IDs."""
        return list(self._agents.keys())
