# LLM Container Sandbox

A system that allows LLMs (Claude) to safely analyze and work with projects in isolated podman containers.

## Features

- **Complete Isolation**: Projects run in isolated podman containers with no access to host resources
- **Git Worktrees**: Work on specific commits without affecting your main repository
- **MCP Tools**: LLM can execute commands and git operations via Model Context Protocol
- **Structured Output**: Get JSON-formatted results matching your schema
- **Branch Management**: Automatically pull branches created by the LLM back to your main repo
- **Network Control**: Choose between isolated (no network) or enabled network access
- **Containerfile Generation**: LLM explores your project and generates appropriate Containerfiles
- **Custom Subcommands**: Create reusable analysis workflows as Python plugins

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer
- Podman (rootless mode recommended)
- Git
- Anthropic API key

## Installation

### Install uv (if not already installed)

```bash
# On macOS and Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with pip
pip install uv
```

### Install llm-sandbox

```bash
# Clone the repository
git clone https://github.com/yourusername/llm-container-sandbox.git
cd llm-container-sandbox

# Install with uv
uv pip install -e .

# Or with dev dependencies
uv pip install -e ".[dev]"
```

### Setup Podman

Ensure podman is installed and configured:

```bash
# Fedora/RHEL
sudo dnf install podman

# Ubuntu/Debian
sudo apt install podman

# macOS
brew install podman
podman machine init
podman machine start

# Verify installation
podman --version
```

## Quick Start

### 1. Install llm-sandbox

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/yourusername/llm-container-sandbox.git
cd llm-container-sandbox
uv pip install -e .
```

### 2. Set API Key

```bash
export ANTHROPIC_API_KEY="your-api-key"
```

### 4. Initialize Your Project

```bash
cd /path/to/your/project
llm-sandbox init
```

During initialization, you'll be prompted to select or generate a Containerfile. You can also specify a custom path interactively.

This will:
- Search for existing Containerfiles or generate one using LLM
- The LLM will explore your project files to understand dependencies
- Save configuration to `.llm-sandbox/`
- Prepare the project for sandboxed execution

### 5. Run Analysis

Run the analysis with inline schema:

```bash
llm-sandbox run \
  --commit HEAD \
  --prompt "Analyze this project and identify potential issues" \
  --schema '{"type": "object", "properties": {"summary": {"type": "string"}, "issues": {"type": "array", "items": {"type": "string"}}}, "required": ["summary", "issues"]}' \
  --pull-branches feature/analysis
```

Or use files for prompt and schema:

```bash
# Create prompt file
cat > prompt.txt << 'EOF'
Analyze this project and identify potential issues.
Focus on security vulnerabilities and code quality.
EOF

# Create schema file
cat > schema.json << 'EOF'
{
  "type": "object",
  "properties": {
    "summary": {"type": "string"},
    "issues": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["summary", "issues"]
}
EOF

# Run analysis
llm-sandbox run \
  --commit HEAD \
  --prompt-file prompt.txt \
  --schema-file schema.json \
  --pull-branches feature/analysis
```

This will:
- Create a worktree from HEAD commit
- Build and run isolated container
- Let Claude analyze the project using MCP tools
- Pull the `feature/analysis` branch (if created) back to your repo
- Output structured JSON result

## Configuration

### Global Config (`$XDG_CONFIG_HOME/llm-sandbox/config.yaml`)

Default location: `~/.config/llm-sandbox/config.yaml`

```yaml
llm:
  default_provider: anthropic  # Provider to use (currently only "anthropic" supported)
  providers:
    anthropic:
      api_key_env: ANTHROPIC_API_KEY  # Environment variable containing API key
      model: claude-sonnet-4-5        # Model to use

container:
  network: isolated  # or enabled
```

**Supported Providers:**
- `anthropic`: Claude API (claude-sonnet-4-5, claude-opus-4-6, etc.)

Additional providers can be added by implementing the `LLMProvider` interface.

### Project Config (`.llm-sandbox/config.yaml`)

Created automatically by `llm-sandbox init`:

```yaml
containerfile: .llm-sandbox/Containerfile  # Path relative to project root
image_tag: llm-sandbox-myproject
```

## How It Works

1. **Worktree Creation**: Creates a git worktree from your specified commit
2. **Container Build**: Builds or uses cached container image
3. **Mounts**:
   - `/project` (read-only): Your original project
   - `/workspace` (read-write): The git worktree
4. **MCP Server**: Provides tools for command execution and git commits
5. **LLM Execution**: Claude analyzes the project using available tools
6. **Branch Pulling**: Branches created in the worktree are pulled to main repo
7. **Cleanup**: Container and worktree are automatically removed

## MCP Tools

The system uses the Model Context Protocol (MCP) to provide tools to the LLM.

### Container MCP Tools (during `run`)

- `execute_command`: Run any shell command in the container
- `git_commit`: Stage and commit files with a message (optionally creating a branch)

### Local MCP Tools (during `init`)

- `read_file`: Read contents of files in the project directory
- `list_directory`: List files and directories in a path

## Subcommands

The CLI uses a subcommand architecture. Both built-in commands (like `run`) and custom commands are implemented as subcommands.

### Built-in Subcommands

- `init` - Initialize project configuration (special case, not a Subcommand)
- `run` - Run one-shot LLM prompt in isolated container

### Custom Subcommands

You can create custom subcommands that leverage the sandbox infrastructure. Subcommands are Python modules that implement a simple interface.

### Common Options for All Subcommands

All subcommands automatically receive these common options:

- `--project-dir PATH`: Project directory (default: current directory)
- `--commit REF`: Git commit/branch/tag to use (default: HEAD)
- `--network {isolated|enabled}`: Network access mode (default: from config)
- `--pull-branches BRANCHES`: Comma-separated list of branches to pull from worktree

These are available in `kwargs` and the `run_sandbox` function is pre-configured with these values.

### Creating a Subcommand

Create a Python file in one of these locations:
- **Global**: `~/.config/llm-sandbox/subcommands/mycommand.py`
- **Project**: `.llm-sandbox/subcommands/mycommand.py`

```python
import click
from llm_sandbox.subcommand import Subcommand

class MySubcommand(Subcommand):
    name = "analyze"
    help = "Analyze the project"

    def add_arguments(self, command):
        """Add custom CLI arguments."""
        command.params.append(
            click.Option(["--depth"], type=int, default=3, help="Analysis depth")
        )
        return command

    def execute(self, project_dir, run_sandbox, **kwargs):
        """
        Execute the subcommand.

        Args:
            project_dir: Path to the project directory
            run_sandbox: Function pre-configured with common options.
                Signature: run_sandbox(prompt: str, output_schema: dict) -> dict
            **kwargs: CLI arguments including:
                - commit: From --commit (already configured in run_sandbox)
                - network: From --network (already configured in run_sandbox)
                - pull_branches: From --pull-branches (already configured in run_sandbox)
                - depth: Custom argument
        """
        depth = kwargs.get("depth", 3)
        commit = kwargs.get("commit")  # Available but already used by run_sandbox

        result = run_sandbox(
            prompt=f"Analyze this project with depth {depth}",
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "issues": {"type": "array", "items": {"type": "string"}}
                }
            }
        )

        print(f"Found {len(result['issues'])} issues")
```

### Using Custom Subcommands

Once created, the subcommand is automatically available with both common and custom options:

```bash
# Use common options
llm-sandbox analyze --commit develop --depth 5

# With network and branch pulling
llm-sandbox analyze --commit main --network enabled --pull-branches feature/fixes --depth 3
```

### Example Subcommand

See `example_subcommand.py` for a complete example of a code analysis subcommand with:
- Custom arguments
- Structured output schema
- Results formatting and display
- File output option

## CLI Reference

### `llm-sandbox init`

Initialize project configuration.

Options:
- `--project-dir PATH`: Project directory (default: current directory)

The init process will:
1. Search for existing Containerfiles
2. Present options:
   - **Option 1**: Generate new Containerfile with LLM
   - **Option 2**: Specify custom path (existing or new location)
   - **Options 3+**: Use found Containerfiles

### `llm-sandbox run`

Run one-shot LLM prompt in isolated container.

**Common Options** (available to all subcommands):
- `--project-dir PATH`: Project directory (default: current directory)
- `--commit REF`: Git commit/branch/tag to use (default: HEAD)
- `--network {isolated|enabled}`: Network access mode (default: from config)
- `--pull-branches BRANCHES`: Comma-separated list of branches to pull

**Run-specific Options**:
- `--prompt TEXT`: Prompt text (use --prompt-file for file input)
- `--prompt-file FILE`: File containing the prompt
- `--schema JSON`: JSON schema string for structured output
- `--schema-file FILE`: JSON schema file for structured output

Note: Either `--prompt` or `--prompt-file` must be provided (but not both).
Either `--schema` or `--schema-file` must be provided (but not both).

## Example Workflows

### Security Audit

```bash
# Schema
cat > audit-schema.json << 'EOF'
{
  "type": "object",
  "properties": {
    "vulnerabilities": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "file": {"type": "string"},
          "line": {"type": "number"},
          "severity": {"type": "string"},
          "description": {"type": "string"}
        }
      }
    }
  }
}
EOF

# Create prompt
echo "Perform a security audit of this codebase" > audit-prompt.txt

# Run audit
llm-sandbox run \
  --commit main \
  --prompt-file audit-prompt.txt \
  --schema-file audit-schema.json
```

### Refactoring

```bash
echo "Refactor the authentication module to use async/await" > refactor-prompt.txt

llm-sandbox run \
  --commit develop \
  --prompt-file refactor-prompt.txt \
  --schema-file refactor-schema.json \
  --pull-branches feature/async-auth
```

### Documentation Generation

```bash
llm-sandbox run \
  --commit HEAD \
  --prompt "Generate API documentation from source code" \
  --schema-file docs-schema.json \
  --pull-branches docs/api-reference
```

## Security

- **Container Isolation**: Podman rootless mode provides strong isolation
- **Network**: Default isolated mode prevents network access
- **File Access**: Only mounted directories are accessible
- **No Privilege Escalation**: Containers run without elevated privileges
- **API Keys**: Loaded from environment, never stored in files

## Development

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src tests

# Lint
ruff check src tests
```

## Extending LLM Sandbox

### Adding a New LLM Provider

To add support for a new LLM provider (e.g., OpenAI, Google):

1. **Create a provider class** in `src/llm_sandbox/llm_provider.py`:

```python
class OpenAIProvider(LLMProvider):
    """OpenAI API provider."""

    def __init__(self, provider_config):
        import os

        # Get API key from environment
        api_key = os.getenv(provider_config.api_key_env)
        if not api_key:
            raise ValueError(f"API key not found. Set {provider_config.api_key_env}")

        self.api_key = api_key
        self.model = provider_config.model
        self.provider_config = provider_config
        # Initialize your client here

    def generate_text(self, prompt: str, max_tokens: int = 2000) -> str:
        # Implement simple text generation
        pass

    def generate_structured(
        self,
        prompt: str,
        mcp_server: MCPServer,
        output_schema: Dict[str, Any],
        max_iterations: int = 25,
    ) -> Dict[str, Any]:
        # Implement the structured generation logic
        # with MCP tool support
        pass
```

2. **Update the factory function**:

```python
def create_llm_provider(provider_name: str, provider_config) -> LLMProvider:
    if provider_name == "anthropic":
        return ClaudeProvider(provider_config)
    elif provider_name == "openai":
        return OpenAIProvider(provider_config)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider_name}")
```

3. **Add to configuration**:

```yaml
llm:
  default_provider: openai
  providers:
    openai:
      api_key_env: OPENAI_API_KEY
      model: gpt-4
```

## Architecture

See the detailed architecture and implementation plan in the project documentation.

## License

MIT

## Contributing

Contributions welcome! Please open an issue or pull request.
