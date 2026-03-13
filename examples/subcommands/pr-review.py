"""Example subcommand: GitHub PR review with interactive suggestions.

This subcommand demonstrates:
1. Fetching a PR branch from GitHub
2. Using the LLM to review changes and suggest improvements
3. Interactive user approval of suggestions
4. Posting accepted suggestions as inline GitHub review comments on specific lines

The review comments appear directly on the relevant lines in the "Files changed" tab,
using GitHub's suggestion feature for easy application.

Usage:
    llm-sandbox pr-review --pr 123
    llm-sandbox pr-review --pr 123 --max-suggestions 5
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

import click
import requests

from llm_sandbox.subcommand import Subcommand


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


class PRReviewSubcommand(Subcommand):
    """Review GitHub PR and suggest improvements interactively."""

    name = "pr-review"
    help = "Review a GitHub PR and interactively apply LLM-suggested improvements"

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
                ["--max-suggestions"],
                type=int,
                default=10,
                help="Maximum number of suggestions to generate (default: 10)",
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

    def execute(self, project_dir: Path, run_sandbox, **kwargs):
        """Execute PR review workflow."""
        pr_number = kwargs["pr"]
        max_suggestions = kwargs.get("max_suggestions", 10)
        token = kwargs.get("with_token") or os.getenv("GH_TOKEN")

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
        click.echo(f"GitHub PR Review: #{pr_number}")
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
            click.echo(f"  Branch: {pr_info['head_ref']}")
            click.echo(f"  Base: {pr_info['base_ref']}")
            click.echo(f"  Author: {pr_info['author']}")
            click.echo(f"  Head SHA: {pr_info['head_sha']}")
        except Exception as e:
            click.echo(f"Error fetching PR info: {e}", err=True)
            sys.exit(1)

        # Step 3: Define the review prompt and schema
        review_prompt = f"""Review the changes in PR #{pr_number} and suggest improvements.

The PR branch is '{pr_info['head_ref']}' (based on '{pr_info['base_ref']}').

Steps:
1. Use checkout_commit to create a worktree named 'pr-{pr_number}' from the PR branch '{pr_info['head_ref']}'
2. Use checkout_commit to create a worktree named 'base' from the base branch '{pr_info['base_ref']}'
3. Compare the changes between the two branches
4. Analyze the code quality, potential bugs, and suggest improvements
5. For each suggestion, provide the file path, line range, current code, suggested code, and reasoning

Focus on:
- Code quality and best practices
- Potential bugs or edge cases
- Performance improvements
- Security concerns
- Readability and maintainability

Provide up to {max_suggestions} specific, actionable suggestions."""

        suggestion_schema = {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of the PR review",
                },
                "suggestions": {
                    "type": "array",
                    "description": "List of improvement suggestions",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "description": "File path relative to worktree",
                            },
                            "line_start": {
                                "type": "integer",
                                "description": "Starting line number",
                            },
                            "line_end": {
                                "type": "integer",
                                "description": "Ending line number",
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
                                "description": "Explanation of why this change is suggested",
                            },
                            "category": {
                                "type": "string",
                                "enum": ["bug", "performance", "security", "style", "refactor"],
                                "description": "Category of the suggestion",
                            },
                        },
                        "required": [
                            "file",
                            "line_start",
                            "line_end",
                            "current_code",
                            "suggested_code",
                            "reason",
                            "category",
                        ],
                    },
                },
            },
            "required": ["summary", "suggestions"],
        }

        # Step 4: Get LLM review
        click.echo("\nAnalyzing PR with LLM...")
        review_result = run_sandbox(
            prompt=review_prompt,
            output_schema=suggestion_schema,
        )

        # Step 5: Show summary
        click.echo(f"\n{'='*60}")
        click.echo("Review Summary")
        click.echo(f"{'='*60}")
        click.echo(review_result["summary"])
        click.echo(f"\nFound {len(review_result['suggestions'])} suggestions")

        if not review_result["suggestions"]:
            click.echo("\nNo suggestions to apply. PR looks good!")
            return

        # Step 6: Interactive approval
        click.echo(f"\n{'='*60}")
        click.echo("Review Suggestions")
        click.echo(f"{'='*60}\n")

        accepted_suggestions = []
        for i, suggestion in enumerate(review_result["suggestions"], 1):
            click.echo(f"\nSuggestion {i}/{len(review_result['suggestions'])}")
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

        # Step 7: Post review to GitHub
        click.echo(f"\n{'='*60}")
        click.echo(f"Posting {len(accepted_suggestions)} suggestions to GitHub")
        click.echo(f"{'='*60}\n")

        # Post summary comment
        summary_body = self._format_summary_comment(len(accepted_suggestions), review_result["summary"])
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

        # Step 8: Show results
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
