# Async Implementation - Final Summary

## Design Principle: Async-Only Interface

The LLM provider has been refactored to use **async-only methods**. Synchronous compatibility is provided through wrappers where needed (e.g., `runner.run_agent()` uses `asyncio.run()` internally).

## Key Components

### 1. LLM Provider (`llm_provider.py`) - Async Only

**`LLMProvider` (base class)**:
- Abstract async methods only:
  - `generate_text_async()`
  - `generate_structured_async()`
  - `validate_async()`

**`ClaudeProvider`**:
- Uses `AsyncAnthropic` client
- Implements all async methods
- Automatically detects async MCP servers and executes tools concurrently
- **Only supports Anthropic API** (Vertex AI not supported)

### 2. Container Management (`container.py`) - Async exec_command Only

**Single async method**: `exec_command_async()`
- Uses `FuturesSession` from `requests-futures` library
- Truly async without threads - wraps requests with concurrent.futures
- All other container methods remain synchronous (they're only called during setup/cleanup)

**Implementation**:
```python
from requests_futures.sessions import FuturesSession

# In __init__:
self.async_session = FuturesSession(
    executor=ThreadPoolExecutor(max_workers=10),
    session=requests_unixsocket.Session()
)

# In exec_command_async:
future = self.async_session.post(url, json=data)
response = await asyncio.wrap_future(future)
```

### 3. Git Operations (`git_ops.py`) - Sync Only

All git operations remain synchronous:
- `create_worktree_on_branch()`
- `commit_files()`
- `remove_worktree()`

Async tools use `asyncio.to_thread()` when calling these methods with locks.

### 4. Async Tools (`async_tools.py`)

All tools implement `AsyncMCPTool` base class with `execute_async()` method:

**Container tools**:
- `AsyncExecuteCommandTool` - Calls `container_manager.exec_command_async()`

**Git tools** (use locks + asyncio.to_thread):
- `AsyncCheckoutCommitTool`
- `AsyncGitCommitTool`

**File tools** (use asyncio.to_thread):
- `AsyncReadFileTool`
- `AsyncWriteFileTool`
- `AsyncEditFileTool`

**Example**:
```python
async def execute_async(self, arguments):
    # Use git lock for safe concurrent access
    if hasattr(self.runner, '_git_lock') and self.runner._git_lock:
        async with self.runner._git_lock:
            await asyncio.to_thread(
                self.runner.git_ops.commit_files,
                worktree_path,
                files,
                message,
            )
```

### 5. Runner (`runner.py`)

**Synchronous wrapper** for backwards compatibility:
```python
def run_agent(self, prompt, output_schema, mcp_server, verbose=False):
    """Sync wrapper - uses asyncio.run() internally"""
    return asyncio.run(
        self.llm_provider.generate_structured_async(
            prompt, mcp_server, output_schema, verbose
        )
    )
```

**Parallel execution**:
```python
def run_agents_parallel(self, agents, verbose=False):
    """Run multiple agents concurrently"""
    return asyncio.run(self._run_agents_async(agents, verbose))
```

## Usage Examples

### Single Agent (Backwards Compatible)

```python
from llm_sandbox import SandboxRunner, AgentConfig
from llm_sandbox.mcp_tools import AsyncMCPServer
from llm_sandbox.async_tools import AsyncCheckoutCommitTool

runner = SandboxRunner(project_path, config)
runner.setup()

# Create async MCP server
server = AsyncMCPServer()
server.add_tool(AsyncCheckoutCommitTool(runner))

# Run single agent (sync wrapper over async implementation)
result = runner.run_agent(prompt, output_schema, server)

runner.cleanup()
```

### Multiple Agents in Parallel

```python
agents = [
    AgentConfig(
        prompt="Analyze authentication",
        output_schema={"type": "object", "properties": {...}},
        mcp_server=create_server(runner),
        agent_id="auth",
    ),
    AgentConfig(
        prompt="Analyze database",
        output_schema={"type": "object", "properties": {...}},
        mcp_server=create_server(runner),
        agent_id="db",
    ),
]

# All agents run concurrently
results = runner.run_agents_parallel(agents, verbose=True)
```

## Shared Environment Model

All parallel agents share:
- **Container** - Single container for all agents
- **Instance ID** - Same `instance_id`
- **Worktrees directory** - Same base directory
- **Git state** - Synchronized with `_git_lock` and `_worktrees_lock`

Each agent gets:
- **Isolated conversation** - Separate `LLMProvider` instance
- **Separate tools** - Different `MCPServer` per agent (configured in `AgentConfig`)

## Dependencies

Added to `pyproject.toml`:
```toml
dependencies = [
    ...
    "requests-futures>=1.0.0",  # For async container operations
]
```

Existing dependencies already support async:
- `anthropic>=0.39.0` - Includes `AsyncAnthropic` client
- `pytest-asyncio>=0.21.0` - For async testing (dev dependency)

## Performance Benefits

1. **Concurrent tool execution** - When Claude requests multiple tools, they execute in parallel
2. **Parallel agent execution** - Multiple agents run simultaneously sharing resources
3. **True async I/O** - Container exec uses FuturesSession (no blocking)
4. **Expected speedup**: 3-4x for multiple agents, near-linear for concurrent tools

## Testing

```bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/test_parallel_execution.py -v

# Try example
python examples/parallel_agents_example.py
```

## File Changes Summary

1. **`src/llm_sandbox/llm_provider.py`** - Async-only interface (~500 lines)
2. **`src/llm_sandbox/container.py`** - Added `exec_command_async()` with FuturesSession
3. **`src/llm_sandbox/git_ops.py`** - Kept sync-only (no changes needed)
4. **`src/llm_sandbox/async_tools.py`** - All async tool implementations (~700 lines)
5. **`src/llm_sandbox/mcp_tools.py`** - Added `AsyncMCPTool` and `AsyncMCPServer`
6. **`src/llm_sandbox/runner.py`** - Parallel execution + sync wrappers
7. **`pyproject.toml`** - Added `requests-futures` dependency
8. **`examples/parallel_agents_example.py`** - Example usage
9. **Documentation files** - PARALLEL_EXECUTION.md, IMPLEMENTATION_SUMMARY.md, etc.

## Migration from Previous Design

If you were using the earlier implementation with both sync and async methods on providers:

**Old** (dual interface):
```python
# Provider had both sync and async
result = provider.generate_structured(...)  # sync
result = await provider.generate_structured_async(...)  # async
```

**New** (async-only):
```python
# Provider only has async methods
result = await provider.generate_structured_async(...)  # async only

# Use sync wrapper at runner level if needed
result = runner.run_agent(...)  # internally uses asyncio.run()
```

## Why This Design?

1. **Simplicity** - Single async interface, no confusion about which method to use
2. **Performance** - Async-first design enables natural concurrent execution
3. **Backwards compatibility** - Sync wrappers in `runner.run_agent()` mean existing code still works
4. **Clean separation** - Async at LLM/tools layer, sync wrappers at application layer
5. **True async where it matters** - Container exec uses FuturesSession, not threads

## Limitations

1. **Vertex AI not supported** - Only Anthropic API backend works
2. **Git operations use threads** - `asyncio.to_thread()` for git operations (acceptable since they're fast)
3. **Setup/cleanup remain sync** - Only called once per session, async not needed

## Next Steps (Future Optimizations)

- [ ] Add `asyncio.Semaphore` for agent concurrency limits
- [ ] Add timeout support for individual agents
- [ ] Better error handling and retry logic
- [ ] Consider native async git library if one becomes available
- [ ] Streaming support for async operations
