"""Example subcommand: GitHub PR review with instruction-based criteria.

This subcommand demonstrates a single-agent workflow with custom MCP tools:
1. Fetches PR information from GitHub API
2. Fetches PR head from GitHub into branch (fetch/pr-{id}/{head-branch} pattern)
3. Pre-checks out PR head and base commits into worktrees (pr-head and pr-base)
4. Agent reads project documentation (AGENTS.md, CLAUDE.md) if available
5. Agent uses custom GitHub API tools (get_pull_request_commits, get_pull_request_diff) to fetch PR data
6. OR agent uses git history (git rev-list --ancestry-path) to identify commits in the PR
7. Agent examines each commit (git show) to understand all changes
8. Agent finds review instruction files in review/ and docs/review/ folders
9. Agent reads ALL review instruction files and applies criteria to ALL PR changes
10. Agent generates suggestions based on all criteria
11. User approves suggestions and posts to GitHub

Custom MCP Tools:
- get_pull_request_commits: Fetches commit list from GitHub API
- get_pull_request_diff: Fetches full PR diff from GitHub API

The review/ and docs/review/ folders contain INSTRUCTIONS on how to review code,
not the code to review. The actual code to review is identified through git history
between pr-base and pr-head commits.

If no review instruction files exist, uses general best practices review criteria.

Usage:
    llm-sandbox pr-review --pr 123
    llm-sandbox pr-review --pr 123 --with-token ghp_xxxxx

Authentication:
    Set GH_TOKEN environment variable or use --with-token option
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import click
import requests

from llm_sandbox.mcp_tools import (
    MCPTool,
    MCPServer,
    ExecuteCommandTool,
    CheckoutCommitTool,
    GitCommitTool,
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadProjectFileTool,
    ListProjectDirectoryTool,
)
from llm_sandbox.subcommand import Subcommand


class GetPullRequestDiffTool(MCPTool):
    """Custom MCP tool for fetching GitHub PR diff."""

    def __init__(self, github_client: "GitHubClient", repo: str, pr_number: int):
        """
        Initialize the PR diff tool.

        Args:
            github_client: GitHubClient instance
            repo: Repository in owner/name format
            pr_number: Pull request number
        """
        super().__init__(
            name="get_pull_request_diff",
            description="Get the full diff of the pull request from GitHub API. Returns the unified diff format showing all changes.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        self.github_client = github_client
        self.repo = repo
        self.pr_number = pr_number

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch the PR diff from GitHub API."""
        try:
            diff = self.github_client.get_pull_request_diff(self.repo, self.pr_number)
            return {
                "success": True,
                "diff": diff,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch PR diff: {str(e)}",
            }


class GetPullRequestCommitsTool(MCPTool):
    """Custom MCP tool for fetching GitHub PR commits."""

    def __init__(self, github_client: "GitHubClient", repo: str, pr_number: int):
        """
        Initialize the PR commits tool.

        Args:
            github_client: GitHubClient instance
            repo: Repository in owner/name format
            pr_number: Pull request number
        """
        super().__init__(
            name="get_pull_request_commits",
            description="Get the list of commits in the pull request from GitHub API. Returns commit SHAs, messages, authors, and timestamps.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        self.github_client = github_client
        self.repo = repo
        self.pr_number = pr_number

    def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch the PR commits from GitHub API."""
        try:
            commits = self.github_client.get_pull_request_commits(self.repo, self.pr_number)
            return {
                "success": True,
                "commits": commits,
                "count": len(commits),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch PR commits: {str(e)}",
            }


class GitHubClient:
    """GitHub API client."""

    def __init__(self, token: str):
        """Initialize GitHub client with token."""
        self.token = token
        self.base_url = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        """Get PR information."""
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()

        return {
            "title": data["title"],
            "head_ref": data["head"]["ref"],
            "base_ref": data["base"]["ref"],
            "author": data["user"]["login"],
            "head_sha": data["head"]["sha"],
            "base_sha": data["base"]["sha"],
        }

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        """Post a comment on an issue/PR."""
        url = f"{self.base_url}/repos/{repo}/issues/{issue_number}/comments"
        response = requests.post(
            url,
            headers=self.headers,
            json={"body": body},
        )
        response.raise_for_status()

    def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        path: str,
        line_start: int,
        line_end: int,
        body: str,
    ) -> None:
        """Post an inline review comment on specific lines."""
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/comments"

        comment_data = {
            "body": body,
            "commit_id": commit_sha,
            "path": path,
            "side": "RIGHT",
        }

        # Single line vs multi-line
        if line_start == line_end:
            comment_data["line"] = line_end
        else:
            comment_data["start_line"] = line_start
            comment_data["start_side"] = "RIGHT"
            comment_data["line"] = line_end

        response = requests.post(url, headers=self.headers, json=comment_data)
        response.raise_for_status()

    def get_pull_request_diff(self, repo: str, pr_number: int) -> str:
        """
        Get the full diff of a pull request.

        Args:
            repo: Repository in owner/name format
            pr_number: Pull request number

        Returns:
            Unified diff format string
        """
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}"
        headers = self.headers.copy()
        headers["Accept"] = "application/vnd.github.v3.diff"

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.text

    def get_pull_request_commits(self, repo: str, pr_number: int) -> list:
        """
        Get the list of commits in a pull request.

        Args:
            repo: Repository in owner/name format
            pr_number: Pull request number

        Returns:
            List of commit dictionaries with metadata
        """
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}/commits"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()

        commits_data = response.json()
        commits = []

        for commit in commits_data:
            commits.append({
                "sha": commit["sha"],
                "short_sha": commit["sha"][:7],
                "message": commit["commit"]["message"],
                "author": commit["commit"]["author"]["name"],
                "author_email": commit["commit"]["author"]["email"],
                "date": commit["commit"]["author"]["date"],
                "committer": commit["commit"]["committer"]["name"],
            })

        return commits


class PRReviewMCPServer(MCPServer):
    """MCP Server for PR review with all built-in tools plus GitHub API tools."""

    def __init__(self, runner, github_client: "GitHubClient", repo: str, pr_number: int):
        """
        Initialize PR review MCP server.

        Args:
            runner: SandboxRunner instance
            github_client: GitHubClient instance
            repo: Repository in owner/name format
            pr_number: Pull request number
        """
        super().__init__()
        # Add all built-in tools
        self.add_tool(ExecuteCommandTool(runner))
        self.add_tool(CheckoutCommitTool(runner))
        self.add_tool(GitCommitTool(runner))
        self.add_tool(ReadFileTool(runner))
        self.add_tool(WriteFileTool(runner))
        self.add_tool(EditFileTool(runner))
        self.add_tool(GlobTool(runner))
        self.add_tool(GrepTool(runner))
        self.add_tool(ReadProjectFileTool(runner))
        self.add_tool(ListProjectDirectoryTool(runner))
        # Add custom GitHub API tools
        self.add_tool(GetPullRequestDiffTool(github_client, repo, pr_number))
        self.add_tool(GetPullRequestCommitsTool(github_client, repo, pr_number))


class PRReviewSubcommand(Subcommand):
    """GitHub PR review with instruction-based criteria using a single agent."""

    name = "pr-review"
    help = "PR review: single agent reads docs, finds changes, applies review instructions"

    def add_arguments(self, command):
        """Add custom arguments."""
        command.params.append(
            click.Option(
                ["--pr"],
                type=int,
                required=True,
                help="GitHub PR number to review",
            )
        )
        command.params.append(
            click.Option(
                ["--with-token"],
                type=str,
                help="GitHub token (defaults to GH_TOKEN environment variable)",
            )
        )
        return command

    def execute(self, project_dir: Path, runner, **kwargs):
        """Execute multi-agent PR review workflow."""
        pr_number = kwargs["pr"]
        token = kwargs.get("with_token") or os.getenv("GH_TOKEN")
        network = kwargs["network"]
        verbose = kwargs["verbose"]

        if not token:
            click.echo(
                "Error: GitHub token not found.\n"
                "\n"
                "Set GH_TOKEN environment variable or use --with-token option:\n"
                "  export GH_TOKEN=ghp_xxxxxxxxxxxx\n"
                "  or\n"
                "  llm-sandbox pr-review --pr 123 --with-token ghp_xxxxxxxxxxxx",
                err=True
            )
            sys.exit(1)

        # Initialize GitHub API client
        self.github = GitHubClient(token)

        click.echo(f"\n{'='*60}")
        click.echo(f"Multi-Agent GitHub PR Review: #{pr_number}")
        click.echo(f"{'='*60}\n")

        # Step 1: Get repository info
        click.echo("Fetching repository information...")
        try:
            repo_name = self._get_repo_name(project_dir)
            click.echo(f"  Repository: {repo_name}")
        except Exception as e:
            click.echo(f"Error getting repository: {e}", err=True)
            sys.exit(1)

        # Step 2: Fetch PR info using GitHub API
        click.echo("Fetching PR information...")
        try:
            pr_info = self.github.get_pull_request(repo_name, pr_number)
            click.echo(f"  PR Title: {pr_info['title']}")
            click.echo(f"  Branch: {pr_info['head_ref']} ({pr_info['head_sha'][:7]})")
            click.echo(f"  Base: {pr_info['base_ref']} ({pr_info['base_sha'][:7]})")
            click.echo(f"  Author: {pr_info['author']}")
        except Exception as e:
            click.echo(f"Error fetching PR info: {e}", err=True)
            sys.exit(1)

        # Step 3: Fetch PR head from GitHub into local branch
        click.echo("\nFetching PR commits...")

        # Create branch name with fetch/pr-{id}/{branch} pattern
        pr_head_branch = f"fetch/pr-{pr_number}/{pr_info['head_ref']}"

        try:
            # Fetch the PR head (this will also fetch the necessary base commits)
            # GitHub exposes PRs at refs/pull/<number>/head
            subprocess.run(
                ["git", "fetch", "origin", f"pull/{pr_number}/head:{pr_head_branch}"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            click.echo(f"  Fetched PR head: {pr_head_branch}")
        except subprocess.CalledProcessError as e:
            click.echo(f"Error fetching PR head: {e.stderr}", err=True)
            sys.exit(1)

        # Step 4: Single agent does everything
        click.echo("\nStarting review agent...")

        agent_prompt = f"""You are a code review agent for PR #{pr_number}.

PR Information:
- Title: {pr_info['title']}
- Head branch: {pr_info['head_ref']}
- Base branch: {pr_info['base_ref']}
- Author: {pr_info['author']}

Worktrees already checked out for you:
- 'pr-head': Contains the PR changes (head: {pr_info['head_ref']})
- 'pr-base': Contains the base branch (base: {pr_info['base_ref']})

Your tasks:

1. Read project documentation from pr-head worktree (if available):
   - Try to read AGENTS.md (using read_file)
   - Try to read CLAUDE.md (using read_file)
   - If these don't exist, that's fine - note that in your summary

2. Identify commits and changes in the PR:
   - You can use get_pull_request_commits() to get the list of commits from GitHub API
   - You can use get_pull_request_diff() to get the full PR diff from GitHub API
   - For each commit, use git show to examine what changed (files, diffs, commit messages)
   - Build a summary of all changes across all commits in the PR

3. Find and read review instruction files from pr-head worktree:
   - Use glob to find files in: review/ directory
   - Use glob to find files in: docs/review/ directory
   - Read EACH instruction file using read_file(worktree="pr-head", path="...")
   - Understand all the review criteria from all instruction files
   - If no review instruction files exist, use general best practices

4. Review ALL the changes in the PR according to ALL the criteria:
   - Apply all criteria from all instruction files
   - Use git commands or file tools to examine the changes
   - Use read_file to examine specific files from pr-head and pr-base
   - Focus on the changes between the two worktrees
   - Generate suggestions for ANY files that violate any criteria

Review criteria to apply:
- All specific instructions from review instruction files (if any exist)
- Code quality and best practices
- Potential bugs or edge cases
- Performance improvements
- Security concerns
- Readability and maintainability
- Consistency with existing code patterns
- Alignment with project documentation

For each suggestion, provide:
- The file path (relative to repository root)
- Line numbers in the pr-head version
- Current code and suggested replacement
- Clear reasoning based on the review criteria and which instruction file it relates to (if applicable)

Return:
- documentation_summary: Summary of AGENTS.md and CLAUDE.md (or "No project documentation found")
- commits_summary: Summary of commits and changes found
- review_instruction_files: List of review instruction file paths found (empty if none)
- review_criteria_applied: Summary of all review criteria from all instruction files
- suggestions: All review suggestions across all files"""

        agent_schema = {
            "type": "object",
            "properties": {
                "documentation_summary": {
                    "type": "string",
                    "description": "Summary of project documentation (AGENTS.md, CLAUDE.md)",
                },
                "commits_summary": {
                    "type": "string",
                    "description": "Summary of commits and changes found between pr-base and pr-head",
                },
                "review_instruction_files": {
                    "type": "array",
                    "description": "List of review instruction file paths from review/ and docs/review/ (empty if none)",
                    "items": {"type": "string"},
                },
                "review_criteria_applied": {
                    "type": "string",
                    "description": "Summary of all review criteria applied from all instruction files",
                },
                "suggestions": {
                    "type": "array",
                    "description": "All review suggestions across all files",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "File path relative to repository root",
                            },
                            "line_start": {
                                "type": "integer",
                                "description": "Starting line number in pr-head",
                            },
                            "line_end": {
                                "type": "integer",
                                "description": "Ending line number in pr-head",
                            },
                            "current_code": {
                                "type": "string",
                                "description": "Current code snippet",
                            },
                            "suggested_code": {
                                "type": "string",
                                "description": "Suggested improved code",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Explanation based on review criteria",
                            },
                            "category": {
                                "type": "string",
                                "enum": ["bug", "performance", "security", "style", "refactor", "documentation"],
                                "description": "Category of the suggestion",
                            },
                        },
                        "required": ["file", "line_start", "line_end", "current_code", "suggested_code", "reason", "category"],
                    },
                },
            },
            "required": ["documentation_summary", "commits_summary", "review_instruction_files", "review_criteria_applied", "suggestions"],
        }

        # Run single agent with custom tools
        try:
            # Setup the sandbox environment
            runner.setup(network=network)

            # Create checkout tool instance
            checkout_tool = CheckoutCommitTool(runner)

            # Pre-checkout worktrees for PR head and base
            click.echo("\nChecking out worktrees...")
            click.echo(f"  Creating worktree 'pr-head' from {pr_head_branch}...")
            head_result = checkout_tool.execute({
                "commit": pr_head_branch,
                "worktree_name": "pr-head",
            })
            if not head_result["success"]:
                click.echo(f"Error: {head_result['error']}", err=True)
                sys.exit(1)

            # Use the base ref directly (like "main" or "master")
            # The base commit is already available from the PR head fetch
            click.echo(f"  Creating worktree 'pr-base' from {pr_info['base_ref']}...")
            base_result = checkout_tool.execute({
                "commit": pr_info['base_ref'],
                "worktree_name": "pr-base",
            })
            if not base_result["success"]:
                click.echo(f"Error: {base_result['error']}", err=True)
                sys.exit(1)

            click.echo("  Worktrees created successfully!")

            # Create MCP server with all built-in tools + GitHub API tools
            mcp_server = PRReviewMCPServer(runner, self.github, repo_name, pr_number)

            result = runner.run_agent(
                prompt=agent_prompt,
                output_schema=agent_schema,
                mcp_server=mcp_server,
                verbose=verbose,
            )
        finally:
            runner.cleanup()

        # Show results
        click.echo(f"\n{'='*60}")
        click.echo("Review Agent Results")
        click.echo(f"{'='*60}")
        click.echo(f"\nDocumentation Summary:")
        click.echo(result["documentation_summary"])
        click.echo(f"\nCommits Summary:")
        click.echo(result["commits_summary"])
        click.echo(f"\nReview Instruction Files: {len(result['review_instruction_files'])}")
        if result["review_instruction_files"]:
            for file in result["review_instruction_files"]:
                click.echo(f"  - {file}")
        else:
            click.echo("  (No specific review instruction files found - using general best practices)")
        click.echo(f"\nCriteria Applied:")
        click.echo(result['review_criteria_applied'])
        click.echo(f"\nFound {len(result['suggestions'])} suggestions")

        all_suggestions = result["suggestions"]

        # Aggregate results
        click.echo(f"\n{'='*60}")
        click.echo(f"Review Complete - Total Suggestions: {len(all_suggestions)}")
        click.echo(f"{'='*60}")

        if not all_suggestions:
            click.echo("\nNo suggestions generated. All files look good!")
            return

        # Step 5: Interactive approval
        click.echo(f"\n{'='*60}")
        click.echo("Review Suggestions")
        click.echo(f"{'='*60}\n")

        accepted_suggestions = []
        for i, suggestion in enumerate(all_suggestions, 1):
            click.echo(f"\nSuggestion {i}/{len(all_suggestions)}")
            click.echo(f"  File: {suggestion['file']}")
            click.echo(f"  Lines: {suggestion['line_start']}-{suggestion['line_end']}")
            click.echo(f"  Category: {suggestion['category']}")
            click.echo(f"  Reason: {suggestion['reason']}")
            click.echo(f"\n  Current code:")
            click.echo(f"    {self._indent_code(suggestion['current_code'], 4)}")
            click.echo(f"\n  Suggested code:")
            click.echo(f"    {self._indent_code(suggestion['suggested_code'], 4)}")

            # Ask user for approval
            if click.confirm("\n  Accept this suggestion?", default=False):
                accepted_suggestions.append(suggestion)
                click.echo("    ✓ Accepted")
            else:
                click.echo("    ✗ Rejected")

        if not accepted_suggestions:
            click.echo("\nNo suggestions accepted. No review to post.")
            return

        # Step 6: Prepare summary comment
        summary = f"""Code review completed for PR #{pr_number}.

Documentation reviewed:
{result['documentation_summary']}

Changes analyzed:
{result['commits_summary'][:300]}{"..." if len(result['commits_summary']) > 300 else ""}

Review criteria applied:
{result['review_criteria_applied'][:200]}{"..." if len(result['review_criteria_applied']) > 200 else ""}

Total suggestions: {len(all_suggestions)}
Accepted suggestions: {len(accepted_suggestions)}"""

        summary_body = self._format_summary_comment(len(accepted_suggestions), summary)

        # Let user review/edit the summary comment
        click.echo(f"\n{'='*60}")
        click.echo("Review Summary Comment")
        click.echo(f"{'='*60}\n")
        click.echo(summary_body)
        click.echo(f"\n{'='*60}")

        # Ask what to do with the summary
        click.echo("\nOptions:")
        click.echo("  [a] Accept summary")
        click.echo("  [e] Edit summary")
        click.echo("  [s] Skip summary")

        post_summary = True
        choice = click.prompt("Choose an option", type=click.Choice(['a', 'e', 's'], case_sensitive=False), default='a')

        if choice == 'e':
            click.echo("\nEnter your edited summary (press Ctrl+D or Ctrl+Z when done):")
            click.echo("(Each line you type will be added to the summary)\n")
            lines = []
            try:
                while True:
                    line = input()
                    lines.append(line)
            except EOFError:
                pass

            if lines:
                summary_body = "\n".join(lines)
                click.echo(f"\n{'='*60}")
                click.echo("Updated Summary:")
                click.echo(f"{'='*60}\n")
                click.echo(summary_body)
                click.echo(f"\n{'='*60}")

        if choice == 's':
            post_summary = False
            click.echo("\nSummary comment will be skipped")

        # Final confirmation before posting everything
        click.echo(f"\n{'='*60}")
        click.echo("Ready to Post Review")
        click.echo(f"{'='*60}")
        click.echo(f"\nThis will post to PR #{pr_number}:")
        if post_summary:
            click.echo(f"  • 1 summary comment")
        click.echo(f"  • {len(accepted_suggestions)} inline suggestions")
        click.echo()

        if not click.confirm("Post review to GitHub?", default=True):
            click.echo("\nCancelled. Review not posted.")
            return

        # Post to GitHub
        click.echo(f"\n{'='*60}")
        click.echo(f"Posting review to GitHub")
        click.echo(f"{'='*60}\n")

        # Post summary comment if not skipped
        if post_summary:
            try:
                self.github.post_issue_comment(repo_name, pr_number, summary_body)
                click.echo(f"✓ Posted review summary")
            except Exception as e:
                click.echo(f"Warning: Failed to post summary: {e}", err=True)

        # Post each suggestion as an inline comment
        success_count = 0
        failed_suggestions = []

        for i, suggestion in enumerate(accepted_suggestions, 1):
            try:
                body = self._format_inline_comment(suggestion)
                self.github.post_review_comment(
                    repo_name,
                    pr_number,
                    pr_info["head_sha"],
                    suggestion["file"],
                    suggestion["line_start"],
                    suggestion["line_end"],
                    body,
                )
                click.echo(f"✓ Posted inline comment {i}/{len(accepted_suggestions)}: {suggestion['file']}")
                success_count += 1
            except Exception as e:
                click.echo(f"✗ Failed to post comment {i}: {e}", err=True)
                failed_suggestions.append(suggestion)

        # Step 7: Show results
        click.echo(f"\n{'='*60}")
        click.echo(f"Posted {success_count}/{len(accepted_suggestions)} inline comments")
        click.echo(f"{'='*60}")

        if failed_suggestions:
            click.echo(f"\n⚠ Failed to post {len(failed_suggestions)} suggestions as inline comments")
            click.echo("These suggestions could not be posted (file may not exist in PR diff):")
            for s in failed_suggestions:
                click.echo(f"  - {s['file']}:{s['line_start']}-{s['line_end']}")

        click.echo(f"\nView the review at:")
        click.echo(f"  https://github.com/{repo_name}/pull/{pr_number}")

    def _get_repo_name(self, project_dir: Path) -> str:
        """Get GitHub repository owner/name from git remote."""
        try:
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            remote_url = result.stdout.strip()

            # Parse GitHub URL
            # Support both SSH (git@github.com:owner/repo.git) and HTTPS (https://github.com/owner/repo.git)
            if "github.com" in remote_url:
                # Extract owner/repo
                match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(\.git)?$', remote_url)
                if match:
                    return match.group(1)

            raise ValueError(f"Could not parse GitHub repository from: {remote_url}")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get git remote: {e.stderr}")
        except Exception as e:
            raise RuntimeError(f"Failed to determine repository: {e}")

    def _indent_code(self, code: str, indent: int) -> str:
        """Indent code snippet for display."""
        lines = code.split("\n")
        return "\n" + "\n".join(" " * indent + line for line in lines)

    def _format_summary_comment(self, count: int, summary: str) -> str:
        """Format summary as a GitHub comment in Markdown."""
        parts = [
            "## 🤖 LLM Code Review",
            "",
            summary,
            "",
            f"Posted {count} inline suggestions on specific lines. Check the 'Files changed' tab to see them.",
            "",
            "---",
            "*This review was generated by llm-sandbox with LLM assistance*",
        ]
        return "\n".join(parts)

    def _format_inline_comment(self, suggestion: dict) -> str:
        """Format a single suggestion as an inline comment."""
        category_emoji = {
            "bug": "🐛",
            "performance": "⚡",
            "security": "🔒",
            "style": "💅",
            "refactor": "♻️",
            "documentation": "📝",
        }.get(suggestion["category"], "💡")

        parts = [
            f"**{category_emoji} {suggestion['category'].title()}**",
            "",
            suggestion["reason"],
            "",
            "<details>",
            "<summary>Suggested change</summary>",
            "",
            "```suggestion",
            suggestion["suggested_code"],
            "```",
            "</details>",
        ]
        return "\n".join(parts)
