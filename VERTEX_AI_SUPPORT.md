# Google Cloud Vertex AI Support

This document explains how to use Claude models through Google Cloud Vertex AI instead of Anthropic's direct API.

## Overview

The LLM Sandbox supports two backends for Claude models:

1. **Anthropic API** (direct) - Uses Anthropic's API directly
2. **Vertex AI** - Uses Claude models through Google Cloud's Vertex AI

Both backends provide the same functionality and use the same Anthropic SDK.

## Why Use Vertex AI?

- **Enterprise billing**: Use your existing GCP billing
- **Compliance**: Some organizations require using GCP services
- **Regional availability**: Access Claude in specific GCP regions
- **Integration**: Easier integration with other GCP services

## Prerequisites

### For Vertex AI Backend:

1. **GCP Project** with Vertex AI API enabled
2. **Authentication** configured (Application Default Credentials)
3. **Claude models** enabled in your GCP project/region

### Enable Vertex AI:

```bash
# Authenticate
gcloud auth application-default login

# Enable Vertex AI API
gcloud services enable aiplatform.googleapis.com

# Verify your project ID
gcloud config get-value project
```

## Configuration

### Option 1: Global Configuration File

Create or edit `~/.config/llm-sandbox/config.yaml`:

```yaml
llm:
  default_provider: vertex-ai

  providers:
    vertex-ai:
      api_key_env: ANTHROPIC_API_KEY  # Required field but not used for Vertex AI
      model: claude-sonnet-4-5
      backend: vertex-ai
      region: us-east5              # Your GCP region
      project_id: my-gcp-project    # Your GCP project ID
```

### Option 2: Keep Both Backends

```yaml
llm:
  default_provider: anthropic  # Use Anthropic by default

  providers:
    # Direct Anthropic API
    anthropic:
      api_key_env: ANTHROPIC_API_KEY
      model: claude-sonnet-4-5
      backend: anthropic

    # Vertex AI (alternative)
    vertex-ai:
      api_key_env: ANTHROPIC_API_KEY  # Not used but required
      model: claude-sonnet-4-5
      backend: vertex-ai
      region: us-east5
      project_id: my-gcp-project
```

## Available Regions

Claude models are available in specific GCP regions. Check availability:

```bash
# List available regions for your project
gcloud ai models list --region=us-east5
```

Common regions:
- `us-east5` (Columbus, Ohio)
- `us-central1` (Iowa)
- `europe-west1` (Belgium)
- `europe-west4` (Netherlands)

## Usage

### Using Default Provider

If you set `default_provider: vertex-ai` in config:

```bash
llm-sandbox run \
  --prompt "Analyze this code" \
  --schema schema.json
```

### Explicitly Selecting Provider

If you want to switch between providers:

```bash
# Use Vertex AI (if configured)
llm-sandbox run \
  --provider vertex-ai \
  --prompt "Analyze this code" \
  --schema schema.json

# Use direct Anthropic API
llm-sandbox run \
  --provider anthropic \
  --prompt "Analyze this code" \
  --schema schema.json
```

**Note:** The `--provider` flag is not yet implemented. Currently, only `default_provider` from config is supported.

## Authentication

### Vertex AI Authentication

Vertex AI uses Google Cloud Application Default Credentials (ADC):

```bash
# Method 1: User credentials
gcloud auth application-default login

# Method 2: Service account (for CI/CD)
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"

# Method 3: Compute Engine / Cloud Run (automatic)
# No manual authentication needed when running on GCP
```

### Direct Anthropic API Authentication

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Model Names

The model names are the same for both backends:

- `claude-opus-4`
- `claude-sonnet-4-5`
- `claude-sonnet-4`
- `claude-haiku-4`

## Troubleshooting

### Error: "API key not found"

Even though Vertex AI doesn't use API keys, the `api_key_env` field is required in the configuration. Set it to any value:

```yaml
api_key_env: UNUSED
```

### Error: "Vertex AI backend requires 'region'"

Ensure you've configured the `region` field:

```yaml
backend: vertex-ai
region: us-east5
```

### Error: "Vertex AI backend requires 'project_id'"

Ensure you've configured your GCP project ID:

```yaml
backend: vertex-ai
project_id: my-gcp-project
```

### Error: "Permission denied"

Ensure you have the necessary GCP permissions:

```bash
# Check current authentication
gcloud auth list

# Check project
gcloud config get-value project

# Test Vertex AI access
gcloud ai models list --region=us-east5
```

Required IAM roles:
- `roles/aiplatform.user` or
- `roles/aiplatform.admin`

## Cost Comparison

Pricing may vary between Anthropic's direct API and Vertex AI. Check current pricing:

- **Anthropic**: https://www.anthropic.com/pricing
- **Vertex AI**: https://cloud.google.com/vertex-ai/generative-ai/pricing

Note: Vertex AI pricing may include additional GCP infrastructure costs.

## Example Configuration

See `config.example.yaml` in the project root for a complete configuration example.

## Implementation Details

The implementation uses the Anthropic Python SDK's built-in Vertex AI support:

- **Direct API**: Uses `anthropic.Anthropic` client
- **Vertex AI**: Uses `anthropic.AnthropicVertex` client

Both clients provide the same interface, ensuring feature parity.
