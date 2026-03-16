# Agent Parameter Refactoring - Complete ✅

## Summary

Simplified the LLM provider and tool interfaces to use `agent` as the single parameter instead of passing both `mcp_server` and `agent` separately.

## Changes Implemented

### 1. LLMProvider.generate_structured takes Agent ✅

**Problem:** Method took `mcp_server` parameter but really needs the full agent context.

**Solution:** Pass `agent` instead, access `agent.mcp_server` internally.

**Changes:**
- ✅ **LLMProvider.generate_structured signature:**
  ```python
  # OLD:
  async def generate_structured(self, prompt, mcp_server, output_schema, ...)

  # NEW:
  async def generate_structured(self, prompt, agent, output_schema, ...)
  ```

- ✅ **Implementation:**
  - Access MCP server via `agent.mcp_server`
  - Build system prompt using `agent.mcp_server`
  - Pass agent to `_execute_single_tool()`

---

### 2. _execute_single_tool takes Agent ✅

**Problem:** Method took `mcp_server` parameter when it should use agent.

**Solution:** Take `agent` parameter, access `agent.mcp_server.execute_tool()`.

**Changes:**
- ✅ **_execute_single_tool signature:**
  ```python
  # OLD:
  async def _execute_single_tool(self, mcp_server, block)

  # NEW:
  async def _execute_single_tool(self, agent, block)
  ```

- ✅ **Implementation:**
  - Call `agent.mcp_server.execute_tool()` instead of `mcp_server.execute_tool()`

---

### 3. MCPTool.execute takes only Agent ✅

**Problem:** Tools received both `mcp_server` and `agent` parameters, creating redundancy.

**Solution:** Tools receive only `agent`, access `agent.mcp_server` when needed.

**Changes:**
- ✅ **MCPTool.execute signature:**
  ```python
  # OLD:
  async def execute(self, arguments, mcp_server=None, agent=None)

  # NEW:
  async def execute(self, arguments, agent: Optional["Agent"])
  ```

- ✅ **All 10 tool implementations updated:**
  - ExecuteCommandTool
  - GitCommitTool
  - CheckoutCommitTool
  - ReadFileTool
  - WriteFileTool
  - EditFileTool
  - GlobTool
  - GrepTool
  - SpawnAgentTool (uses `agent.mcp_server` and `agent.spawn_depth`)
  - WaitForAgentsTool

---

### 4. MCPServer.execute_tool passes only Agent ✅

**Problem:** Server was passing both mcp_server and agent to tools.

**Solution:** Only pass agent (which contains mcp_server reference).

**Changes:**
- ✅ **MCPServer.execute_tool:**
  ```python
  # OLD:
  await tool.execute(arguments, mcp_server=self, agent=self.agent)

  # NEW:
  await tool.execute(arguments, self.agent)
  ```

---

### 5. Agent execution passes self to LLM provider ✅

**Problem:** Agent passed `self.mcp_server` to generate_structured.

**Solution:** Agent passes `self` (entire agent context).

**Changes:**
- ✅ **Agent._execute:**
  ```python
  # OLD:
  result = await self._llm_provider.generate_structured(
      self.prompt,
      self.mcp_server,
      self.output_schema,
      ...
  )

  # NEW:
  result = await self._llm_provider.generate_structured(
      self.prompt,
      self,
      self.output_schema,
      ...
  )
  ```

---

### 6. Handle direct tool calls (review engine) ✅

**Problem:** Review engine calls tools directly without agent context.

**Solution:** Agent parameter is Optional, pass None when called outside agent.

**Changes:**
- ✅ **Tool signature uses Optional:**
  ```python
  async def execute(self, arguments, agent: Optional["Agent"])
  ```

- ✅ **Review engine passes None:**
  ```python
  await checkout_tool.execute({"commit": ref, ...}, agent=None)
  ```

---

## Files Modified

1. **src/llm_sandbox/llm_provider.py**
   - Added TYPE_CHECKING import for Agent
   - generate_structured: `mcp_server` → `agent` parameter
   - _execute_single_tool: `mcp_server` → `agent` parameter
   - Access MCP server via `agent.mcp_server`

2. **src/llm_sandbox/mcp_tools.py**
   - MCPTool.execute: only takes `agent` (not mcp_server)
   - All 10 tool implementations updated
   - MCPServer.execute_tool: only passes agent
   - SpawnAgentTool: uses `agent.mcp_server` and `agent.spawn_depth`
   - Agent parameter is Optional for direct calls

3. **src/llm_sandbox/runner.py**
   - Agent._execute: passes `self` instead of `self.mcp_server`

4. **src/llm_sandbox/subcommands/review/engine.py**
   - checkout_tool.execute: passes `agent=None`

---

## API Changes

### Breaking Changes

**LLMProvider.generate_structured:**
```python
# OLD:
await llm_provider.generate_structured(prompt, mcp_server, schema)

# NEW:
await llm_provider.generate_structured(prompt, agent, schema)
```

**Tool.execute:**
```python
# OLD:
await tool.execute(arguments, mcp_server=server, agent=agent)

# NEW:
await tool.execute(arguments, agent)
# Or for direct calls:
await tool.execute(arguments, None)
```

### Non-Breaking Changes

- Agent parameter is Optional, so direct tool calls still work
- Tools can access everything they need via agent:
  - `agent.mcp_server` - for MCP server access
  - `agent.runner` - for runner access
  - `agent.spawn_depth` - for spawn depth

---

## Benefits

### ✅ Simpler Signatures
- Single parameter (`agent`) instead of multiple (`mcp_server`, `agent`)
- No redundancy - agent contains mcp_server reference
- Clearer ownership model

### ✅ More Context Available
- Tools have full agent context, not just MCP server
- Can access runner, spawn_depth, etc. when needed
- Future-proof for additional agent properties

### ✅ Consistent Pattern
- All tool calls go through agent
- LLM provider works with agent context
- Single source of truth for execution context

---

## Verification

✅ All files compile successfully
✅ No references to old mcp_server parameter in tool calls
✅ SpawnAgentTool uses agent.mcp_server correctly
✅ Review engine handles direct tool calls with agent=None
✅ LLMProvider accesses agent.mcp_server internally

---

## Migration Notes

**For tool implementations:**
- Change signature from `execute(self, arguments, mcp_server=None, agent=None)`
  to `execute(self, arguments, agent: Optional["Agent"])`
- Access MCP server via `agent.mcp_server` (check agent is not None first if needed)
- Access runner via `agent.runner`
- Access spawn depth via `agent.spawn_depth`

**For LLM provider calls:**
- Pass `agent` instead of `mcp_server`
- Access MCP server via `agent.mcp_server` internally

**For direct tool calls (outside agent context):**
- Pass `agent=None` as second parameter
- Tool must handle None case appropriately

---

## Status: COMPLETE ✅

All changes implemented and verified.

**Impact:**
- LLMProvider API: takes `agent` instead of `mcp_server`
- Tool API: takes only `agent` (not separate mcp_server + agent)
- Agent execution: passes `self` to LLM provider
- Review engine: passes `agent=None` for direct calls
