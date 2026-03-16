# Output Separation Refactoring Guide

## Overview

This project has been refactored to separate business logic from output/display concerns. Business logic now emits **typed events** and returns **structured dataclasses**, while the CLI layer handles all formatting and display.

## Architecture

```
┌─────────────────────┐
│   CLI Layer         │  - Wires up event handlers
│  (subcommands)      │  - Formats output using OutputService
│                     │  - Handles --verbose, --format, --quiet flags
└──────────┬──────────┘
           │ subscribes to events
           ▼
┌─────────────────────┐
│  Business Logic     │  - Emits typed events (dataclasses)
│  (runner, container │  - Returns structured data
│   llm_provider)     │  - NO click.echo or formatting
└─────────────────────┘
```

## Key Components

### 1. EventEmitter (`src/llm_sandbox/events.py`)

Type-safe event system using dataclasses:

```python
from llm_sandbox.events import EventEmitter
from dataclasses import dataclass

# Define event types
@dataclass
class UserCreated:
    user_id: str
    timestamp: datetime

# Create emitter and subscribe
emitter = EventEmitter()
emitter.on(UserCreated, lambda e: print(f"User {e.user_id} created"))

# Emit events
emitter.emit(UserCreated(user_id="123", timestamp=datetime.now()))
```

### 2. OutputService (`src/llm_sandbox/output.py`)

Abstraction for formatted output with multiple implementations:

```python
from llm_sandbox.output import create_output_service

# Create output service
output = create_output_service(format="text", verbose=True)

# Use for formatting
output.info("Normal message")
output.success("Operation completed")  # Green with ✓
output.warning("Something to note")    # Yellow
output.error("Something failed")       # Red to stderr
output.verbose("Detailed info")        # Only shown with --verbose
```

**Implementations:**
- `ClickOutputService` - Standard CLI with colors (default)
- `JSONOutputService` - Machine-readable JSON output
- `QuietOutputService` - Errors only
- `CaptureOutputService` - For testing

### 3. Event Types

**ContainerManager** (6 events):
- `ImagePullStarted`, `ImagePullProgress`, `ImagePullCompleted`
- `BuildLogLine`, `ImageNotFoundLocally`

**SandboxRunner** (12 events):
- `InstanceCreated`, `ContainerStarted`
- `WorktreeCreated`, `WorktreeRemoveFailed`
- `BranchDeleted`, `BranchKept`
- `AgentStarted`, `AgentCompleted`, `AgentFailed`
- `ParallelAgentsStarted`, `BackgroundAgentsCanceling`
- `CleanupStarted`, `WarningIssued`

**LLMProvider** (6 events):
- `LLMIterationStarted`, `LLMMessageSent`, `LLMResponseReceived`
- `LLMToolsExecuting`, `LLMToolCompleted`, `LLMJSONParseError`

### 4. Data Models (`src/llm_sandbox/models.py`)

Business logic returns structured data:

```python
from llm_sandbox.models import ContainerInfo

# Method returns dataclass instead of string
container_info = container_manager.create_container(...)
print(f"Container ID: {container_info.container_id}")
print(f"Created at: {container_info.created_at}")
```

## Using in CLI Commands

### Quick Start (Recommended)

Use the `event_handlers` module for consistent wiring:

```python
from llm_sandbox.event_handlers import wire_up_all_events
from llm_sandbox.output import create_output_service
from llm_sandbox.runner import SandboxRunner

def execute(project_dir, verbose, **kwargs):
    # Create output service
    output = create_output_service(format="text", verbose=verbose)

    # Create runner
    runner = SandboxRunner(project_dir, config)

    # Wire up ALL events with one call
    wire_up_all_events(runner, output)

    # Run your code - events are automatically formatted
    await runner.setup()
    result = await runner.run_agents([agent])
```

### Manual Wiring

For fine-grained control:

```python
from llm_sandbox.event_handlers import (
    wire_up_runner_events,
    wire_up_container_events,
    wire_up_llm_events
)

# Wire up specific components
wire_up_runner_events(runner, output)
wire_up_container_events(runner.container_manager, output)

# Or wire up individual events
runner.events.on(InstanceCreated,
    lambda e: output.info(f"Instance: {e.instance_id}"))

runner.events.on(WarningIssued,
    lambda e: output.warning(e.message))
```

### Custom Event Handlers

```python
def on_agent_completed(event: AgentCompleted):
    """Custom handler with complex logic."""
    output.success(f"Agent {event.agent_id} finished!")
    # ... additional logic ...

runner.events.on(AgentCompleted, on_agent_completed)
```

## Example: Complete Subcommand

```python
from llm_sandbox.event_handlers import wire_up_all_events
from llm_sandbox.output import create_output_service

class MySubcommand(Subcommand):
    name = "mycommand"

    def execute(self, project_dir, verbose, **kwargs):
        # Create output service
        output = create_output_service(
            format="text",
            verbose=verbose,
            quiet=False
        )

        # Load config and create runner
        config = load_config(project_dir)
        runner = SandboxRunner(project_dir, config)

        # Wire up all events
        wire_up_all_events(runner, output)

        # Your business logic
        async def run():
            await runner.setup()

            # ... do work ...

            result = await runner.run_agents([agent])

            # Format final output
            output.success("Task completed!")
            return result

        # Run
        try:
            result = asyncio.run(run())
            click.echo(json.dumps(result, indent=2))
        except Exception as e:
            output.error(str(e))
            sys.exit(1)
```

## Testing

Test business logic by verifying events, not output:

```python
from llm_sandbox.output import CaptureOutputService

def test_container_creation():
    """Test that container creation emits correct events."""
    manager = ContainerManager()

    events_received = []
    manager.events.on(ImageNotFoundLocally,
        lambda e: events_received.append(e))

    # Call business logic
    info = manager.create_container(...)

    # Verify structured data
    assert isinstance(info, ContainerInfo)
    assert info.container_id is not None

    # Verify events
    assert len(events_received) == 1
    assert events_received[0].image == "python:3.11"
```

## Migration Checklist for New Subcommands

- [ ] Create `OutputService` with `create_output_service()`
- [ ] Wire up events with `wire_up_all_events(runner, output)` or individual helpers
- [ ] Use `output.info()`, `output.success()`, etc. for display
- [ ] Never use `click.echo()` directly in subcommand business logic
- [ ] Format final results at the end (JSON, tables, etc.)
- [ ] Test by verifying events and return values, not output strings

## Benefits

✅ **Separation of Concerns**: Business logic is pure, CLI handles display
✅ **Type Safety**: Events are dataclasses with autocomplete
✅ **Testability**: Test data structures, not string parsing
✅ **Flexibility**: Same events → multiple output formats (text/JSON/custom)
✅ **Maintainability**: Change display without touching business logic

## Backward Compatibility

Existing verbose output (`if verbose:` blocks) remains for now. As subcommands are migrated to use `OutputService`, these can be gradually removed.
