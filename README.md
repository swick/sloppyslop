# LLM Container Sandbox

A safe isolated execution environment for LLM code analysis and modification using containers and git worktrees.

## Features

- **Isolated Execution**: Run LLM code analysis in isolated containers with configurable network access
- **Git Worktree Management**: Dynamic creation of git worktrees for safe code exploration and modification
- **Structured Output**: JSON schema-based structured output from LLM interactions
- **Extensible Subcommands**: Create custom workflows with the subcommand system
- **Multiple LLM Providers**: Support for both Anthropic API and Google Cloud Vertex AI
- **Containerfile Generation**: LLM-assisted Containerfile creation for custom environments

## Quick Start

### Installation

```bash
pip install -e .
```

### Configuration

The tool uses a two-tier configuration system:
- **Global config**: `~/.config/llm-sandbox/config.yaml` (or `$XDG_CONFIG_HOME/llm-sandbox/config.yaml`)
- **Project config**: `.llm-sandbox/config.yaml` (in your project directory)

#### Using Anthropic API

Set your API key:
```bash
export ANTHROPIC_API_KEY="your-api-key"
```

Check configuration:
```bash
llm-sandbox check
```

#### Using Google Cloud Vertex AI

Set environment variables:
```bash
export CLAUDE_CODE_USE_VERTEX=1
export ANTHROPIC_VERTEX_PROJECT_ID="your-gcp-project"
```

Authenticate:
```bash
gcloud auth application-default login
```

Check configuration:
```bash
llm-sandbox check
```

### Generate a Containerfile

```bash
llm-sandbox gen-containerfile my-project-env
```

This creates:
- `.llm-sandbox/Containerfile` - Generated containerfile
- `.llm-sandbox/config.yaml` - Project configuration

**Why this is needed:** The Containerfile defines the isolated environment where the LLM operates. It should include all tools and dependencies that the LLM might need to analyze or modify your project.

**Important:** You can and should edit the generated Containerfile to customize it for your project:

- **Pre-install tools**: Add any tools the LLM should use (e.g., `jq`, `ripgrep`, language-specific linters, build tools). While the LLM can install tools during execution, it's much faster and more reliable if they're already in the image.

- **Add project dependencies**: Include compilers, interpreters, package managers, or other tools specific to your project's language/framework.

- **Update as your project evolves**: When your project's dependencies change, update the Containerfile and rebuild with `llm-sandbox build --force`.

Example additions to a Containerfile:
```dockerfile
# Add Python tools for a Python project
RUN pip install ruff black mypy pytest

# Add Node.js tools for a JS project
RUN npm install -g eslint prettier typescript

# Add general development tools
RUN dnf install -y ripgrep fd-find jq
```

### Build the Container Image

```bash
llm-sandbox build
```

### Run an Analysis

```bash
llm-sandbox run \
  --prompt "Analyze this project and suggest improvements" \
  --schema '{"type":"object","properties":{"suggestions":{"type":"array","items":{"type":"string"}}}}' \
  --keep-branch improvements
```

The LLM can use the `checkout_commit` tool to create worktrees from any commit/branch it needs during execution.

## Core Concepts

### Git Worktrees

The LLM operates on git worktrees created dynamically in the container:
- Each worktree is isolated in `/worktrees/{name}`
- Worktrees can have hierarchical names like `feature/auth`
- Changes are committed to branches: `llm-container/{instance-id}/{name}`
- Use `--keep-branch` to preserve specific branches after cleanup

### Branch Management

- During execution, branches are created with pattern: `llm-container/{instance-id}/{name}`
- After execution, specified branches are renamed: `{name}` (removing the prefix)
- All other temporary branches are automatically cleaned up
- Use `llm-sandbox cleanup` to remove all temporary worktrees and branches

### Subcommands

Create custom workflows by defining subcommands. See [examples/subcommands/](examples/subcommands/) for examples.

Example subcommands:
- `pr-review` - Multi-agent GitHub PR review: coordinator reads docs and review instructions, spawns per-file agents for changed files, posts suggestions as inline comments

### Available Tools for LLM

The LLM has access to the following tools when executing in the sandbox:

**Project Exploration (read-only /project):**
- `read_project_file` - Read files from the original project
- `list_project_directory` - List directory contents to explore structure

**Git Operations:**
- `checkout_commit` - Create a worktree from any commit/branch
- `git_commit` - Commit changes to a worktree's branch

**File Operations (worktree-only):**
- `read_file` - Read file content from a worktree
- `write_file` - Create or overwrite a file in a worktree
- `edit_file` - Replace multiple line ranges in a file (can edit several locations in one operation)

**Search Tools (worktree-only):**
- `glob` - Find files matching a pattern (e.g., `*.py`, `**/*.js`)
- `grep` - Search file contents using regex (uses ripgrep when available)

**Shell:**
- `execute_command` - Run any shell command in the container

**Tool Usage Pattern:**
1. Use `read_project_file`/`list_project_directory` to explore the original project
2. Use `checkout_commit` to create a worktree when you need to modify files
3. Use worktree file tools (`read_file`, `write_file`, `edit_file`, `glob`, `grep`) for file operations
4. Use `git_commit` to commit changes

**Important:** Worktree file operation and search tools only work within checked-out worktrees. To modify files, you must first create a worktree with `checkout_commit`.

## Examples

See the [examples/](examples/) directory for:
- Custom subcommand implementations
- Workflow patterns
- Integration examples

## Commands

### `llm-sandbox check`
Validate LLM provider configuration and connectivity

### `llm-sandbox gen-containerfile <name>`
Generate a Containerfile using LLM analysis
- `--extra-prompt` - Additional instructions for generation
- `--force` - Overwrite existing configuration

### `llm-sandbox build`
Build the container image
- `--force` - Force rebuild even if up-to-date

### `llm-sandbox run`
Execute one-shot LLM prompt in isolated container
- `--prompt` or `--prompt-file` - Input prompt
- `--schema` or `--schema-file` - JSON schema for structured output
- `--keep-branch` - Branch name to preserve (can be specified multiple times)
- `--network` - Network mode: `isolated` or `enabled`
- `--verbose` - Show detailed tool usage and LLM messages

Note: The LLM can use the `checkout_commit` tool to create worktrees from any commit/branch as needed.

### `llm-sandbox cleanup`
Clean up all temporary worktrees and branches

## Configuration File Format

See [config.example.yaml](config.example.yaml) for a complete configuration example.

```yaml
llm:
  # Auto-detected if not set: uses vertex-ai if CLAUDE_CODE_USE_VERTEX is set
  default_provider: anthropic

  providers:
    anthropic:
      model: claude-sonnet-4-5
      api_key_env: ANTHROPIC_API_KEY

    vertex-ai:
      model: claude-sonnet-4-5
      region: us-east5  # Or set CLOUD_ML_REGION
      project_id: my-gcp-project  # Or set ANTHROPIC_VERTEX_PROJECT_ID

container:
  network: isolated  # or 'enabled'

image:
  # Option 1: Use pre-built image
  image: registry.fedoraproject.org/fedora-toolbox

  # Option 2: Build from Containerfile
  image: my-custom-image
  build:
    containerfile: .llm-sandbox/Containerfile
    auto_rebuild: true
```

## Architecture

```
┌─────────────────────────────────────────┐
│ CLI (llm-sandbox run ...)              │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ Runner (SandboxRunner)                 │
│ - Manages instance lifecycle          │
│ - Coordinates LLM and container       │
└──────────────┬──────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌──────────────┐ ┌──────────────────────┐
│ LLM Provider │ │ Container Manager    │
│ - Claude API │ │ - Podman operations  │
│ - Vertex AI  │ │ - Image building     │
└──────┬───────┘ └──────────┬───────────┘
       │                    │
       ▼                    ▼
┌──────────────────────────────────────────┐
│ MCP Tools                               │
│ Project: read_project_file, list_dir    │
│ Git: checkout_commit, git_commit        │
│ Worktree Files: read, write, edit      │
│ Worktree Search: glob, grep             │
│ Shell: execute_command                  │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ Git Worktrees (on host)                │
│ .llm-sandbox/worktrees/{instance-id}/   │
│   ├── feature/auth/                     │
│   └── bugfix/issue-123/                 │
└─────────────────────────────────────────┘
```

## Development

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/

# Lint
ruff src/
```

## License

MIT
