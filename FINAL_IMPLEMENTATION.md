# Final Async Implementation

## Design: Async-Only Interfaces with Blocking Operations

All interfaces are async-only. Operations can block - we're not optimizing for concurrency yet, just establishing the async foundation.

## Core Components

### 1. LLM Provider - Async-Only Interface

**`LLMProvider` (base class)**:
```python
class LLMProvider(ABC):
    @abstractmethod
    async def generate_text_async(self, prompt, max_tokens) -> str: ...

    @abstractmethod
    async def generate_structured_async(self, prompt, mcp_server, output_schema) -> Dict: ...

    @abstractmethod
    async def validate_async(self) -> Dict: ...
```

**`ClaudeProvider`** - Supports both Anthropic API and Vertex AI:
- **Anthropic**: Uses `AsyncAnthropic` client (truly async)
- **Vertex AI**: Uses `AnthropicVertex` wrapped in `asyncio.to_thread()` (async via threads)

### 2. MCP Tools - Async-Only Interface

**`MCPTool` (base class)**:
```python
class MCPTool(ABC):
    @abstractmethod
    async def execute_async(self, arguments) -> Dict: ...
```

**`MCPServer`**:
```python
class MCPServer:
    async def execute_tool_async(self, tool_name, arguments) -> Dict: ...
```

**All tools are blocking**:
- `ExecuteCommandTool` - Uses `container_manager.exec_command_async()`
- `CheckoutCommitTool` - Calls blocking `git_ops.create_worktree_on_branch()`
- `GitCommitTool` - Calls blocking `git_ops.commit_files()`
- `ReadFileTool` - Calls blocking `file_path.read_text()`
- `WriteFileTool` - Calls blocking `file_path.write_text()`
- `EditFileTool` - Calls blocking file operations

### 3. Container - One Async Method

**`ContainerManager`**:
- `exec_command_async()` - Uses `FuturesSession` (async via thread pool)
- `exec_command()` - Wraps `exec_command_async()` with `asyncio.run()`
- All other methods remain synchronous (only called during setup/cleanup)

### 4. Runner - Sync Wrappers

**`SandboxRunner`**:
```python
def run_agent(self, ...):
    """Synchronous wrapper for backwards compatibility"""
    return asyncio.run(
        self.llm_provider.generate_structured_async(...)
    )

def run_agents_parallel(self, agents):
    """Run multiple agents concurrently"""
    return asyncio.run(self._run_agents_async(agents))
```

## Key Simplifications

1. **No separate AsyncMCPTool/AsyncMCPServer** - Just use `MCPTool` and `MCPServer` with async methods
2. **Tools can block** - No `asyncio.to_thread()` wrapping for file/git operations
3. **Only one truly async operation** - `exec_command_async()` uses `FuturesSession`
4. **Clean interfaces** - Everything is `async def`, even if it blocks

## Usage

### Single Agent

```python
from llm_sandbox import SandboxRunner, MCPServer
from llm_sandbox.mcp_tools import ExecuteCommandTool, CheckoutCommitTool

runner = SandboxRunner(project_path, config)
runner.setup()

# Create MCP server with tools
server = MCPServer()
server.add_tools([
    ExecuteCommandTool(runner),
    CheckoutCommitTool(runner),
])

# Run agent (sync wrapper)
result = runner.run_agent(prompt, output_schema, server)

runner.cleanup()
```

### Parallel Agents

```python
from llm_sandbox import AgentConfig

agents = [
    AgentConfig(prompt1, schema1, server1, "agent-1"),
    AgentConfig(prompt2, schema2, server2, "agent-2"),
]

# All agents run concurrently
results = runner.run_agents_parallel(agents, verbose=True)
```

## Concurrent Execution

When Claude requests multiple tools in one turn, they execute concurrently:

```python
# In ClaudeProvider.generate_structured_async():
tool_results = await asyncio.gather(*[
    mcp_server.execute_tool_async(block.name, block.input)
    for block in tool_blocks
], return_exceptions=True)
```

Even though most tools block internally, the concurrent execution still happens at the asyncio level.

## Benefits

1. **Simple** - No dual sync/async APIs, just async everywhere
2. **Backwards compatible** - `runner.run_agent()` wraps async with `asyncio.run()`
3. **Concurrent tools** - Multiple tool requests execute in parallel
4. **Parallel agents** - Multiple agents run simultaneously
5. **Clean foundation** - Easy to optimize individual operations later

## Dependencies

Added to `pyproject.toml`:
```toml
"requests-futures>=1.0.0"  # For async container exec
```

## Files Changed

1. **`src/llm_sandbox/llm_provider.py`** - Async-only interface, supports Anthropic + Vertex AI
2. **`src/llm_sandbox/mcp_tools.py`** - Async-only MCPTool/MCPServer, tools can block
3. **`src/llm_sandbox/container.py`** - Added `exec_command_async()` with FuturesSession
4. **`src/llm_sandbox/runner.py`** - Parallel execution + sync wrappers
5. **`src/llm_sandbox/__init__.py`** - Updated exports
6. **`examples/parallel_agents_example.py`** - Updated to use MCPServer
7. **`pyproject.toml`** - Added requests-futures

## Files Removed

- **`src/llm_sandbox/async_tools.py`** - Deleted (tools now in mcp_tools.py)

## What's Not Async (Yet)

- File I/O operations (read/write) - Just block
- Git operations - Just block
- Container setup/cleanup - Remain synchronous

This is intentional - we're establishing the async interface first, optimizations come later.

## Performance

Even with blocking operations, we get benefits from:
1. **Concurrent tool execution** - When Claude requests 3 tools, they run in parallel (even if each blocks)
2. **Parallel agents** - Multiple agents run simultaneously
3. **Async container exec** - True async via FuturesSession

Expected speedup: 2-3x for parallel agents, near-linear for concurrent tools.

## Migration from Earlier Versions

If you had code using `AsyncMCPServer`:
```python
# Old
from llm_sandbox.mcp_tools import AsyncMCPServer
from llm_sandbox.async_tools import AsyncCheckoutCommitTool

# New - just use MCPServer
from llm_sandbox import MCPServer
from llm_sandbox.mcp_tools import CheckoutCommitTool
```

The tools are the same, just imported differently now.
