# Spawn Depth Refactoring - Complete ✅

## Summary

Replaced `is_background` boolean with `spawn_depth` integer in Agent, and gave tools access to their executing agent.

## Changes Implemented

### 1. Replace is_background with spawn_depth ✅

**Problem:** Binary `is_background` flag doesn't convey nesting depth of spawned agents.

**Solution:** Track spawn depth as an integer (0 = foreground, >0 = background).

**Changes:**
- ✅ **Agent.__init__:**
  - Removed `is_background: bool` parameter
  - Added `spawn_depth: int = 0` parameter
  - Added `parent: Optional["Agent"] = None` parameter
  - Agent ID generation: simple UUID (no prefix, spawn_depth indicates background)
  - Stored as `self.spawn_depth` and `self.parent` instance properties

- ✅ **Agent execution:**
  - Events use `spawn_depth` directly (no is_background computation)
  - BackgroundAgentSpawned event uses `self.spawn_depth` directly
  - No more background parameter passed to `_execute()`

- ✅ **Event definitions:**
  - `AgentStarted`, `AgentCompleted`, `AgentFailed` have only `spawn_depth` field
  - Removed `is_background` completely
  - Event handlers compute background status from `spawn_depth > 0`

---

### 2. Tools Access to Agent ✅

**Problem:** Tools couldn't access the agent they're executing on.

**Solution:** Tools receive agent reference via parameter.

**Changes:**
- ✅ **MCPTool.execute() signature:**
  ```python
  # OLD:
  async def execute(self, arguments, mcp_server=None) -> Dict:

  # NEW:
  async def execute(self, arguments, mcp_server=None, agent=None) -> Dict:
  ```

- ✅ **MCPServer:**
  - Added `agent: Optional["Agent"]` property
  - Set by `Agent.__init__`: `self.mcp_server.agent = self`
  - `execute_tool()` passes agent: `tool.execute(args, mcp_server=self, agent=self.agent)`

- ✅ **All tool implementations updated:**
  - ExecuteCommandTool
  - GitCommitTool
  - CheckoutCommitTool
  - ReadFileTool
  - WriteFileTool
  - EditFileTool
  - GlobTool
  - GrepTool
  - SpawnAgentTool (uses agent.spawn_depth)
  - WaitForAgentsTool

---

### 3. Remove spawn_depth from MCPServer ✅

**Problem:** Spawn depth was property of MCPServer, but logically belongs to Agent.

**Solution:** Move spawn depth to Agent, remove from MCPServer.

**Changes:**
- ✅ **MCPServer.__init__:**
  ```python
  # OLD:
  def __init__(self, spawn_depth: int = 0):
      self.spawn_depth = spawn_depth

  # NEW:
  def __init__(self):
      self.agent: Optional["Agent"] = None
  ```

- ✅ **SpawnAgentTool:**
  ```python
  # OLD:
  parent_depth = mcp_server.spawn_depth
  child_mcp_server = MCPServer(spawn_depth=child_depth)
  agent = Agent(..., is_background=True)

  # NEW:
  parent_depth = agent.spawn_depth
  child_mcp_server = MCPServer()
  child_agent = Agent(..., spawn_depth=child_depth)
  ```

- ✅ **All MCPServer subclasses:**
  - RunMCPServer
  - GenContainerfileMCPServer
  - PRReviewMCPServer
  - All call `super().__init__()` without parameters (already correct)

---

## Files Modified

1. **src/llm_sandbox/runner.py**
   - Agent.__init__: `is_background` → `spawn_depth`
   - Agent.execute(): use spawn_depth
   - Agent._execute(): compute is_background from spawn_depth
   - Set `mcp_server.agent = self` in __init__
   - Updated event emission

2. **src/llm_sandbox/mcp_tools.py**
   - Added TYPE_CHECKING import for Agent
   - MCPTool.execute(): added `agent` parameter
   - MCPServer.__init__: removed `spawn_depth` parameter
   - MCPServer: added `agent` property
   - MCPServer.execute_tool(): pass agent to tools
   - SpawnAgentTool: use `agent.spawn_depth` instead of `mcp_server.spawn_depth`
   - SpawnAgentTool: create child with `spawn_depth` instead of `is_background`
   - All 10 tool implementations: updated execute() signatures

3. **src/llm_sandbox/subcommand.py**
   - Updated docstring example: `spawn_depth=1` instead of `is_background=True`

4. **src/llm_sandbox/event_handlers.py**
   - Updated `_format_agent_label()` to use `spawn_depth` instead of `is_background`
   - Updated all event handlers to check `e.spawn_depth > 0` instead of `e.is_background`

---

## API Changes

### Breaking Changes

**Agent constructor:**
```python
# OLD:
Agent(runner, prompt, schema, mcp_server, is_background=True)

# NEW:
Agent(runner, prompt, schema, mcp_server, spawn_depth=1, parent=parent_agent)
```

**MCPServer constructor:**
```python
# OLD:
MCPServer(spawn_depth=1)

# NEW:
MCPServer()  # spawn_depth now comes from agent
```

**MCPTool.execute():**
```python
# OLD:
async def execute(self, arguments, mcp_server=None):
    ...

# NEW:
async def execute(self, arguments, mcp_server=None, agent=None):
    # Can now access agent.spawn_depth, agent.runner, etc.
    ...
```

### Breaking Changes for Event Handlers

**Event fields:**
```python
# OLD:
AgentStarted(agent_id, is_background=True, spawn_depth=1)
AgentCompleted(agent_id, is_background=True)
AgentFailed(agent_id, error, is_background=True)

# NEW:
AgentStarted(agent_id, spawn_depth=1)
AgentCompleted(agent_id, spawn_depth=1)
AgentFailed(agent_id, error, spawn_depth=1)
```

**Event handler checks:**
```python
# OLD:
if event.is_background:
    ...

# NEW:
if event.spawn_depth > 0:
    ...
```

### Non-Breaking Changes

- Tool implementations backward compatible (agent parameter is optional)

---

## Benefits

### ✅ Clearer Semantics
- Spawn depth is now explicit (0, 1, 2, ...) instead of implicit (background vs foreground)
- Agent owns its depth (not the MCPServer)
- Tools can query depth for recursion limits

### ✅ Tool Capabilities
- Tools have full access to their executing agent
- Can query `agent.spawn_depth` for recursion control
- Can access `agent.runner` for sandbox operations
- Can access `agent.mcp_server` for tool introspection

### ✅ Consistent Model
- Agent depth is property of Agent (not MCPServer)
- MCPServer is just a tool registry (no state)
- Clear ownership: Agent owns depth, MCPServer owns tools

---

## Verification

✅ All files compile successfully
✅ No references to old `is_background` parameter in Agent
✅ All tool signatures updated with agent parameter
✅ SpawnAgentTool uses agent.spawn_depth correctly
✅ Events use spawn_depth only (is_background completely removed)

---

## Migration Notes

**For tool implementations:**
- Add `agent` parameter to execute() signature
- Access agent via parameter: `agent.spawn_depth`, `agent.runner`, etc.
- Backward compatible: agent parameter is optional (defaults to None)

**For agent creation:**
- Replace `is_background=True` with `spawn_depth=1` (or higher)
- Add `parent=parent_agent` to track agent hierarchy
- Spawn depth 0 = foreground agent
- Spawn depth > 0 = background agent

**For MCPServer subclasses:**
- Remove `spawn_depth` parameter from `super().__init__()` call
- If you need spawn depth, access via `self.agent.spawn_depth`

---

## Status: COMPLETE ✅

All changes implemented and verified.

**Impact:**
- Agent API: `is_background` → `spawn_depth` parameter
- MCPServer API: removed `spawn_depth` parameter
- Tool API: added `agent` parameter
- Events: no changes (backward compatible)
