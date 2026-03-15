"""Review target abstractions for different review sources (local, GitHub PR, etc)."""

import re
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import requests

from .models import Review, FeedbackItem


class ReviewTarget(ABC):
    """Abstract base class for review targets (local, GitHub PR, GitLab MR, etc)."""

    @abstractmethod
    def get_base_ref(self) -> str:
        """Get the base commit/branch reference."""
        pass

    @abstractmethod
    def get_head_ref(self) -> str:
        """Get the head commit/branch reference."""
        pass

    @abstractmethod
    def get_description(self) -> str:
        """Get a human-readable description of the review target."""
        pass

    @abstractmethod
    def fetch_if_needed(self, project_dir: Path) -> None:
        """Fetch remote refs if needed (e.g., PR from GitHub)."""
        pass

    @abstractmethod
    def get_diff(self) -> Optional[str]:
        """Get the full diff for the review. Returns None if not available."""
        pass

    @abstractmethod
    def get_commits(self) -> Optional[List[Dict[str, Any]]]:
        """Get the list of commits in the review. Returns None if not available."""
        pass

    @abstractmethod
    def can_publish(self) -> bool:
        """Whether this target supports publishing reviews."""
        pass

    @abstractmethod
    def publish_review(self, review: Review, pr_info: Optional[Dict] = None) -> None:
        """Publish the review to the target (e.g., post to GitHub)."""
        pass


class LocalReviewTarget(ReviewTarget):
    """Review target for local commits/branches."""

    def __init__(self, base_ref: str, head_ref: str):
        self.base_ref = base_ref
        self.head_ref = head_ref

    def get_base_ref(self) -> str:
        return self.base_ref

    def get_head_ref(self) -> str:
        return self.head_ref

    def get_description(self) -> str:
        return f"{self.base_ref}..{self.head_ref}"

    def fetch_if_needed(self, project_dir: Path) -> None:
        # Local refs, nothing to fetch
        pass

    def get_diff(self) -> Optional[str]:
        # For local reviews, diff should be obtained via git commands
        # Agent can use execute_command tool
        return None

    def get_commits(self) -> Optional[List[Dict[str, Any]]]:
        # For local reviews, commits should be obtained via git commands
        # Agent can use execute_command tool
        return None

    def can_publish(self) -> bool:
        return False

    def publish_review(self, review: Review, pr_info: Optional[Dict] = None) -> None:
        raise NotImplementedError("Cannot publish local reviews to remote")


class GitHubPRTarget(ReviewTarget):
    """Review target for GitHub Pull Requests."""

    def __init__(self, pr_number: int, token: str, project_dir: Path):
        self.pr_number = pr_number
        self.token = token
        self.project_dir = project_dir
        self.github_client = None
        self.repo_name = None
        self.pr_info = None
        self.pr_head_branch = None

    def get_description(self) -> str:
        return f"PR #{self.pr_number}"

    def fetch_if_needed(self, project_dir: Path) -> None:
        """Fetch PR information and head branch from GitHub."""
        self.github_client = GitHubClient(self.token)

        # Get repository name
        click.echo("Fetching repository information...")
        try:
            self.repo_name = self._get_repo_name(project_dir)
            click.echo(f"  Repository: {self.repo_name}")
        except Exception as e:
            click.echo(f"Error getting repository: {e}", err=True)
            sys.exit(1)

        # Fetch PR info
        click.echo("Fetching PR information...")
        try:
            self.pr_info = self.github_client.get_pull_request(self.repo_name, self.pr_number)
            click.echo(f"  PR Title: {self.pr_info['title']}")
            click.echo(f"  Branch: {self.pr_info['head_ref']} ({self.pr_info['head_sha'][:7]})")
            click.echo(f"  Base: {self.pr_info['base_ref']} ({self.pr_info['base_sha'][:7]})")
            click.echo(f"  Author: {self.pr_info['author']}")
        except Exception as e:
            click.echo(f"Error fetching PR info: {e}", err=True)
            sys.exit(1)

        # Fetch PR head
        click.echo("\nFetching PR commits...")
        self.pr_head_branch = f"fetch/pr-{self.pr_number}/{self.pr_info['head_ref']}"

        try:
            subprocess.run(
                ["git", "fetch", "origin", f"pull/{self.pr_number}/head:{self.pr_head_branch}"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            click.echo(f"  Fetched PR head: {self.pr_head_branch}")
        except subprocess.CalledProcessError as e:
            click.echo(f"Error fetching PR head: {e.stderr}", err=True)
            sys.exit(1)

    def get_base_ref(self) -> str:
        if not self.pr_info:
            raise RuntimeError("Must call fetch_if_needed() first")
        return self.pr_info['base_ref']

    def get_head_ref(self) -> str:
        if not self.pr_head_branch:
            raise RuntimeError("Must call fetch_if_needed() first")
        return self.pr_head_branch

    def get_diff(self) -> Optional[str]:
        """Get the full PR diff from GitHub API."""
        if not self.github_client or not self.repo_name:
            raise RuntimeError("Must call fetch_if_needed() first")

        try:
            return self.github_client.get_pull_request_diff(self.repo_name, self.pr_number)
        except Exception as e:
            click.echo(f"Warning: Failed to fetch PR diff from GitHub: {e}", err=True)
            return None

    def get_commits(self) -> Optional[List[Dict[str, Any]]]:
        """Get the list of commits from GitHub API."""
        if not self.github_client or not self.repo_name:
            raise RuntimeError("Must call fetch_if_needed() first")

        try:
            return self.github_client.get_pull_request_commits(self.repo_name, self.pr_number)
        except Exception as e:
            click.echo(f"Warning: Failed to fetch PR commits from GitHub: {e}", err=True)
            return None

    def can_publish(self) -> bool:
        return True

    def publish_review(self, review: Review, pr_info: Optional[Dict] = None) -> None:
        """Post review summary and inline comments to GitHub."""
        if not self.github_client or not self.pr_info:
            raise RuntimeError("Must call fetch_if_needed() first")

        # Format summary for GitHub
        if review.summary:
            summary_body = self._format_summary_comment(len(review.feedback), review.summary)

            # Show summary
            click.echo(f"\n{'='*60}")
            click.echo("Review Summary Comment")
            click.echo(f"{'='*60}\n")
            click.echo(summary_body)
            click.echo(f"\n{'='*60}")

        # Final confirmation
        click.echo(f"\n{'='*60}")
        click.echo("Ready to Post Review")
        click.echo(f"{'='*60}")
        click.echo(f"\nThis will post to PR #{self.pr_number}:")
        if review.summary:
            click.echo(f"  • 1 summary comment")
        click.echo(f"  • {len(review.feedback)} inline suggestions")
        click.echo()

        if not click.confirm("Post review to GitHub?", default=True):
            click.echo("\nCancelled. Review not posted.")
            return

        # Post to GitHub
        click.echo(f"\n{'='*60}")
        click.echo(f"Posting review to GitHub")
        click.echo(f"{'='*60}\n")

        # Post summary
        if review.summary:
            try:
                self.github_client.post_issue_comment(self.repo_name, self.pr_number, summary_body)
                click.echo(f"✓ Posted review summary")
            except Exception as e:
                click.echo(f"Warning: Failed to post summary: {e}", err=True)

        # Post inline comments
        success_count = 0
        failed_suggestions = []

        for i, suggestion in enumerate(review.feedback, 1):
            try:
                body = self._format_inline_comment(suggestion)
                self.github_client.post_review_comment(
                    self.repo_name,
                    self.pr_number,
                    self.pr_info["head_sha"],
                    suggestion.file,
                    suggestion.line_start,
                    suggestion.line_end,
                    body,
                )
                click.echo(f"✓ Posted inline comment {i}/{len(review.feedback)}: {suggestion.file}")
                success_count += 1
            except Exception as e:
                click.echo(f"✗ Failed to post comment {i}: {e}", err=True)
                failed_suggestions.append(suggestion)

        # Show results
        click.echo(f"\n{'='*60}")
        click.echo(f"Posted {success_count}/{len(review.feedback)} inline comments")
        click.echo(f"{'='*60}")

        if failed_suggestions:
            click.echo(f"\n⚠ Failed to post {len(failed_suggestions)} suggestions as inline comments")
            click.echo("These suggestions could not be posted (file may not exist in PR diff):")
            for s in failed_suggestions:
                click.echo(f"  - {s.file}:{s.line_start}-{s.line_end}")

        click.echo(f"\nView the review at:")
        click.echo(f"  https://github.com/{self.repo_name}/pull/{self.pr_number}")

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
            if "github.com" in remote_url:
                match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(\.git)?$', remote_url)
                if match:
                    return match.group(1)

            raise ValueError(f"Could not parse GitHub repository from: {remote_url}")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get git remote: {e.stderr}")
        except Exception as e:
            raise RuntimeError(f"Failed to determine repository: {e}")

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

    def _format_inline_comment(self, suggestion: FeedbackItem) -> str:
        """Format a single suggestion as an inline comment."""
        category_emoji = {
            "bug": "🐛",
            "performance": "⚡",
            "security": "🔒",
            "style": "💅",
            "refactor": "♻️",
            "documentation": "📝",
        }.get(suggestion.category, "💡")

        parts = [
            f"**{category_emoji} {suggestion.category.title()}**",
            "",
            suggestion.reason,
            "",
            "<details>",
            "<summary>Suggested change</summary>",
            "",
            "```suggestion",
            suggestion.suggested_code,
            "```",
            "</details>",
        ]
        return "\n".join(parts)


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
