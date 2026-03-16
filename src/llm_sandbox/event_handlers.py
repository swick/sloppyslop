"""Event handler utilities for wiring up events in CLI layer.

This module provides helper functions to consistently wire up event handlers
for Runner, ContainerManager, and LLMProvider events across different subcommands.
"""

from llm_sandbox.container import (
    ImageBuildProgress,
    ImageBuildState,
    ImagePullProgress,
    ImagePullState,
)
from llm_sandbox.llm_provider import (
    LLMIterationStarted,
    LLMJSONParseError,
    LLMResponseReceived,
    LLMToolCompleted,
    LLMToolsExecuting,
)
from llm_sandbox.output import OutputService
from llm_sandbox.runner import (
    AgentCompleted,
    AgentFailed,
    AgentStarted,
    BackgroundAgentCompleted,
    BackgroundAgentFailed,
    BackgroundAgentSpawned,
    BackgroundAgentStarting,
    BackgroundAgentsAllCompleted,
    BackgroundAgentsCanceling,
    BackgroundAgentsWaiting,
    BranchDeleted,
    BranchKept,
    CleanupStarted,
    ContainerStarted,
    InstanceCreated,
    ParallelAgentsStarted,
    WarningIssued,
    WorktreeRemoveFailed,
)


def create_image_pull_callback(output: OutputService):
    """Create a callback for image pull progress.

    Args:
        output: OutputService for formatting output

    Returns:
        Callback function for ImagePullProgress events
    """
    def handle_image_pull(event: ImagePullProgress):
        if event.state == ImagePullState.STARTED:
            output.info(f"Pulling image: {event.reference}")
        elif event.state == ImagePullState.DOWNLOADING and event.message:
            output.verbose(event.message)
        elif event.state == ImagePullState.COMPLETED:
            output.success(f"Image pulled: {event.reference}")
        elif event.state == ImagePullState.FAILED:
            output.error(f"Failed to pull image: {event.error}")

    return handle_image_pull


def create_image_build_callback(output: OutputService):
    """Create a callback for image build progress.

    Args:
        output: OutputService for formatting output

    Returns:
        Callback function for ImageBuildProgress events
    """
    def handle_image_build(event: ImageBuildProgress):
        if event.state == ImageBuildState.STARTED:
            output.info(f"Building image: {event.tag}")
        elif event.state == ImageBuildState.BUILDING and event.log_line:
            output.verbose(event.log_line.rstrip())
        elif event.state == ImageBuildState.COMPLETED:
            output.success(f"Image built: {event.tag}")
        elif event.state == ImageBuildState.FAILED:
            output.error(f"Failed to build image: {event.error}")

    return handle_image_build


def wire_up_runner_events(runner, output: OutputService) -> None:
    """Wire up sandbox runner event handlers.

    Args:
        runner: SandboxRunner instance
        output: OutputService for formatting output
    """
    # Setup events
    runner.events.on(
        InstanceCreated,
        lambda e: output.info(f"Instance ID: {e.instance_id}")
    )

    runner.events.on(
        ContainerStarted,
        lambda e: output.success(f"Container started: {e.container_id[:12]}")
    )

    # Cleanup events
    runner.events.on(
        CleanupStarted,
        lambda e: output.verbose(f"Cleaning up {e.component}...")
    )

    runner.events.on(
        BackgroundAgentsCanceling,
        lambda e: output.info(f"Canceling {e.agent_count} background agent(s)...")
    )

    # Branch operations
    runner.events.on(
        BranchKept,
        lambda e: output.info(f"Keeping branch: {e.original_name} → {e.new_name}")
    )

    runner.events.on(
        BranchDeleted,
        lambda e: output.verbose(f"Deleted branch: {e.branch_name}")
    )

    runner.events.on(
        WorktreeRemoveFailed,
        lambda e: output.warning(f"Failed to remove worktree {e.name}: {e.error}")
    )

    # Agent execution
    runner.events.on(
        AgentStarted,
        lambda e: output.verbose(f"\n[Agent {e.agent_id}] Starting execution...")
    )

    runner.events.on(
        AgentCompleted,
        lambda e: output.verbose(f"\n[Agent {e.agent_id}] Completed successfully")
    )

    runner.events.on(
        AgentFailed,
        lambda e: output.error(f"\n[Agent {e.agent_id}] Failed: {e.error}")
    )

    runner.events.on(
        ParallelAgentsStarted,
        lambda e: output.info(
            f"\n{'='*60}\n"
            f"Running {e.agent_count} agent(s) in parallel\n"
            f"Shared environment:\n"
            f"  - Container ID: {e.container_id[:12]}\n"
            f"  - Instance ID: {e.instance_id}\n"
            f"{'='*60}"
        )
    )

    # Background agent events
    runner.events.on(
        BackgroundAgentSpawned,
        lambda e: output.verbose(
            f"→ Spawned background agent '{e.agent_id}' "
            f"(depth {e.spawn_depth}, {e.tool_count} tools)"
        )
    )

    runner.events.on(
        BackgroundAgentStarting,
        lambda e: output.verbose(
            f"\n{'='*60}\n"
            f"[Background Agent {e.agent_id}] Starting at depth {e.spawn_depth}\n"
            f"{'='*60}"
        )
    )

    runner.events.on(
        BackgroundAgentCompleted,
        lambda e: output.verbose(f"\n[Background Agent {e.agent_id}] ✓ Completed successfully")
    )

    runner.events.on(
        BackgroundAgentFailed,
        lambda e: output.verbose(f"\n[Background Agent {e.agent_id}] ✗ Failed: {e.error}")
    )

    runner.events.on(
        BackgroundAgentsWaiting,
        lambda e: output.verbose(
            f"\nWaiting for {e.agent_count} background agent(s) to complete...\n" +
            "\n".join(f"  - {aid}" for aid in e.agent_ids)
        )
    )

    runner.events.on(
        BackgroundAgentsAllCompleted,
        lambda e: output.verbose(f"✓ All {e.agent_count} agent(s) completed")
    )

    # Warnings
    runner.events.on(
        WarningIssued,
        lambda e: output.warning(f"{e.message} [{e.context}]" if e.context else e.message)
    )


def wire_up_llm_events(llm_provider, output: OutputService) -> None:
    """Wire up LLM provider event handlers.

    Args:
        llm_provider: LLMProvider instance
        output: OutputService for formatting output
    """
    llm_provider.events.on(
        LLMIterationStarted,
        lambda e: output.verbose(
            f"\n{'='*60}\n"
            f"Iteration {e.iteration}/{e.max_iterations}\n"
            f"{'='*60}"
        )
    )

    llm_provider.events.on(
        LLMResponseReceived,
        lambda e: output.verbose(f"Response stop reason: {e.stop_reason}")
    )

    llm_provider.events.on(
        LLMToolsExecuting,
        lambda e: output.verbose(f"→ Executing {e.tool_count} tool(s): {', '.join(e.tool_names)}")
    )

    llm_provider.events.on(
        LLMToolCompleted,
        lambda e: output.verbose(
            f"← Tool {e.tool_name}: {'✓' if e.success else '✗'}"
        )
    )

    llm_provider.events.on(
        LLMJSONParseError,
        lambda e: output.error(f"Failed to parse JSON from LLM response: {e.error}")
    )


def wire_up_all_events(runner, output: OutputService) -> None:
    """Wire up all event handlers for a runner and its components.

    Convenience function that wires up events for the runner, its container manager,
    and its LLM provider (if available).

    Args:
        runner: SandboxRunner instance
        output: OutputService for formatting output
    """
    # Wire up runner events
    wire_up_runner_events(runner, output)

    # Wire up container manager events
    wire_up_container_events(runner.container_manager, output)

    # Wire up LLM provider events (if available)
    if runner.llm_provider:
        wire_up_llm_events(runner.llm_provider, output)
