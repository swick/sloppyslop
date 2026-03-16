# Warning Callback Method Change ✅

## Summary

Changed the warning callback from a constructor parameter to a setter method for cleaner API design.

---

## Changes Implemented

### Problem

Warning callback was a constructor parameter, mixing setup concerns:
```python
runner = SandboxRunner(
    path,
    config,
    verbose=True,
    warning_callback=lambda msg, ctx: ...  # Mixed with construction
)
```

### Solution

Use a setter method to separate construction from configuration:
```python
runner = SandboxRunner(path, config, verbose=True)
runner.set_warning_callback(lambda msg, ctx: ...)  # Configure separately
```

---

## Changes Made

### 1. SandboxRunner ✅

**Updated `__init__`:**
```python
# OLD:
def __init__(self, ..., warning_callback: Optional[callable] = None):
    self._warning_callback = warning_callback

# NEW:
def __init__(self, ...):  # Removed parameter
    self._warning_callback: Optional[callable] = None  # Initialize to None
```

**Added `set_warning_callback` method:**
```python
def set_warning_callback(self, callback: callable) -> None:
    """Set the warning callback for receiving warnings.

    Args:
        callback: Function that receives (message: str, context: str)
    """
    self._warning_callback = callback
```

### 2. All Subcommands ✅

Updated all three subcommands to use the new method:

**run subcommand:**
```python
# OLD:
runner = SandboxRunner(..., warning_callback=lambda msg, ctx: ...)

# NEW:
runner = SandboxRunner(...)
runner.set_warning_callback(lambda msg, ctx: output.warning(...))
```

**gen_containerfile subcommand:**
```python
# Same pattern
runner = SandboxRunner(...)
runner.set_warning_callback(lambda msg, ctx: output.warning(...))
```

**review subcommand:**
```python
# Same pattern
runner = SandboxRunner(...)
runner.set_warning_callback(lambda msg, ctx: output.warning(...))
```

---

## Files Modified

1. **src/llm_sandbox/runner.py**
   - Removed `warning_callback` parameter from `__init__`
   - Initialize `_warning_callback` to None
   - Added `set_warning_callback()` method

2. **src/llm_sandbox/subcommands/run/subcommand.py**
   - Removed parameter from constructor
   - Added `runner.set_warning_callback()` call

3. **src/llm_sandbox/subcommands/gen_containerfile/subcommand.py**
   - Removed parameter from constructor
   - Added `runner.set_warning_callback()` call

4. **src/llm_sandbox/subcommands/review/subcommand.py**
   - Removed parameter from constructor
   - Added `runner.set_warning_callback()` call

---

## Benefits

### ✅ Cleaner API
- Separates construction from configuration
- Constructor focused on essential setup
- Optional features configured via methods

### ✅ More Flexible
- Callback can be set/changed after construction
- Callback can be None (warnings ignored)
- Can be set conditionally

### ✅ Better Separation of Concerns
- Construction: runner setup
- Configuration: callbacks, handlers
- Execution: running agents

---

## API Changes

### Breaking Change for Direct Users

**Old API:**
```python
runner = SandboxRunner(
    path,
    config,
    warning_callback=my_callback
)
```

**New API:**
```python
runner = SandboxRunner(path, config)
runner.set_warning_callback(my_callback)
```

### No Breaking Changes for:
- CLI users (internal implementation detail)
- Event system
- Agent creation
- Tool execution

---

## Verification

✅ All files compile successfully
✅ No remaining `warning_callback=` parameters
✅ All subcommands use `set_warning_callback()` method
✅ Warning functionality preserved

---

## Status: COMPLETE ✅

Warning callback successfully moved to setter method:
- ✅ Cleaner constructor API
- ✅ More flexible configuration
- ✅ All subcommands updated
- ✅ No breaking changes for CLI users
