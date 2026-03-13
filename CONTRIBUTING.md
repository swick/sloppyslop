# Contributing to LLM Container Sandbox

Thank you for your interest in contributing to LLM Container Sandbox!

## Development Setup

### Prerequisites

- Python 3.10 or higher
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer
- Podman
- Git

### Getting Started

1. **Fork and clone the repository**

```bash
git clone https://github.com/yourusername/llm-container-sandbox.git
cd llm-container-sandbox
```

2. **Install uv** (if not already installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

3. **Install dependencies with uv**

```bash
# Install in editable mode with dev dependencies
uv pip install -e ".[dev]"
```

4. **Set up your API key**

```bash
export ANTHROPIC_API_KEY="your-api-key"
```

## Development Workflow

### Running Tests

```bash
pytest
```

### Code Formatting

We use `black` for code formatting:

```bash
black src tests
```

### Linting

We use `ruff` for linting:

```bash
ruff check src tests
```

### Type Checking

While not currently enforced, consider running type checks:

```bash
mypy src
```

## Making Changes

1. Create a new branch for your feature or fix:
   ```bash
   git checkout -b feature/my-new-feature
   ```

2. Make your changes and ensure:
   - Code is formatted with `black`
   - Linting passes with `ruff`
   - Tests pass with `pytest`
   - New functionality includes tests

3. Commit your changes:
   ```bash
   git add .
   git commit -m "Add my new feature"
   ```

4. Push to your fork:
   ```bash
   git push origin feature/my-new-feature
   ```

5. Create a Pull Request

## Testing Custom Subcommands

To test custom subcommands during development:

1. Create a test project directory
2. Initialize it with `llm-sandbox init`
3. Create subcommands in `.llm-sandbox/subcommands/` or `~/.config/llm-sandbox/subcommands/`
4. Test with the CLI

## Project Structure

```
llm-container-sandbox/
├── src/llm_sandbox/
│   ├── __init__.py
│   ├── __main__.py          # CLI entry point
│   ├── analyzer.py          # Containerfile analysis
│   ├── builtin_subcommands.py  # Built-in commands
│   ├── config.py            # Configuration management
│   ├── container.py         # Podman integration
│   ├── git_ops.py           # Git operations
│   ├── llm_provider.py      # Claude API integration
│   ├── mcp_tools.py         # MCP base classes (MCPServer, MCPTool)
│   ├── mcp_server.py        # Container MCP server implementation
│   ├── local_tools.py       # Local MCP server implementation
│   ├── runner.py            # Workflow orchestration
│   ├── subcommand.py        # Subcommand base class
│   └── worktree.py          # Git worktree management
├── tests/
│   ├── test_config.py
│   └── test_analyzer.py
├── pyproject.toml
└── README.md
```

## Coding Standards

- Follow PEP 8 style guidelines (enforced by `black` and `ruff`)
- Write descriptive docstrings for all public functions and classes
- Keep functions focused and single-purpose
- Add type hints where appropriate
- Write tests for new functionality

## Adding New Features

### Adding a Built-in Subcommand

1. Create a new class in `src/llm_sandbox/builtin_subcommands.py`
2. Inherit from `Subcommand`
3. Implement `add_arguments()` and `execute()`
4. Register it in `__main__.py` with `register_builtin_subcommands()`

### Adding Configuration Options

1. Update the appropriate model in `src/llm_sandbox/config.py`
2. Update documentation in README.md
3. Add tests for the new configuration

### Adding a New LLM Provider

To add support for a new LLM provider:

1. Create a new class inheriting from `LLMProvider` in `src/llm_sandbox/llm_provider.py`
2. Implement the `generate_structured()` method
3. Update the `create_llm_provider()` factory function
4. Add provider configuration to the config schema
5. Update documentation and tests

Example:
```python
class MyProvider(LLMProvider):
    def __init__(self, provider_config):
        import os

        # Get API key from environment
        api_key = os.getenv(provider_config.api_key_env)
        if not api_key:
            raise ValueError(f"API key not found")

        self.api_key = api_key
        self.model = provider_config.model
        self.provider_config = provider_config
        # Initialize your provider client

    def generate_text(self, prompt, max_tokens=2000):
        # Implement text generation
        pass

    def generate_structured(self, prompt, mcp_server, output_schema, max_iterations=25):
        # Implement structured generation with MCP tool support
        pass
```

## Questions?

Feel free to open an issue for discussion or questions about contributing.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
