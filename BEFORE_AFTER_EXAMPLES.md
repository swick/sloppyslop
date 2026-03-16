# Before & After: Output Separation Refactoring

This document shows concrete examples of how the code changed during the refactoring.

## Example 1: Container Creation

### Before ❌

```python
# src/llm_sandbox/container.py
def create_container(self, image_id: str, ...) -> str:
    """Create container with mounts."""

    # Check if image exists, pull if not
    if not self.image_exists(image_id):
        click.echo(f"Image not found locally: {image_id}")  # ❌ Output in business logic
        self.pull_image(image_id)

    # ... create container ...

    return container_id  # Just a string
```

### After ✅

```python
# src/llm_sandbox/container.py
def create_container(self, image_id: str, ...) -> ContainerInfo:
    """Create container with mounts."""

    # Check if image exists, pull if not
    if not self.image_exists(image_id):
        self.events.emit(ImageNotFoundLocally(image=image_id))  # ✅ Type-safe event
        self.pull_image(image_id)

    # ... create container ...

    return ContainerInfo(  # ✅ Structured data
        container_id=container_id,
        image=image_id,
        created_at=datetime.now(),
        project_mount=project_mount,
        worktrees_mount=worktrees_mount,
        network=network,
    )
```

**CLI Layer:**
```python
# CLI wires up event handler
container_manager.events.on(ImageNotFoundLocally,
    lambda e: output.info(f"Image not found locally: {e.image}"))
```

---

## Example 2: Runner Setup

### Before ❌

```python
# src/llm_sandbox/runner.py
async def setup(self):
    self.instance_id = self._generate_instance_id()
    self.worktrees_base_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Instance ID: {self.instance_id}")  # ❌ Output in business logic

    container_id = self.container_manager.create_container(...)
    self.container_manager.start_container(container_id)
    click.echo(f"Container started: {container_id[:12]}")  # ❌ More output
```

### After ✅

```python
# src/llm_sandbox/runner.py
async def setup(self):
    self.instance_id = self._generate_instance_id()
    self.worktrees_base_dir.mkdir(parents=True, exist_ok=True)
    self.events.emit(InstanceCreated(  # ✅ Type-safe event
        instance_id=self.instance_id,
        timestamp=datetime.now()
    ))

    container_info = self.container_manager.create_container(...)
    self.container_id = container_info.container_id  # ✅ Use structured data
    self.container_manager.start_container(self.container_id)
    self.events.emit(ContainerStarted(  # ✅ Another event
        container_id=self.container_id,
        image=container_info.image,
        timestamp=datetime.now()
    ))
```

**CLI Layer:**
```python
# Wire up event handlers with ONE LINE!
from llm_sandbox.event_handlers import wire_up_all_events

output = create_output_service(format="text", verbose=True)
wire_up_all_events(runner, output)

# Events automatically formatted:
# runner.events.on(InstanceCreated, lambda e: output.info(f"Instance ID: {e.instance_id}"))
# runner.events.on(ContainerStarted, lambda e: output.success(f"Container started: {e.container_id[:12]}"))
```

---

## Example 3: LLM Iteration

### Before ❌

```python
# src/llm_sandbox/llm_provider.py
async def generate_structured(self, ...):
    iteration = 0
    while iteration < max_iterations:
        iteration += 1

        if self.verbose:
            click.echo(f"\n{'='*60}")  # ❌ Formatting in business logic
            click.echo(f"Iteration {iteration}/{max_iterations}")
            click.echo(f"{'='*60}")

        response = await self._make_api_call(...)

        if self.verbose:
            click.echo(f"\nResponse stop reason: {response.stop_reason}")  # ❌ More output
```

### After ✅

```python
# src/llm_sandbox/llm_provider.py
async def generate_structured(self, ...):
    iteration = 0
    while iteration < max_iterations:
        iteration += 1

        self.events.emit(LLMIterationStarted(  # ✅ Type-safe event
            iteration=iteration,
            max_iterations=max_iterations
        ))

        response = await self._make_api_call(...)

        self.events.emit(LLMResponseReceived(  # ✅ Another event
            stop_reason=response.stop_reason,
            usage=response.usage.__dict__
        ))
```

**CLI Layer:**
```python
# Event handler formats for display
llm_provider.events.on(LLMIterationStarted,
    lambda e: output.verbose(
        f"\n{'='*60}\n"
        f"Iteration {e.iteration}/{e.max_iterations}\n"
        f"{'='*60}"
    ))

llm_provider.events.on(LLMResponseReceived,
    lambda e: output.verbose(f"Response stop reason: {e.stop_reason}"))
```

---

## Example 4: Subcommand Implementation

### Before ❌

```python
# src/llm_sandbox/subcommands/run/subcommand.py
def execute(self, project_dir: Path, **kwargs):
    # Validation
    if not prompt and not prompt_file:
        click.echo("Error: Either --prompt or --prompt-file must be provided", err=True)  # ❌
        sys.exit(1)

    # Load config
    config = load_config(project_dir)
    runner = SandboxRunner(project_dir, config)

    # Run
    result = asyncio.run(self._execute_async(runner, ...))

    # Output
    click.echo("\n" + "=" * 60)  # ❌ Manual formatting
    click.echo("Result:")
    click.echo("=" * 60)
    click.echo(json.dumps(result, indent=2))
```

### After ✅

```python
# src/llm_sandbox/subcommands/run/subcommand.py
def execute(self, project_dir: Path, **kwargs):
    # Create output service
    output = create_output_service(format="text", verbose=kwargs["verbose"])  # ✅

    # Validation
    if not prompt and not prompt_file:
        output.error("Either --prompt or --prompt-file must be provided")  # ✅
        sys.exit(1)

    # Load config
    config = load_config(project_dir)
    runner = SandboxRunner(project_dir, config)

    # Wire up ALL events with ONE LINE!
    wire_up_all_events(runner, output)  # ✅ That's it!

    # Run
    try:
        result = asyncio.run(self._execute_async(runner, ...))

        # Output
        output.info("\n" + "=" * 60)  # ✅ Through OutputService
        output.info("Result:")
        output.info("=" * 60)
        click.echo(json.dumps(result, indent=2))
    except Exception as e:
        output.error(f"Execution failed: {e}")  # ✅ Clean error handling
        sys.exit(1)
```

---

## Example 5: Testing

### Before ❌

```python
# Testing was difficult - had to parse strings
def test_container_creation():
    manager = ContainerManager()

    # Capture output with complex mocking
    with patch('click.echo') as mock_echo:
        container_id = manager.create_container("python:3.11", ...)

        # Parse string output 😞
        assert "Image not found" in mock_echo.call_args[0][0]
```

### After ✅

```python
# Testing is simple - verify data structures
def test_container_creation():
    manager = ContainerManager()

    # Capture events
    events_received = []
    manager.events.on(ImageNotFoundLocally,
        lambda e: events_received.append(e))

    # Call method
    container_info = manager.create_container("python:3.11", ...)

    # Verify structured data ✅
    assert isinstance(container_info, ContainerInfo)
    assert container_info.container_id is not None
    assert container_info.image == "python:3.11"

    # Verify events ✅
    assert len(events_received) == 1
    assert events_received[0].image == "python:3.11"
```

---

## Example 6: Multiple Output Formats

### Before ❌

```python
# Only one format - text to terminal
# No easy way to get JSON or quiet mode
```

### After ✅

```python
# Text format (default)
output = create_output_service(format="text", verbose=True)
wire_up_all_events(runner, output)
# → Colored output with ✓ symbols

# JSON format (machine-readable)
output = create_output_service(format="json", verbose=True)
wire_up_all_events(runner, output)
output.flush()  # Outputs JSON array of events

# Quiet mode (errors only)
output = create_output_service(format="text", quiet=True)
wire_up_all_events(runner, output)
# → Only errors displayed

# Testing (capture for assertions)
from llm_sandbox.output import CaptureOutputService
output = CaptureOutputService()
wire_up_all_events(runner, output)
messages = output.get_messages("success")
assert "Container started" in messages[0]
```

---

## Key Improvements Summary

| Aspect | Before | After |
|--------|--------|-------|
| **Output Location** | ❌ Mixed in business logic | ✅ CLI layer only |
| **Data Types** | ❌ Strings, primitives | ✅ Typed dataclasses |
| **Events** | ❌ None | ✅ 24 typed events |
| **Testing** | ❌ Parse strings | ✅ Verify data structures |
| **Flexibility** | ❌ Hard-coded text | ✅ Multiple formats (text/JSON/quiet) |
| **Type Safety** | ❌ Strings everywhere | ✅ Full type hints |
| **Integration** | ❌ Manual each time | ✅ ONE LINE: `wire_up_all_events()` |
| **Maintainability** | ❌ Change logic to change display | ✅ Change handlers only |

---

## The Power of Events

### Before: 470 `click.echo()` calls scattered across 14 files

```python
click.echo(f"Pulling image: {reference}")
click.echo(f"Container started: {container_id}")
click.echo(f"Warning: Failed to remove worktree")
click.echo(f"Iteration {iteration}/{max_iterations}")
# ... 466 more ...
```

### After: 24 event types, emitted from business logic

```python
self.events.emit(ImagePullStarted(reference=reference))
self.events.emit(ContainerStarted(container_id=container_id, ...))
self.events.emit(WorktreeRemoveFailed(name=name, error=error))
self.events.emit(LLMIterationStarted(iteration=iteration, ...))
```

**CLI layer handles ALL formatting with ONE LINE:**
```python
wire_up_all_events(runner, output)
```

---

## Conclusion

The refactoring transformed:
- **470 scattered `click.echo` calls** → **24 typed events**
- **String-based output** → **Structured data + events**
- **Mixed concerns** → **Clean separation**
- **Hard to test** → **Easy to verify**
- **One format** → **Multiple formats (text/JSON/quiet/custom)**
- **Manual wiring** → **ONE LINE integration**

**Result: Clean, testable, maintainable, flexible architecture** ✅
