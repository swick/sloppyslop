# Event Cleanup - Per-Agent Events & Warnings ✅

## Summary

Replaced batch event `BackgroundAgentsCanceling` with per-agent `AgentCancelled` event, and converted `WorktreeRemoveFailed` from event to warning.

---

## Changes Implemented

### 1. Per-Agent Cancellation Events ✅

**Problem:** `BackgroundAgentsCanceling` reported a count of cancelled agents, but didn't identify which agents.

**Solution:** Emit `AgentCancelled` event for each individual agent that gets cancelled.

**Changes:**

- ✅ **Removed event:**
  - `BackgroundAgentsCanceling(agent_count: int)`

- ✅ **Added event:**
  - `AgentCancelled(agent_id: str)`

- ✅ **TaskManager:**
  - Added `events: EventEmitter` parameter to `__init__`
  - `cancel_all()` emits `AgentCancelled` for each agent
  - Stores event emitter as `self._events`

- ✅ **SandboxRunner:**
  - Passes `self.events` to `TaskManager` constructor
  - Removed bulk event emission in cleanup

- ✅ **Event handler:**
  ```python
  # OLD:
  lambda e: output.info(f"Canceling {e.agent_count} background agent(s)...")

  # NEW:
  lambda e: output.verbose(f"Cancelled agent: {e.agent_id}")
  ```

---

### 2. WorktreeRemoveFailed → Warning ✅

**Problem:** `WorktreeRemoveFailed` was an event but semantically is just a warning.

**Solution:** Use warning callback instead of event.

**Changes:**

- ✅ **Removed event:**
  - `WorktreeRemoveFailed(name: str, error: str)`

- ✅ **Replaced with warning:**
  ```python
  # OLD:
  self.events.emit(WorktreeRemoveFailed(name=worktree_name, error=str(e)))

  # NEW:
  self._warn(f"Failed to remove worktree {worktree_name}: {e}", "cleanup_worktrees")
  ```

- ✅ **Removed event handler:**
  - Handler displayed warning anyway, now goes through warning callback

---

## Current Event Types (5 total)

### Branch Events (1):
1. `BranchKept(original_name, new_name)` - Branch kept during cleanup

### Agent Events (4):
2. `AgentStarted(agent_id, spawn_depth)` - Agent started execution
3. `AgentCompleted(agent_id, spawn_depth)` - Agent completed
4. `AgentFailed(agent_id, error, spawn_depth)` - Agent failed
5. `AgentCancelled(agent_id)` - Agent cancelled during cleanup

---

### 3. Remove Unnecessary Events ✅

**Problem:** Several events didn't provide useful information:
- `WorktreeCreated` - Never emitted
- `BranchDeleted` - Just verbose noise during cleanup
- `BackgroundAgentSpawned` - Redundant with AgentStarted (spawn_depth indicates background)

**Solution:** Remove these events entirely.

**Changes:**

- ✅ **Removed events:**
  - `WorktreeCreated(name, path, branch)` - Never used
  - `BranchDeleted(branch_name)` - Cleanup detail
  - `BackgroundAgentSpawned(agent_id, spawn_depth, tool_count)` - Redundant with AgentStarted
  - `BackgroundAgentsWaiting(agent_ids, agent_count)` - Noise during wait
  - `BackgroundAgentsAllCompleted(agent_count)` - Noise after wait

- ✅ **Removed emissions:**
  - Removed BranchDeleted emission from cleanup code
  - Removed BackgroundAgentSpawned emission from Agent.execute()
  - Removed BackgroundAgentsWaiting/AllCompleted from WaitForAgentsTool

- ✅ **Removed event handlers:**
  - No handler needed for branch deletion (just cleanup detail)
  - No handler needed for agent spawning (AgentStarted handles it)
  - No handler needed for wait start/end (tool execution is enough)

---

## Files Modified

1. **src/llm_sandbox/runner.py**
   - Removed 7 event types:
     - `BackgroundAgentsCanceling` → per-agent `AgentCancelled`
     - `WorktreeRemoveFailed` → warning
     - `WorktreeCreated` → never used
     - `BranchDeleted` → unnecessary
     - `BackgroundAgentSpawned` → redundant
     - `BackgroundAgentsWaiting` → noise
     - `BackgroundAgentsAllCompleted` → noise
   - Added `AgentCancelled` event
   - TaskManager takes `EventEmitter` in `__init__`
   - TaskManager emits per-agent `AgentCancelled` events
   - SandboxRunner passes events to TaskManager
   - Worktree removal failure uses warning callback
   - Removed BranchDeleted emission
   - Removed BackgroundAgentSpawned emission

2. **src/llm_sandbox/event_handlers.py**
   - Updated imports (removed 7 old events, added AgentCancelled)
   - Added handler for AgentCancelled (verbose output)
   - Removed 5 event handlers:
     - WorktreeRemoveFailed
     - BranchDeleted
     - BackgroundAgentSpawned
     - BackgroundAgentsWaiting
     - BackgroundAgentsAllCompleted

3. **src/llm_sandbox/mcp_tools.py**
   - Removed BackgroundAgentsWaiting/AllCompleted emissions from WaitForAgentsTool

---

## Benefits

### ✅ Better Granularity
- Know exactly which agents were cancelled (not just count)
- Can track individual agent lifecycle more precisely

### ✅ Consistent Warning Pattern
- Worktree removal failures use warning callback (like other warnings)
- No special event for what's just a warning message

### ✅ Cleaner Event Model
- Events represent significant state changes
- Warnings represent expected/recoverable failures
- Clear separation of concerns

---

## API Changes

### Breaking Changes for Event Handlers

**Old:**
```python
runner.events.on(BackgroundAgentsCanceling, lambda e: ...)
runner.events.on(WorktreeRemoveFailed, lambda e: ...)
```

**New:**
```python
runner.events.on(AgentCancelled, lambda e: ...)
# WorktreeRemoveFailed now goes through warning_callback
```

### No Breaking Changes for:
- Agent creation
- Tool execution
- LLM provider calls
- External API

---

## Verification

✅ All files compile successfully
✅ TaskManager receives event emitter
✅ Per-agent cancellation events emitted
✅ WorktreeRemoveFailed converted to warning
✅ Event handlers updated

---

## Status: COMPLETE ✅

Event cleanup implemented successfully:
- ✅ Per-agent `AgentCancelled` events (replaces batch event)
- ✅ `WorktreeRemoveFailed` → warning
- ✅ 7 event types removed:
  - BackgroundAgentsCanceling
  - WorktreeRemoveFailed
  - WorktreeCreated
  - BranchDeleted
  - BackgroundAgentSpawned
  - BackgroundAgentsWaiting
  - BackgroundAgentsAllCompleted
- ✅ **Total events: 10 → 5 (50% reduction)**
- ✅ More granular agent tracking
- ✅ Cleaner, focused event model
- ✅ Events only for significant state changes
