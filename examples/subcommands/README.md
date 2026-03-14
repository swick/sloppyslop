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
1. Fetches PR information from GitHub API
2. Pre-checks out PR head and base commits into worktrees (pr-head and pr-base)
3. Agent reads project documentation (AGENTS.md, CLAUDE.md)
4. Agent identifies commits and changes in the PR using git history or GitHub API
5. Agent finds and reads ALL review instruction files from `review/` and `docs/review/` folders
6. Agent applies all review criteria to ALL PR changes
7. Agent generates suggestions based on all criteria
8. User approves suggestions interactively
9. Posts accepted suggestions as **inline review comments** directly on the relevant lines

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
1. **Pre-setup (Python code):**
   - Fetches PR information from GitHub API
   - Pre-checks out worktrees for PR head and base branches (pr-head and pr-base)

2. **Review agent performs all tasks:**
   - Uses pre-checked-out worktrees (pr-head and pr-base)
   - Reads AGENTS.md and CLAUDE.md for project context
   - Uses git history (git rev-list, git show) or GitHub API to identify all commits and changes in the PR
   - Finds all review instruction files from `review/` and `docs/review/` folders
   - Reads ALL review instruction files to understand all criteria to apply
   - Examines all changes in the PR using git commands and file tools
   - Applies all review criteria from all instruction files
   - Generates suggestions following project documentation and review guidelines
   - Categories: bug/performance/security/style/refactor/documentation

3. **User approval:**
   - All suggestions are shown to the user
   - User reviews and accepts/rejects each suggestion interactively

4. **GitHub posting:**
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

    def execute(self, project_dir: Path, runner, **kwargs):
        """Execute your workflow."""
        my_arg = kwargs["my_arg"]
        network = kwargs["network"]
        verbose = kwargs["verbose"]

        # Define your prompt and schema
        prompt = f"Do something with {my_arg}"
        schema = {
            "type": "object",
            "properties": {
                "result": {"type": "string"}
            }
        }

        # Setup and run the sandbox
        try:
            runner.setup(
                keep_branches=["my-branch"],  # optional, defaults to []
                network=network,
            )

            result = runner.run_prompt(
                prompt=prompt,
                output_schema=schema,
                verbose=verbose,
                custom_tools=None,  # optional, defaults to None
            )

            # Process the result
            click.echo(f"Result: {result['result']}")
        finally:
            # Cleanup is called automatically, but you can call it explicitly
            runner.cleanup()
```

3. Available in `**kwargs`:
   - `network`: Network mode from `--network`
   - `verbose`: Boolean from `--verbose`
   - Any custom arguments you added in `add_arguments()`

4. The `runner` parameter (SandboxRunner instance):
   - **`runner.setup(keep_branches: list = None, network: str = None)`**: Setup the sandbox environment
     - Creates worktrees directory
     - Builds/gets container image
     - Starts the container
   - **`runner.run_prompt(prompt: str, output_schema: dict, verbose: bool = False, custom_tools: list = None) -> dict`**: Execute LLM prompt
     - Must call `setup()` first
     - Returns structured output from LLM
     - Can provide custom MCP tools to extend available tools
   - **`runner.cleanup()`**: Cleanup container and worktrees
     - Called automatically by destructor
     - Safe to call multiple times
     - Best practice: call in `finally` block

## Creating Custom MCP Tools

Subcommands can define custom MCP tools to extend the LLM's capabilities. Custom tools allow you to:
- Add domain-specific operations (e.g., GitHub API calls)
- Provide specialized data access
- Implement custom validation or processing logic

### Example: Custom Tool for GitHub API

```python
from llm_sandbox.mcp_tools import MCPTool
from typing import Any, Dict

class GetPullRequestInfoTool(MCPTool):
    """Tool for fetching GitHub PR information."""

    def __init__(self, github_token: str, repo: str):
        """Initialize the tool with GitHub credentials."""
        super().__init__(
            name="get_pull_request_info",
            description="Fetch detailed information about a GitHub pull request",
            parameters={
                "type": "object",
                "properties": {
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number",
                    },
                },
                "required": ["pr_number"],
            },
        )
        self.github_token = github_token
        self.repo = repo

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the tool - fetch PR info from GitHub API."""
        pr_number = arguments["pr_number"]

        try:
            # Make GitHub API call
            import requests
            url = f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}"
            headers = {
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github+json",
            }
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            return {
                "success": True,
                "title": data["title"],
                "state": data["state"],
                "author": data["user"]["login"],
                "head_sha": data["head"]["sha"],
                "base_sha": data["base"]["sha"],
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }
```

### Using Custom Tools in Subcommands

```python
from llm_sandbox.mcp_tools import MCPTool

class MySubcommand(Subcommand):
    def execute(self, project_dir: Path, runner, **kwargs):
        # Create custom tool instances
        custom_tool = GetPullRequestInfoTool(
            github_token=kwargs["token"],
            repo="owner/repo",
        )

        # Setup, run with custom tools, and cleanup
        try:
            runner.setup(network=kwargs["network"])
            result = runner.run_prompt(
                prompt="Fetch PR #123 and analyze it",
                output_schema={"type": "object", "properties": {...}},
                verbose=kwargs["verbose"],
                custom_tools=[custom_tool],  # List of custom tools
            )
        finally:
            runner.cleanup()
```

### Custom Tool Guidelines

- **Inherit from `MCPTool`**: All custom tools must extend the `MCPTool` base class
- **Implement `execute()`**: This method receives the arguments and returns a result dictionary
- **Return format**: Always return a dict with at least `{"success": bool}`. On error, include `"error": str`
- **Parameters schema**: Use JSON Schema to define the tool's input parameters
- **Stateless when possible**: Tools should generally be stateless or accept state in constructor
- **Error handling**: Catch exceptions and return error messages instead of raising

## Tips

- Use descriptive schema definitions to guide the LLM's output
- Break complex workflows into multiple LLM calls
- Use `click.echo()` for user feedback
- Use `click.confirm()` for user approval steps
- Check for tool prerequisites (like `gh` CLI) early
- Provide clear next-step instructions to users
- Use custom MCP tools for operations that don't fit standard file/git/command tools
