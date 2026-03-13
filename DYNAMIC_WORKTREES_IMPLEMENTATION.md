# Dynamic Worktree Management - Implementation Summary

## Overview

Successfully implemented dynamic, on-demand worktree creation for the LLM Container Sandbox. The LLM can now create multiple worktrees from any commit during execution instead of being limited to a single pre-created worktree.

## Architecture Decision: Host Git Operations

**Important:** All git operations execute on the host using GitOperations class:

- **CheckoutCommitTool**: Executes on host using `runner.git_ops.create_worktree_on_branch()`
  - Creates worktrees on the host filesystem
  - Worktrees are then visible in container via `/worktrees` mount
  - Uses GitPython for consistent git operations

- **GitCommitTool**: Executes on host using `runner.git_ops.commit_files()`
  - Commits are made on the host git repository
  - Worktrees are mounted at `/worktrees` in container (read-write)
  - Uses GitPython for consistent git operations

**Why host execution?**
- Container has read-only access to `/project` (main repo)
- Git operations must modify the host repository
- Consistent use of GitOperations class (GitPython)
- Worktrees visible both in container and on host

## Key Changes

### 1. Instance Management (runner.py)

**Added:**
- `instance_id`: Unique identifier for each container run (format: `YYYYMMDD-HHMMSS-{uuid}`)
- `worktrees_base_dir`: Base directory for all worktrees in this instance
- `created_worktrees`: List tracking all worktree names created during execution
- `_generate_instance_id()`: Generates unique instance IDs
- `_cleanup_worktrees()`: Intelligent cleanup that:
  - Keeps specified output branches (already in main repo)
  - Removes all worktrees
  - Deletes non-output branches
  - Cleans up instance directory

**Modified:**
- `run_prompt()`: Now creates empty worktrees directory instead of single worktree
- Container initialization passes instance context to MCP server
- Cleanup uses new `_cleanup_worktrees()` method

### 2. Git Operations (git_ops.py)

**Added:**
- `create_worktree_on_branch()`: Creates worktree from commit on a new branch (uses GitPython)
  - Branch is created **directly in main repository**
  - Worktree is a checkout of that branch
- `delete_branch()`: Deletes branches (uses GitPython, with error suppression for robustness)
- `commit_files()`: Commits files in a worktree (uses GitPython, used by GitCommitTool)
  - Commits go directly to the branch in the main repository

**Removed (old architecture):**
- `pull_branch_to_repo()`: No longer needed - branches already exist in main repo
- `pull_branches()`: No longer needed - branches already exist in main repo
- `get_worktree_branches()`: No longer needed - we track worktrees via runner
- `create_worktree()`: Replaced by `create_worktree_on_branch()`
- `get_commit_hash()`: No longer used in new architecture

**Refactored:**
- All git operations now use GitPython module instead of subprocess calls
- Consistent error handling with git.GitCommandError

### 3. MCP Tools (mcp_tools.py)

**New Tool: CheckoutCommitTool**
- Creates worktrees on-demand from any commit using `GitOperations.create_worktree_on_branch()`
- Auto-generates worktree names if not specified (format: `wt-{uuid}`)
- Validates worktree names (pattern: `[a-zA-Z0-9_-]+`)
- Prevents duplicate worktree names
- Creates branches with pattern: `llm-container/{instance-id}/{worktree-name}`
- Returns worktree info including path and branch name
- Executes on host, worktrees visible in container via `/worktrees` mount

**Modified: GitCommitTool**
- Branch parameter now **REQUIRED** (breaking change)
- Validates branch matches pattern: `llm-container/{instance-id}/{worktree-name}`
- Automatically derives worktree path from branch name
- **Executes git commands on the host** (not in container) using `GitOperations.commit_files()`
- Works with any worktree created by CheckoutCommitTool
- Requires reference to runner for accessing worktree base directory and git operations

**Modified: ExecuteCommandTool**
- Default working directory changed from `/workspace` to `/worktrees`

### 4. Container Management (container.py)

**Modified:**
- Mount point changed from `/workspace:rw` to `/worktrees:rw`
- Working directory changed from `/workspace` to `/worktrees`
- Parameter renamed from `worktree_mount` to `worktrees_mount`

### 5. CLI (__main__.py)

**Modified:**
- `--pull-branches`: Help text updated to clarify it expects worktree names (not branch names)
- Semantic change: parameter now specifies which worktrees to keep as output branches

## Branch Naming Convention

All branches follow a strict pattern for isolation and cleanup:

```
llm-container/{instance-id}/{worktree-name}
```

Example:
```
llm-container/20260313-152345-a7b3c2/my-feature
```

This ensures:
- Complete isolation between container runs
- Easy identification of temporary branches
- Safe cleanup without affecting user branches

## Worktree Lifecycle

1. **Creation**: LLM calls `checkout_commit` tool
   - Creates worktree at `/worktrees/{worktree-name}/`
   - Creates branch `llm-container/{instance-id}/{worktree-name}`
   - Tracks in `runner.created_worktrees`

2. **Usage**: LLM works in worktree
   - Executes commands in worktree directory
   - Commits using `git_commit` with required branch parameter
   - Can create multiple worktrees as needed

3. **Cleanup**: Automatic at end of run
   - Output branches: kept in main repo (already there)
   - All worktrees: removed from disk
   - Non-output branches: deleted from repository
   - Instance directory: removed

## Migration Guide

### Old Workflow
```bash
# Single worktree pre-created from commit
llm-sandbox run \
  --commit main \
  --pull-branches my-branch \
  --prompt "Make changes and commit to my-branch"
```

### New Workflow
```bash
# No pre-created worktree, LLM creates dynamically
llm-sandbox run \
  --commit main \
  --pull-branches my-work \
  --prompt "Use checkout_commit to create 'my-work' from main, make changes, commit"
```

### Key Differences
1. `--commit` parameter still exists but doesn't create a worktree
2. `--pull-branches` expects worktree names (not branch names)
3. LLM must explicitly call `checkout_commit` before working
4. Branch parameter is required in `git_commit`
5. All branches follow `llm-container/{instance-id}/{worktree-name}` pattern

## Example LLM Workflows

### Single Worktree
```
LLM: checkout_commit(commit="main", worktree_name="bugfix")
→ Returns: {branch: "llm-container/.../bugfix", path: "/worktrees/bugfix"}

LLM: execute_command(command="cd /worktrees/bugfix && vim src/auth.py")

LLM: git_commit(
  files=["src/auth.py"],
  message="Fix auth bug",
  branch="llm-container/.../bugfix"
)

User specifies: --pull-branches bugfix
Result: Branch pulled to main repo
```

### Multiple Worktrees
```
LLM: checkout_commit(commit="v1.0", worktree_name="version-1")
LLM: checkout_commit(commit="v2.0", worktree_name="version-2")

LLM: execute_command(command="diff -r /worktrees/version-1 /worktrees/version-2")

User specifies: --pull-branches (none)
Result: All worktrees cleaned up, no branches kept
```

## Testing Checklist

- [x] Module imports work correctly
- [x] CLI help text updated
- [ ] Single worktree creation and commit
- [ ] Multiple worktree creation
- [ ] Output branch selection and pulling
- [ ] Branch validation (invalid patterns rejected)
- [ ] Duplicate worktree name detection
- [ ] Cleanup verification (no leftover directories/branches)
- [ ] Auto-generated worktree names
- [ ] Invalid commit handling

## Security Considerations

1. **Path Traversal**: Worktree names validated with strict regex
2. **Branch Name Injection**: Pattern validation before git commands
3. **Instance Isolation**: Unique instance IDs prevent cross-run conflicts
4. **Container Access**: Same as before - `/project:ro`, `/worktrees:rw`
5. **Cleanup Robustness**: Errors suppressed to ensure cleanup completes

## Files Modified

1. `src/llm_sandbox/runner.py` - Instance management, cleanup logic
2. `src/llm_sandbox/git_ops.py` - New git operations
3. `src/llm_sandbox/mcp_tools.py` - New CheckoutCommitTool, modified GitCommitTool
4. `src/llm_sandbox/container.py` - Mount point changes
5. `src/llm_sandbox/__main__.py` - CLI help text update

## Implementation Complete

All phases of the plan have been implemented:

- ✅ Phase 1: Foundation (instance ID, git operations)
- ✅ Phase 2: New MCP Tools (checkout_commit, modified git_commit)
- ✅ Phase 3: Container & Runner Changes (mounts, cleanup)
- ✅ Phase 4: CLI Updates (parameter documentation)

The implementation is ready for testing and integration.
