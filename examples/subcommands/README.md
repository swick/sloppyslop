# Example Subcommands

This directory contains example custom subcommands that demonstrate how to extend `llm-sandbox` with your own workflows.

## Using Example Subcommands

To use an example subcommand:

1. Copy the subcommand file to your project or global config directory:

   **Project-level** (available only in this project):
   ```bash
   mkdir -p .llm-sandbox/subcommands
   cp examples/subcommands/pr-review.py .llm-sandbox/subcommands/
   ```

   **Global** (available in all projects):
   ```bash
   mkdir -p ~/.config/llm-sandbox/subcommands
   cp examples/subcommands/pr-review.py ~/.config/llm-sandbox/subcommands/
   ```

2. The subcommand will be automatically discovered and available as:
   ```bash
   llm-sandbox pr-review --help
   ```

## Available Examples

### pr-review.py

An interactive GitHub PR review workflow that:
1. Fetches a PR from GitHub using `gh` CLI
2. Uses the LLM to analyze the PR and suggest improvements
3. Shows each suggestion to the user for approval
4. Posts accepted suggestions as **inline review comments** directly on the relevant lines

**Prerequisites:**
- GitHub CLI (`gh`) installed and authenticated
- Repository must be a GitHub repository

**Usage:**
```bash
# Basic usage
llm-sandbox pr-review --pr 123

# Limit number of suggestions
llm-sandbox pr-review --pr 123 --max-suggestions 5
```

**Workflow:**
1. Fetches PR information (title, branch, author, head commit SHA)
2. LLM reviews the PR and generates up to 10 suggestions (configurable)
3. Each suggestion is shown with:
   - File and line range
   - Category (bug/performance/security/style/refactor)
   - Current code vs. suggested code
   - Reasoning
4. User accepts or rejects each suggestion interactively
5. Accepted suggestions are posted as **inline comments** on specific lines using GitHub API
6. Uses GitHub's `suggestion` feature for one-click code application

**Review comment features:**
- Inline comments appear directly on the relevant code lines in the "Files changed" tab
- GitHub's suggestion blocks allow maintainers to apply changes with one click
- Summary comment in the main conversation lists all suggestions
- Each suggestion includes category emoji (🐛 bug, ⚡ performance, 🔒 security, etc.)

## Creating Your Own Subcommands

To create a custom subcommand:

1. Create a Python file in `.llm-sandbox/subcommands/` or `~/.config/llm-sandbox/subcommands/`

2. Define a class that inherits from `Subcommand`:

```python
from pathlib import Path
import click
from llm_sandbox.subcommand import Subcommand

class MySubcommand(Subcommand):
    name = "my-command"
    help = "Brief description"

    def add_arguments(self, command):
        """Add custom CLI arguments."""
        command.params.append(
            click.Option(
                ["--my-arg"],
                type=str,
                required=True,
                help="Custom argument",
            )
        )
        return command

    def execute(self, project_dir: Path, run_sandbox, **kwargs):
        """Execute your workflow."""
        my_arg = kwargs["my_arg"]

        # Define your prompt and schema
        prompt = f"Do something with {my_arg}"
        schema = {
            "type": "object",
            "properties": {
                "result": {"type": "string"}
            }
        }

        # Run the sandbox (network and verbose pre-configured from CLI)
        # Optionally specify keep_branches
        result = run_sandbox(
            prompt=prompt,
            output_schema=schema,
            keep_branches=["my-branch"],  # optional, defaults to []
        )

        # Process the result
        click.echo(f"Result: {result['result']}")
```

3. Available in `**kwargs`:
   - `network`: Network mode from `--network` (pre-configured in `run_sandbox`)
   - `verbose`: Boolean from `--verbose` (pre-configured in `run_sandbox`)
   - Any custom arguments you added in `add_arguments()`

4. The `run_sandbox` function:
   - Pre-configured with `network` and `verbose` from CLI options
   - Signature: `run_sandbox(prompt: str, output_schema: dict, keep_branches: list = None) -> dict`
   - Subcommands can specify `keep_branches` as needed
   - The LLM can use `checkout_commit` tool to work with any commit/branch
   - Returns the structured output from the LLM

## Tips

- Use descriptive schema definitions to guide the LLM's output
- Break complex workflows into multiple LLM calls
- Use `click.echo()` for user feedback
- Use `click.confirm()` for user approval steps
- Check for tool prerequisites (like `gh` CLI) early
- Provide clear next-step instructions to users
