# Vertex AI Support - Implementation Summary

## Overview

Added support for using Claude models through Google Cloud Vertex AI as an alternative backend to Anthropic's direct API.

## Key Changes

### 1. Configuration (`config.py`)

**Updated `ProviderConfig`:**
```python
class ProviderConfig(BaseModel):
    api_key_env: str
    model: str
    backend: str = "anthropic"  # NEW: "anthropic" or "vertex-ai"
    # Vertex AI specific fields
    region: Optional[str] = None  # NEW: GCP region (e.g., "us-east5")
    project_id: Optional[str] = None  # NEW: GCP project ID
```

**Backward Compatible:**
- Default `backend="anthropic"` maintains existing behavior
- Existing configurations continue to work without changes

### 2. LLM Provider (`llm_provider.py`)

**Updated `ClaudeProvider.__init__()`:**
- Detects `backend` configuration
- Uses `AnthropicVertex` client for Vertex AI
- Uses `Anthropic` client for direct API
- Validates required fields for each backend

**Implementation:**
```python
if provider_config.backend == "vertex-ai":
    # Vertex AI backend
    self.client = AnthropicVertex(
        region=provider_config.region,
        project_id=provider_config.project_id,
    )
else:
    # Direct Anthropic API backend
    self.client = Anthropic(api_key=api_key)
```

### 3. Dependencies

**No changes needed:**
- Anthropic SDK (>= 0.39.0) includes `AnthropicVertex` client
- Uses Google Cloud Application Default Credentials automatically

### 4. Documentation

**Added:**
- `VERTEX_AI_SUPPORT.md` - Complete guide for users
- `config.example.yaml` - Configuration examples for both backends
- `VERTEX_AI_IMPLEMENTATION.md` - This file (implementation details)

**Added Tests:**
- `tests/test_vertex_ai_config.py` - Configuration and provider tests

## Backend Comparison

| Feature | Anthropic API | Vertex AI |
|---------|---------------|-----------|
| **Authentication** | API key | GCP credentials (ADC) |
| **Configuration** | `api_key_env` | `region`, `project_id` |
| **Client Class** | `Anthropic` | `AnthropicVertex` |
| **API Interface** | Same | Same |
| **Features** | All features | All features |
| **Billing** | Anthropic | Google Cloud |

## Configuration Examples

### Direct Anthropic API (Default)

```yaml
llm:
  default_provider: anthropic
  providers:
    anthropic:
      api_key_env: ANTHROPIC_API_KEY
      model: claude-sonnet-4-5
      backend: anthropic  # Optional, default
```

### Vertex AI

```yaml
llm:
  default_provider: vertex-ai
  providers:
    vertex-ai:
      api_key_env: UNUSED  # Required field but not used
      model: claude-sonnet-4-5
      backend: vertex-ai
      region: us-east5
      project_id: my-gcp-project
```

### Both Backends (Switchable)

```yaml
llm:
  default_provider: anthropic  # Change to switch default
  providers:
    anthropic:
      api_key_env: ANTHROPIC_API_KEY
      model: claude-sonnet-4-5
      backend: anthropic

    vertex-ai:
      api_key_env: UNUSED
      model: claude-sonnet-4-5
      backend: vertex-ai
      region: us-east5
      project_id: my-gcp-project
```

## Usage

### Current Implementation

Users can switch backends by changing `default_provider` in config:

```yaml
llm:
  default_provider: vertex-ai  # or anthropic
```

### Future Enhancement (Not Yet Implemented)

Add CLI flag to select provider:

```bash
llm-sandbox run --provider vertex-ai ...
```

This would require updating `__main__.py` to accept and pass provider parameter.

## Authentication

### Anthropic API

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Vertex AI

```bash
# Option 1: User credentials
gcloud auth application-default login

# Option 2: Service account
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json"

# Option 3: Automatic on GCP (Compute Engine, Cloud Run, etc.)
```

## Error Handling

### Vertex AI Validation

The provider validates required fields at initialization:

```python
if not provider_config.region:
    raise ValueError("Vertex AI backend requires 'region' configuration")

if not provider_config.project_id:
    raise ValueError("Vertex AI backend requires 'project_id' configuration")
```

### Anthropic API Validation

```python
api_key = os.getenv(provider_config.api_key_env)
if not api_key:
    raise ValueError(f"API key not found. Set {provider_config.api_key_env}")
```

## Testing

### Test Coverage

**Configuration Tests:**
- ProviderConfig with Vertex AI fields
- Default backend is "anthropic"
- Optional fields are stored but not required

**Provider Tests:**
- Initialize with Vertex AI backend
- Initialize with Anthropic backend
- Vertex AI requires region
- Vertex AI requires project_id
- Anthropic requires API key
- Both backends use same interface

### Running Tests

```bash
pytest tests/test_vertex_ai_config.py -v
```

## Implementation Notes

### Why AnthropicVertex?

The Anthropic Python SDK provides `AnthropicVertex` specifically for Vertex AI integration:
- Handles GCP authentication automatically
- Uses Application Default Credentials
- Same API interface as `Anthropic` client
- No additional dependencies needed

### Backward Compatibility

All existing configurations continue to work:
- Default `backend="anthropic"`
- `region` and `project_id` are optional
- Only used when `backend="vertex-ai"`

### Future Enhancements

1. **CLI Provider Selection:**
   ```bash
   llm-sandbox run --provider vertex-ai ...
   ```

2. **Per-Project Override:**
   Allow `.llm-sandbox/config.yaml` to override provider

3. **Auto-detection:**
   Detect GCP environment and suggest Vertex AI

4. **Multiple Regions:**
   Support multiple Vertex AI configurations for different regions

## Files Modified

1. **src/llm_sandbox/config.py**
   - Added `backend`, `region`, `project_id` to `ProviderConfig`

2. **src/llm_sandbox/llm_provider.py**
   - Imported `AnthropicVertex`
   - Updated `ClaudeProvider.__init__()` to support both backends
   - Added validation for required fields

## Files Added

1. **VERTEX_AI_SUPPORT.md** - User documentation
2. **config.example.yaml** - Configuration examples
3. **tests/test_vertex_ai_config.py** - Tests
4. **VERTEX_AI_IMPLEMENTATION.md** - This file

## Dependencies

**No changes needed:**
- `anthropic>=0.39.0` already includes Vertex AI support
- Uses system's GCP credentials (no new packages)

## Security Considerations

### API Keys

- Anthropic API keys stored in environment variables (not in config files)
- Never commit API keys to version control

### GCP Credentials

- Uses Application Default Credentials (ADC)
- Service account keys should be protected
- Recommended: Use workload identity on GCP

### Configuration Files

- Config files may contain GCP project IDs (not sensitive)
- Region information is public
- No secrets should be in config files

## Troubleshooting

Common issues and solutions documented in `VERTEX_AI_SUPPORT.md`:
- Missing authentication
- Missing permissions
- Invalid regions
- Model availability

## Testing Vertex AI Integration

### Manual Testing

1. **Configure Vertex AI:**
   ```yaml
   # ~/.config/llm-sandbox/config.yaml
   llm:
     default_provider: vertex-ai
     providers:
       vertex-ai:
         api_key_env: UNUSED
         model: claude-sonnet-4-5
         backend: vertex-ai
         region: us-east5
         project_id: YOUR_PROJECT_ID
   ```

2. **Authenticate:**
   ```bash
   gcloud auth application-default login
   ```

3. **Run Test:**
   ```bash
   llm-sandbox run \
     --prompt "Echo: Hello from Vertex AI" \
     --schema '{"type":"object","properties":{"result":{"type":"string"}}}'
   ```

### Automated Testing

Tests use mocking to avoid requiring actual GCP credentials:

```python
@patch("llm_sandbox.llm_provider.AnthropicVertex")
def test_initialize_with_vertex_ai(mock_vertex):
    # Test runs without real GCP credentials
    ...
```

## Summary

The Vertex AI support is:
- ✅ **Fully backward compatible** - Existing configs work unchanged
- ✅ **Production ready** - Uses official Anthropic SDK
- ✅ **Well tested** - Unit tests for all scenarios
- ✅ **Well documented** - User and implementation guides
- ✅ **Secure** - Uses GCP best practices for authentication
- ✅ **Simple** - Just 3 new optional config fields
