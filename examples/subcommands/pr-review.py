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
"""

import json
import subprocess
import sys
from pathlib import Path

import click

from llm_sandbox.subcommand import Subcommand


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
        return command

    def execute(self, project_dir: Path, run_sandbox, **kwargs):
        """Execute PR review workflow."""
        pr_number = kwargs["pr"]
        max_suggestions = kwargs.get("max_suggestions", 10)

        click.echo(f"\n{'='*60}")
        click.echo(f"GitHub PR Review: #{pr_number}")
        click.echo(f"{'='*60}\n")

        # Step 1: Fetch PR info using gh CLI
        click.echo("Fetching PR information...")
        try:
            pr_info = self._fetch_pr_info(pr_number)
            click.echo(f"  PR Title: {pr_info['title']}")
            click.echo(f"  Branch: {pr_info['head_ref']}")
            click.echo(f"  Base: {pr_info['base_ref']}")
            click.echo(f"  Author: {pr_info['author']}")
            click.echo(f"  Head SHA: {pr_info['head_sha']}")
        except Exception as e:
            click.echo(f"Error fetching PR info: {e}", err=True)
            sys.exit(1)

        # Step 2: Define the review prompt and schema
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

        # Step 3: Get LLM review
        click.echo("\nAnalyzing PR with LLM...")
        review_result = run_sandbox(
            prompt=review_prompt,
            output_schema=suggestion_schema,
        )

        # Step 4: Show summary
        click.echo(f"\n{'='*60}")
        click.echo("Review Summary")
        click.echo(f"{'='*60}")
        click.echo(review_result["summary"])
        click.echo(f"\nFound {len(review_result['suggestions'])} suggestions")

        if not review_result["suggestions"]:
            click.echo("\nNo suggestions to apply. PR looks good!")
            return

        # Step 5: Interactive approval
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

        # Step 6: Post review to GitHub
        click.echo(f"\n{'='*60}")
        click.echo(f"Posting {len(accepted_suggestions)} suggestions to GitHub")
        click.echo(f"{'='*60}\n")

        # Post summary comment
        summary_body = self._format_summary_comment(len(accepted_suggestions), review_result["summary"])
        try:
            self._post_pr_comment(pr_number, summary_body)
            click.echo(f"✓ Posted review summary")
        except Exception as e:
            click.echo(f"Warning: Failed to post summary: {e}", err=True)

        # Post each suggestion as an inline comment
        repo_name = self._get_repo_name()
        success_count = 0
        failed_suggestions = []

        for i, suggestion in enumerate(accepted_suggestions, 1):
            try:
                self._post_inline_comment(
                    repo_name,
                    pr_number,
                    pr_info["head_sha"],
                    suggestion,
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

    def _fetch_pr_info(self, pr_number: int) -> dict:
        """Fetch PR information using gh CLI."""
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "title,headRefName,baseRefName,author,headRefOid",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            return {
                "title": data["title"],
                "head_ref": data["headRefName"],
                "base_ref": data["baseRefName"],
                "author": data["author"]["login"],
                "head_sha": data["headRefOid"],
            }
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to fetch PR info. Is 'gh' CLI installed and authenticated?\n{e.stderr}"
            )
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse PR info: {e}")

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

    def _post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post a general comment to GitHub PR using gh CLI."""
        try:
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_number),
                    "--body",
                    body,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to post PR comment. Is 'gh' CLI installed and authenticated?\n{e.stderr}"
            )

    def _post_inline_comment(
        self,
        repo_name: str,
        pr_number: int,
        commit_sha: str,
        suggestion: dict,
    ) -> None:
        """Post an inline review comment on specific lines using GitHub API."""
        body = self._format_inline_comment(suggestion)

        # Build gh api command
        # For single line comments, use 'line'
        # For multi-line comments, use 'start_line' and 'line'
        cmd = [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            f"/repos/{repo_name}/pulls/{pr_number}/comments",
            "-f",
            f"body={body}",
            "-f",
            f"commit_id={commit_sha}",
            "-f",
            f"path={suggestion['file']}",
            "-f",
            "side=RIGHT",
        ]

        # Add line parameters
        if suggestion["line_start"] == suggestion["line_end"]:
            # Single line comment
            cmd.extend(["-F", f"line={suggestion['line_end']}"])
        else:
            # Multi-line comment
            cmd.extend([
                "-F", f"start_line={suggestion['line_start']}",
                "-f", "start_side=RIGHT",
                "-F", f"line={suggestion['line_end']}",
            ])

        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to post inline comment: {e.stderr}")

    def _get_repo_name(self) -> str:
        """Get the GitHub repository name (owner/repo)."""
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return "unknown/repo"
