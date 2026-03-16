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
    LLMMessageSent,
    LLMResponseReceived,
    LLMToolCompleted,
    LLMToolsExecuting,
)
from llm_sandbox.output import OutputService
from llm_sandbox.runner import (
    AgentCancelled,
    AgentCompleted,
    AgentFailed,
    AgentStarted,
    BranchKept,
    Warning,
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
    # Warnings
    runner.events.on(
        Warning,
        lambda e: output.warning(f"{e.message} [{e.context}]" if e.context else e.message)
    )

    # Cleanup events
    runner.events.on(
        AgentCancelled,
        lambda e: output.verbose(f"Cancelled agent: {e.agent.agent_id}")
    )

    # Branch operations
    runner.events.on(
        BranchKept,
        lambda e: output.info(f"Keeping branch: {e.original_name} → {e.new_name}")
    )

    # Agent execution (unified handling)
    def _format_agent_label(agent_id: str, spawn_depth: int) -> str:
        """Format agent label with optional 'Background' prefix."""
        prefix = "Background " if spawn_depth > 0 else ""
        return f"{prefix}Agent {agent_id}"

    # Wire up LLM events for all agents (including sub-agents)
    def _on_agent_started(e):
        # Wire up this agent's LLM events
        wire_up_agent_llm_events(e.agent, output)

        # Display start message
        if e.agent.spawn_depth > 0:
            output.verbose(
                f"\n{'='*60}\n"
                f"[{_format_agent_label(e.agent.agent_id, e.agent.spawn_depth)}] "
                f"Starting at depth {e.agent.spawn_depth}\n"
                f"{'='*60}"
            )
        else:
            output.verbose(
                f"\n[{_format_agent_label(e.agent.agent_id, e.agent.spawn_depth)}] Starting execution..."
            )

    runner.events.on(AgentStarted, _on_agent_started)

    runner.events.on(
        AgentCompleted,
        lambda e: output.verbose(
            f"\n[{_format_agent_label(e.agent.agent_id, e.agent.spawn_depth)}] "
            f"✓ Completed successfully"
        )
    )

    runner.events.on(
        AgentFailed,
        lambda e: output.error(
            f"\n[{_format_agent_label(e.agent.agent_id, e.agent.spawn_depth)}] "
            f"Failed: {e.error}"
        )
    )

def wire_up_agent_llm_events(agent, output: OutputService) -> None:
    """Wire up LLM event handlers for an agent.

    Args:
        agent: Agent instance (with events EventEmitter)
        output: OutputService for formatting output
    """
    agent.events.on(
        LLMIterationStarted,
        lambda e: output.verbose(
            f"\n{'='*60}\n"
            f"Iteration {e.iteration}/{e.max_iterations}\n"
            f"{'='*60}"
        )
    )

    agent.events.on(
        LLMMessageSent,
        lambda e: output.verbose(f"→ Sending {e.role} message: {e.content_preview}{'...' if len(e.content_preview) == 100 else ''}")
    )

    agent.events.on(
        LLMResponseReceived,
        lambda e: output.verbose(
            f"← Response ({e.stop_reason}): {e.content_preview}{'...' if len(e.content_preview) == 100 else ''}"
            if e.content_preview else
            f"← Response stop reason: {e.stop_reason}"
        )
    )

    def _format_tool_params(params):
        """Format tool parameters for display."""
        if not params:
            return ""
        # Show first few keys or truncate
        param_str = str(params)
        if len(param_str) > 50:
            return param_str[:50] + "..."
        return param_str

    agent.events.on(
        LLMToolsExecuting,
        lambda e: output.verbose(
            f"→ Executing {e.tool_count} tool(s): " +
            ", ".join(f"{name}({_format_tool_params(params)})" for name, params in zip(e.tool_names, e.tool_params))
        )
    )

    def _format_tool_result(result):
        """Format tool result for display."""
        if not result:
            return ""
        result_str = str(result)
        if len(result_str) > 100:
            return result_str[:100] + "..."
        return result_str

    agent.events.on(
        LLMToolCompleted,
        lambda e: output.verbose(
            f"← Tool {e.tool_name}: {'✓' if e.success else '✗'} {_format_tool_result(e.result)}"
        )
    )

    agent.events.on(
        LLMJSONParseError,
        lambda e: output.error(f"Failed to parse JSON from LLM response: {e.error}")
    )


