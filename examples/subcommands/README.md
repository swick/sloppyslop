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

A single-agent GitHub PR review workflow that:
1. Checks out PR head and base commits
2. Reads project documentation (AGENTS.md, CLAUDE.md)
3. Identifies commits and changes in the PR using git history
4. Finds and reads ALL review instruction files from `review/` and `docs/review/` folders
5. Applies all review criteria to ALL PR changes
6. Generates suggestions based on all criteria
7. User approves suggestions interactively
8. Posts accepted suggestions as **inline review comments** directly on the relevant lines

**Important:**
- The `review/` and `docs/review/` folders contain **review instructions** (how to review), not the files to review
- The actual files reviewed are those that changed between pr-base and pr-head
- If `review/` folders don't exist, the tool still performs a comprehensive review using general best practices

**Prerequisites:**
- GitHub token (set `GH_TOKEN` environment variable or use `--with-token`)
- Repository must be a GitHub repository

**Optional:**
- AGENTS.md and CLAUDE.md for project context
- Review instruction files in `review/` or `docs/review/` folders for custom review guidelines

**Usage:**
```bash
# Basic usage
llm-sandbox pr-review --pr 123

# With custom token
llm-sandbox pr-review --pr 123 --with-token ghp_xxxxx
```

**Single-Agent Workflow:**
1. **Review agent performs all tasks:**
   - Checks out worktrees for PR head and base branches
   - Reads AGENTS.md and CLAUDE.md for project context
   - Uses git history (git rev-list, git show) to identify all commits and changes in the PR
   - Finds all review instruction files from `review/` and `docs/review/` folders
   - Reads ALL review instruction files to understand all criteria to apply
   - Examines all changes in the PR using git commands and file tools
   - Applies all review criteria from all instruction files
   - Generates suggestions following project documentation and review guidelines
   - Categories: bug/performance/security/style/refactor/documentation

2. **User approval:**
   - All suggestions are shown to the user
   - User reviews and accepts/rejects each suggestion interactively

3. **GitHub posting:**
   - Accepted suggestions posted as inline comments with GitHub's suggestion feature
   - Summary comment includes documentation summary, review criteria applied, and stats

**Review comment features:**
- Inline comments appear directly on the relevant code lines in the "Files changed" tab
- GitHub's suggestion blocks allow maintainers to apply changes with one click
- Summary comment includes project documentation summary
- Each suggestion includes category emoji (🐛 bug, ⚡ performance, 🔒 security, 📝 documentation, etc.)

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
