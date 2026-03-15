"""Code review subcommand with support for GitHub PRs and local commits.

This subcommand demonstrates a multi-agent review workflow with an abstraction layer:
1. For GitHub PRs: Fetches PR info and head branch from GitHub API
2. For local reviews: Uses provided base and head refs directly
3. Pre-checks out head and base commits into worktrees (review-head and review-base)
4. Agent reads project documentation (AGENTS.md, CLAUDE.md) if available
5. Agent uses review tools (get_review_commits, get_review_diff) to fetch data
   - For GitHub PRs: Data fetched from GitHub API via ReviewTarget
   - For local: Agent uses git commands directly
6. Agent examines commits and changes
7. Agent finds review instruction files in review/ and docs/review/ folders
8. Agent reads ALL review instruction files and applies criteria to ALL changes
9. Agent spawns sub-agents for specific review tasks
10. Sub-agents record findings, orchestrator assigns probabilities and identifies duplicates
11. User approves suggestions and posts to GitHub (or saves locally)

Review Tools:
- get_review_commits: Get commit list (from GitHub API or git commands)
- get_review_diff: Get full diff (from GitHub API or git commands)

The review/ and docs/review/ folders contain INSTRUCTIONS on how to review code,
not the code to review. The actual code to review is identified through git history
between review-base and review-head.

If no review instruction files exist, uses general best practices review criteria.

Usage:
    llm-sandbox pr-review --pr 123                    # Review GitHub PR
    llm-sandbox pr-review --base main --head feature  # Review local commits

Authentication (for GitHub PRs):
    Set GH_TOKEN environment variable or use --with-token option
"""

import json
import os
import asyncio
import re
import subprocess
import sys
import uuid
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import click
import requests
import yaml


# Custom string class for literal block scalar style in YAML
class LiteralString(str):
    pass


def literal_presenter(dumper, data):
    """Present strings as literal block scalars (|)."""
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')


# Register the representer
yaml.add_representer(LiteralString, literal_presenter)


# ============================================================================
# Review Data Classes
# ============================================================================

@dataclass
class SpawnedAgent:
    """Represents a sub-agent that was spawned during review."""

    agent_id: str
    task_description: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "task_description": self.task_description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpawnedAgent":
        """Create from dictionary."""
        return cls(
            agent_id=data["agent_id"],
            task_description=data["task_description"],
        )


@dataclass
class FindingsStatistics:
    """Statistics about review findings."""

    total_findings: int
    duplicates_count: Optional[int] = None
    unique_findings: Optional[int] = None
    by_category: Optional[Dict[str, int]] = None
    by_severity: Optional[Dict[str, int]] = None
    high_confidence_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {"total_findings": self.total_findings}
        if self.duplicates_count is not None:
            result["duplicates_count"] = self.duplicates_count
        if self.unique_findings is not None:
            result["unique_findings"] = self.unique_findings
        if self.by_category is not None:
            result["by_category"] = self.by_category
        if self.by_severity is not None:
            result["by_severity"] = self.by_severity
        if self.high_confidence_count is not None:
            result["high_confidence_count"] = self.high_confidence_count
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FindingsStatistics":
        """Create from dictionary."""
        return cls(
            total_findings=data["total_findings"],
            duplicates_count=data.get("duplicates_count"),
            unique_findings=data.get("unique_findings"),
            by_category=data.get("by_category"),
            by_severity=data.get("by_severity"),
            high_confidence_count=data.get("high_confidence_count"),
        )


@dataclass
class ReviewMetadata:
    """Metadata from the review agent's execution."""

    review_summary: str
    documentation_found: List[str]
    review_criteria_summary: str
    sub_agents_spawned: List[SpawnedAgent]
    findings_statistics: FindingsStatistics
    overall_assessment: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "review_summary": self.review_summary,
            "documentation_found": self.documentation_found,
            "review_criteria_summary": self.review_criteria_summary,
            "sub_agents_spawned": [a.to_dict() for a in self.sub_agents_spawned],
            "findings_statistics": self.findings_statistics.to_dict(),
            "overall_assessment": self.overall_assessment,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewMetadata":
        """Create from dictionary."""
        return cls(
            review_summary=data["review_summary"],
            documentation_found=data["documentation_found"],
            review_criteria_summary=data["review_criteria_summary"],
            sub_agents_spawned=[SpawnedAgent.from_dict(a) for a in data["sub_agents_spawned"]],
            findings_statistics=FindingsStatistics.from_dict(data["findings_statistics"]),
            overall_assessment=data["overall_assessment"],
        )


@dataclass
class Review:
    """Container for code review results."""

    summary: Optional[str]  # Review summary text
    feedback: List["FeedbackItem"]  # List of feedback items
    metadata: Optional["ReviewMetadata"] = None  # Agent result metadata

    def filter_feedback(self, probability_threshold: float = 0.5) -> List["FeedbackItem"]:
        """Filter feedback by probability and exclude duplicates."""
        # Filter: keep items with probability >= threshold AND not marked as duplicate
        filtered = [
            f for f in self.feedback
            if (f.probability is None or f.probability >= probability_threshold)
            and f.duplicate_of is None
            and not f.ignore
        ]

        # Sort by probability (highest first, None values last)
        return sorted(filtered, key=lambda x: x.probability if x.probability is not None else 0.0, reverse=True)

    def get_statistics(self) -> Dict[str, int]:
        """Get review statistics."""
        duplicates = len([f for f in self.feedback if f.duplicate_of is not None])
        ignored = len([f for f in self.feedback if f.ignore])
        return {
            "total": len(self.feedback),
            "duplicates": duplicates,
            "ignored": ignored,
            "unique": len(self.feedback) - duplicates - ignored,
        }


@dataclass
class FeedbackItem:
    """Represents a single review feedback item."""

    # Location information (required)
    file: str
    line_start: int
    line_end: int

    # Content (required)
    reason: str
    category: Literal["bug", "performance", "security", "style", "refactor", "documentation", "best-practice"]

    # Code snippets (optional)
    current_code: str = ""
    suggested_code: str = ""

    # Severity (optional, default: medium)
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"

    # Confidence/validation (optional, added by orchestrator)
    probability: Optional[float] = None
    probability_reasoning: str = ""

    # Duplicate tracking (optional, added by orchestrator)
    duplicate_of: Optional[int] = None
    duplicate_reasoning: str = ""

    # User override (optional, set during editing)
    ignore: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "reason": self.reason,
            "category": self.category,
        }

        # Add optional fields only if they have values
        if self.current_code:
            result["current_code"] = self.current_code
        if self.suggested_code:
            result["suggested_code"] = self.suggested_code
        if self.severity != "medium":
            result["severity"] = self.severity
        if self.probability is not None:
            result["probability"] = self.probability
        if self.probability_reasoning:
            result["probability_reasoning"] = self.probability_reasoning
        if self.duplicate_of is not None:
            result["duplicate_of"] = self.duplicate_of
        if self.duplicate_reasoning:
            result["duplicate_reasoning"] = self.duplicate_reasoning
        if self.ignore:
            result["ignore"] = self.ignore

        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeedbackItem":
        """Create from dictionary."""
        return cls(
            file=data["file"],
            line_start=data["line_start"],
            line_end=data["line_end"],
            reason=data["reason"],
            category=data["category"],
            current_code=data.get("current_code", ""),
            suggested_code=data.get("suggested_code", ""),
            severity=data.get("severity", "medium"),
            probability=data.get("probability"),
            probability_reasoning=data.get("probability_reasoning", ""),
            duplicate_of=data.get("duplicate_of"),
            duplicate_reasoning=data.get("duplicate_reasoning", ""),
            ignore=data.get("ignore", False),
        )


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

    def _format_inline_comment(self, suggestion: "FeedbackItem") -> str:
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


from llm_sandbox import AgentConfig
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
    SpawnAgentTool,
    WaitForAgentsTool,
)
from llm_sandbox.subcommand import Subcommand


class GetReviewDiffTool(MCPTool):
    """Generic MCP tool for fetching review diff from ReviewTarget."""

    def __init__(self, review_target: "ReviewTarget"):
        """
        Initialize the review diff tool.

        Args:
            review_target: ReviewTarget instance that provides the diff
        """
        super().__init__(
            name="get_review_diff",
            description="Get the full diff of the code changes being reviewed. Returns the unified diff format showing all changes. For GitHub PRs, this fetches from the GitHub API. For local reviews, use git commands instead.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        self.review_target = review_target

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Fetch the diff from the review target."""
        try:
            diff = self.review_target.get_diff()
            if diff is None:
                return {
                    "success": False,
                    "error": "Diff not available from review target. Use git commands to get diff (e.g., git diff base..head).",
                }
            return {
                "success": True,
                "diff": diff,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch diff: {str(e)}",
            }


class GetReviewCommitsTool(MCPTool):
    """Generic MCP tool for fetching review commits from ReviewTarget."""

    def __init__(self, review_target: "ReviewTarget"):
        """
        Initialize the review commits tool.

        Args:
            review_target: ReviewTarget instance that provides the commits
        """
        super().__init__(
            name="get_review_commits",
            description="Get the list of commits in the code review. Returns commit SHAs, messages, authors, and timestamps. For GitHub PRs, this fetches from the GitHub API. For local reviews, use git commands instead.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )
        self.review_target = review_target

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Fetch the commits from the review target."""
        try:
            commits = self.review_target.get_commits()
            if commits is None:
                return {
                    "success": False,
                    "error": "Commits not available from review target. Use git commands to get commits (e.g., git log base..head).",
                }
            return {
                "success": True,
                "commits": commits,
                "count": len(commits),
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch commits: {str(e)}",
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


class ReviewFeedbackEditor:
    """Handles serialization and editing of review feedback."""

    @staticmethod
    def serialize(feedback: List["FeedbackItem"], summary: Optional[str] = None) -> str:
        """
        Serialize feedback into human-editable YAML format.

        Args:
            feedback: List of feedback items
            summary: Optional review summary comment

        Returns:
            Multi-document YAML representation of feedback
        """
        lines = []
        lines.append("# PR Review Feedback - Multi-Document YAML")
        lines.append("# Edit this file to modify review suggestions")
        lines.append("#")
        lines.append("# How to ignore/delete suggestions:")
        lines.append("# - Delete the entire YAML document (between --- separators)")
        lines.append("# - Delete the index line (e.g., '#   3. file.py:10-20 ...')")
        lines.append("# - OR add 'ignore: true' to the suggestion")
        lines.append("#")
        lines.append("# Suggestion Index:")

        # Add suggestion index/table of contents
        for i, item in enumerate(feedback):
            prob = item.probability if item.probability is not None else 1.0
            dup_marker = " [DUPLICATE]" if item.duplicate_of is not None else ""
            lines.append(f"#   {i + 1}. {item.file}:{item.line_start}-{item.line_end} "
                        f"[{item.category}, {item.severity}, p={prob:.2f}]{dup_marker}")
        lines.append("")

        # Add review summary as first YAML document if provided
        if summary:
            lines.append("---")
            lines.append("# Review Summary Comment")
            lines.append("# Edit the 'summary' field below to customize the GitHub comment")
            summary_doc = {"_type": "review_summary", "summary": LiteralString(summary)}
            summary_yaml = yaml.dump(
                summary_doc,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=100
            )
            lines.append(summary_yaml.rstrip())

        # Serialize each feedback item as a YAML document
        for item in feedback:
            lines.append("---")

            # Convert to dict and wrap multiline string fields with LiteralString
            item_dict = item.to_dict()
            if item_dict.get('current_code'):
                item_dict['current_code'] = LiteralString(item_dict['current_code'])
            if item_dict.get('suggested_code'):
                item_dict['suggested_code'] = LiteralString(item_dict['suggested_code'])

            # Use yaml.dump with proper configuration for readability
            yaml_str = yaml.dump(
                item_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=100
            )
            lines.append(yaml_str.rstrip())

        return "\n".join(lines)

    @staticmethod
    def deserialize(text: str) -> tuple[Optional[str], List["FeedbackItem"]]:
        """
        Deserialize multi-document YAML back into feedback items.

        Args:
            text: Multi-document YAML representation of feedback

        Returns:
            Tuple of (summary, feedback_list) where summary is Optional[str] and feedback_list excludes ignored items
        """
        feedback = []
        summary = None

        try:
            # Parse suggestion index to get valid suggestion numbers
            # Format: "#   1. file.py:10-20 [category, severity, p=0.95]"
            valid_indices = set()
            for line in text.split('\n'):
                # Match index lines like "#   1. file.py:..."
                match = re.match(r'^\#\s+(\d+)\.\s+', line)
                if match:
                    valid_indices.add(int(match.group(1)))

            # Load all YAML documents
            documents = list(yaml.safe_load_all(text))

            # Process each document
            doc_index = 0  # Track position for non-summary documents
            for doc in documents:
                if doc is None:
                    continue

                if not isinstance(doc, dict):
                    continue

                # Check if this is the review summary document
                if doc.get('_type') == 'review_summary':
                    summary = doc.get('summary')
                    continue

                # Track position for feedback documents
                doc_index += 1

                # Skip if index line was deleted (not in valid_indices)
                # But only check if we found any index lines (to handle old files)
                if valid_indices and doc_index not in valid_indices:
                    continue

                # Check if item should be ignored via 'ignore: true'
                ignore = doc.get('ignore', False)
                if isinstance(ignore, str):
                    ignore = ignore.lower() in ['true', 'yes', '1']

                if not ignore:
                    # Remove the ignore field and create FeedbackItem
                    doc.pop('ignore', None)
                    feedback.append(FeedbackItem.from_dict(doc))

        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse YAML: {e}")

        return summary, feedback

    @staticmethod
    def save_feedback(feedback: List["FeedbackItem"], project_dir: Path, feedback_id: Optional[str] = None, summary: Optional[str] = None) -> tuple[str, Path]:
        """
        Save feedback to a file.

        Args:
            feedback: List of feedback items
            project_dir: Project directory path
            feedback_id: Optional ID to use (generates new one if not provided)
            summary: Optional review summary to include in file

        Returns:
            Tuple of (feedback_id, file_path)
        """
        if not feedback_id:
            feedback_id = str(uuid.uuid4())[:8]

        # Save to project directory instead of temp
        review_dir = project_dir / ".llm-sandbox" / "pr-review"
        review_dir.mkdir(parents=True, exist_ok=True)

        review_file = review_dir / f"{feedback_id}.yaml"
        review_file.write_text(ReviewFeedbackEditor.serialize(feedback, summary=summary))
        return feedback_id, review_file

    @staticmethod
    def load_feedback(feedback_id_or_path: str, project_dir: Path) -> Optional[tuple[Optional[str], List["FeedbackItem"]]]:
        """
        Load feedback from a file or ID.

        Args:
            feedback_id_or_path: Either a feedback ID or full file path
            project_dir: Project directory path

        Returns:
            Tuple of (summary, feedback_list), or None if not found/invalid
        """
        # Check if it's a full path
        file_path = Path(feedback_id_or_path)
        if not file_path.exists():
            # Try as ID in project directory
            review_dir = project_dir / ".llm-sandbox" / "pr-review"
            file_path = review_dir / f"{feedback_id_or_path}.yaml"

        if not file_path.exists():
            return None

        try:
            text = file_path.read_text()
            summary, feedback = ReviewFeedbackEditor.deserialize(text)
            return summary, feedback
        except Exception as e:
            click.echo(f"Error loading feedback from {file_path}: {e}", err=True)
            return None

    @staticmethod
    def edit_feedback(feedback: List["FeedbackItem"], project_dir: Path, feedback_id: Optional[str] = None, summary: Optional[str] = None) -> Optional[tuple[Optional[str], List["FeedbackItem"]]]:
        """
        Open feedback in editor for human modification.

        Args:
            feedback: List of feedback items
            project_dir: Project directory path
            feedback_id: Optional ID to use (generates new one if not provided)
            summary: Optional review summary to include in file

        Returns:
            Tuple of (summary, feedback_list), or None if user cancelled
        """
        # Check if we're editing an existing file
        review_dir = project_dir / ".llm-sandbox" / "pr-review"
        review_dir.mkdir(parents=True, exist_ok=True)

        if feedback_id:
            review_file = review_dir / f"{feedback_id}.yaml"
            if not review_file.exists():
                # File doesn't exist yet, create it
                feedback_id, review_file = ReviewFeedbackEditor.save_feedback(feedback, project_dir, feedback_id, summary=summary)
            # else: File exists, don't overwrite it - just use it as-is
        else:
            # No ID provided, create new file
            feedback_id, review_file = ReviewFeedbackEditor.save_feedback(feedback, project_dir, feedback_id, summary=summary)

        # Print file location
        click.echo(f"\n{'='*60}")
        click.echo(f"Review Feedback Saved")
        click.echo(f"{'='*60}")
        click.echo(f"File: {review_file}")
        click.echo(f"ID: {feedback_id}")
        click.echo(f"\nYou can reload this review later with:")
        click.echo(f"  llm-sandbox pr-review --pr <number> --load-review {feedback_id}")
        click.echo(f"{'='*60}\n")

        # Get editor from environment, default to vi
        editor = os.environ.get('EDITOR', 'vi')

        try:
            while True:
                # Open editor
                try:
                    subprocess.run([editor, str(review_file)], check=True)
                except subprocess.CalledProcessError:
                    click.echo(f"Error: Editor '{editor}' failed", err=True)
                    return None

                # Read modified content
                try:
                    modified_text = review_file.read_text()
                    modified_summary, modified_feedback = ReviewFeedbackEditor.deserialize(modified_text)
                except Exception as e:
                    click.echo(f"Error parsing edited feedback: {e}", err=True)
                    choice = click.prompt(
                        "What would you like to do?",
                        type=click.Choice(['edit', 'cancel'], case_sensitive=False),
                        default='edit'
                    )
                    if choice == 'cancel':
                        return None
                    continue

                # Show summary of changes
                click.echo(f"\n{'='*60}")
                click.echo(f"Feedback Summary")
                click.echo(f"{'='*60}")
                click.echo(f"Original suggestions: {len(feedback)}")
                click.echo(f"After editing: {len(modified_feedback)}")
                click.echo(f"Deleted: {len(feedback) - len(modified_feedback)}")

                # Ask what to do next
                click.echo("\nWhat would you like to do?")
                click.echo("  [p] Post review to GitHub")
                click.echo("  [e] Edit again")
                click.echo("  [q] Quit without posting")

                choice = click.prompt(
                    "Choose an option",
                    type=click.Choice(['p', 'e', 'q'], case_sensitive=False),
                    default='p'
                )

                if choice == 'p':
                    # File already saved by editor, just return for posting
                    return modified_summary, modified_feedback
                elif choice == 'q':
                    # File already saved by editor, just quit
                    click.echo(f"\nFeedback saved to: {review_file}")
                    click.echo(f"ID: {feedback_id}")
                    return None
                # else 'e' - loop continues

        except KeyboardInterrupt:
            click.echo("\n\nInterrupted. Feedback saved for later use.")
            return None


class RecordReviewFeedbackTool(MCPTool):
    """Tool for recording review feedback during PR analysis."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize record review feedback tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="record_review_feedback",
            description="Record a review suggestion or feedback item. Use this to incrementally build up review feedback as you analyze the PR. Later you can retrieve all feedback with get_review_feedback.",
            parameters={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path relative to repository root",
                    },
                    "line_start": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed)",
                    },
                    "line_end": {
                        "type": "integer",
                        "description": "Ending line number (1-indexed)",
                    },
                    "current_code": {
                        "type": "string",
                        "description": "Current code snippet that needs review",
                    },
                    "suggested_code": {
                        "type": "string",
                        "description": "Suggested improved code (can be empty if just a comment)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Explanation of why this change is suggested",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["bug", "performance", "security", "style", "refactor", "documentation", "best-practice"],
                        "description": "Category of the feedback",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                        "description": "Severity level (optional, defaults to 'medium')",
                        "default": "medium",
                    },
                },
                "required": ["file", "line_start", "line_end", "reason", "category"],
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Record review feedback."""
        feedback_item = FeedbackItem(
            file=arguments["file"],
            line_start=arguments["line_start"],
            line_end=arguments["line_end"],
            current_code=arguments.get("current_code", ""),
            suggested_code=arguments.get("suggested_code", ""),
            reason=arguments["reason"],
            category=arguments["category"],
            severity=arguments.get("severity", "medium"),
        )

        self.runner._review_feedback.append(feedback_item)

        return {
            "success": True,
            "message": f"Recorded feedback for {arguments['file']}:{arguments['line_start']}-{arguments['line_end']}",
            "total_feedback_items": len(self.runner._review_feedback),
        }


class GetReviewFeedbackTool(MCPTool):
    """Tool for retrieving recorded review feedback."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize get review feedback tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="get_review_feedback",
            description="Retrieve all recorded review feedback. Returns a list of all feedback items recorded so far.",
            parameters={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Optional: filter by file path",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional: filter by category",
                    },
                    "severity": {
                        "type": "string",
                        "description": "Optional: filter by severity",
                    },
                },
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Retrieve review feedback."""
        feedback = self.runner._review_feedback

        # Apply filters if provided
        file_filter = arguments.get("file")
        category_filter = arguments.get("category")
        severity_filter = arguments.get("severity")

        if file_filter:
            feedback = [f for f in feedback if f.file == file_filter]

        if category_filter:
            feedback = [f for f in feedback if f.category == category_filter]

        if severity_filter:
            feedback = [f for f in feedback if f.severity == severity_filter]

        # Convert to dicts for API response
        feedback_dicts = [f.to_dict() for f in feedback]

        return {
            "success": True,
            "feedback": feedback_dicts,
            "count": len(feedback_dicts),
            "total_recorded": len(self.runner._review_feedback),
        }


class UpdateFeedbackTool(MCPTool):
    """Tool for updating review feedback items (probability, duplicates, etc)."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize update feedback tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="update_feedback",
            description="Update review feedback items: assign probability/confidence scores, mark duplicates, or both. Use this after analyzing feedback to indicate confidence levels and identify duplicate issues.",
            parameters={
                "type": "object",
                "properties": {
                    "feedback_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of feedback item indices to update (0-indexed positions in the feedback list)",
                    },
                    "probability": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Optional: Probability/confidence score (0.0 to 1.0) to assign to these items",
                    },
                    "probability_reasoning": {
                        "type": "string",
                        "description": "Optional: Explanation of why this probability was assigned",
                    },
                    "duplicate_of": {
                        "type": "integer",
                        "description": "Optional: Index of the primary/best feedback item that these are duplicates of (0-indexed)",
                    },
                    "duplicate_reasoning": {
                        "type": "string",
                        "description": "Optional: Explanation of why these items are duplicates",
                    },
                },
                "required": ["feedback_indices"],
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Update feedback items."""
        indices = arguments["feedback_indices"]
        probability = arguments.get("probability")
        probability_reasoning = arguments.get("probability_reasoning", "")
        duplicate_of = arguments.get("duplicate_of")
        duplicate_reasoning = arguments.get("duplicate_reasoning", "")

        updated_count = 0
        errors = []

        # Validate duplicate_of index if provided
        if duplicate_of is not None:
            if duplicate_of < 0 or duplicate_of >= len(self.runner._review_feedback):
                return {
                    "success": False,
                    "error": f"duplicate_of index {duplicate_of} out of range (0-{len(self.runner._review_feedback)-1})",
                }

        # Update items
        for idx in indices:
            if idx < 0 or idx >= len(self.runner._review_feedback):
                errors.append(f"Index {idx} out of range (0-{len(self.runner._review_feedback)-1})")
                continue

            # Check if marking as duplicate of itself
            if duplicate_of is not None and idx == duplicate_of:
                errors.append(f"Index {idx} cannot be marked as duplicate of itself")
                continue

            item = self.runner._review_feedback[idx]

            # Update probability if provided
            if probability is not None:
                item.probability = probability
                item.probability_reasoning = probability_reasoning

            # Mark as duplicate if provided
            if duplicate_of is not None:
                item.duplicate_of = duplicate_of
                item.duplicate_reasoning = duplicate_reasoning

            updated_count += 1

        # Build message
        message_parts = []
        if probability is not None:
            message_parts.append(f"probability={probability:.2f}")
        if duplicate_of is not None:
            message_parts.append(f"duplicate_of={duplicate_of}")
        message = f"Updated {updated_count} feedback item(s)" + (f" ({', '.join(message_parts)})" if message_parts else "")

        return {
            "success": len(errors) == 0,
            "updated_count": updated_count,
            "errors": errors if errors else None,
            "message": message,
        }


class PRReviewMCPServer(MCPServer):
    """MCP Server for code review with all built-in tools and review tools."""

    def __init__(self, runner, review_target: "ReviewTarget"):
        """
        Initialize code review MCP server.

        Args:
            runner: SandboxRunner instance
            review_target: ReviewTarget instance (provides diff/commits data)
        """
        super().__init__()
        # Add all built-in tools
        self.add_tool(ExecuteCommandTool(runner))
        self.add_tool(ReadFileTool(runner))
        self.add_tool(GlobTool(runner))
        self.add_tool(GrepTool(runner))
        self.add_tool(SpawnAgentTool(runner, inheritable=False))
        self.add_tool(WaitForAgentsTool(runner))
        # Add review specific tools
        self.add_tool(RecordReviewFeedbackTool(runner))
        self.add_tool(GetReviewFeedbackTool(runner))
        self.add_tool(UpdateFeedbackTool(runner))
        # Add review data tools (work with any ReviewTarget)
        self.add_tool(GetReviewDiffTool(review_target))
        self.add_tool(GetReviewCommitsTool(review_target))


class PRReviewSubcommand(Subcommand):
    """Code review with instruction-based criteria using multi-agent workflow."""

    name = "pr-review"
    help = "Code review: orchestrator agent reads docs, spawns review agents, applies review instructions"

    def add_arguments(self, command):
        """Add custom arguments."""
        command.params.append(
            click.Option(
                ["--pr"],
                type=int,
                help="GitHub PR number to review",
            )
        )
        command.params.append(
            click.Option(
                ["--base"],
                type=str,
                help="Base commit/branch for local review (requires --head)",
            )
        )
        command.params.append(
            click.Option(
                ["--head"],
                type=str,
                help="Head commit/branch for local review (requires --base)",
            )
        )
        command.params.append(
            click.Option(
                ["--with-token"],
                type=str,
                help="GitHub token (defaults to GH_TOKEN environment variable)",
            )
        )
        command.params.append(
            click.Option(
                ["--load-review"],
                type=str,
                help="Load review from file ID or path instead of generating new review",
            )
        )
        command.params.append(
            click.Option(
                ["--review-id"],
                type=str,
                help="Use specified ID for review file (auto-generated if not provided)",
            )
        )
        return command

    def execute(self, project_dir: Path, runner, **kwargs):
        """Execute multi-agent code review workflow."""
        pr_number = kwargs.get("pr")
        base_commit = kwargs.get("base")
        head_commit = kwargs.get("head")
        token = kwargs.get("with_token") or os.getenv("GH_TOKEN")
        network = kwargs["network"]
        verbose = kwargs["verbose"]
        load_review = kwargs.get("load_review")
        review_id = kwargs.get("review_id")

        # Validate arguments
        if pr_number and (base_commit or head_commit):
            click.echo("Error: Cannot use --pr with --base/--head. Choose one mode.", err=True)
            sys.exit(1)

        if not pr_number and not (base_commit and head_commit):
            click.echo("Error: Either --pr OR both --base and --head must be provided.", err=True)
            sys.exit(1)

        if (base_commit and not head_commit) or (head_commit and not base_commit):
            click.echo("Error: Both --base and --head must be provided together.", err=True)
            sys.exit(1)

        # Create appropriate ReviewTarget
        if pr_number is not None:
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
            review_target = GitHubPRTarget(pr_number, token, project_dir)
        else:
            review_target = LocalReviewTarget(base_commit, head_commit)

        click.echo(f"\n{'='*60}")
        click.echo(f"Multi-Agent Code Review: {review_target.get_description()}")
        click.echo(f"{'='*60}\n")

        # Branch 1: Load from existing file
        if load_review:
            review = self._load_review_from_file(load_review, project_dir)
            # Use --load-review value as review_id if no --review-id was specified
            if not review_id:
                review_id = load_review
        # Branch 2: Generate fresh review with LLM
        else:
            # Fetch remote data if needed (PR mode)
            review_target.fetch_if_needed(project_dir)

            review = self._run_review_workflow(runner, review_target, network, verbose)

        # Filter and display feedback
        sorted_feedback = review.filter_feedback(probability_threshold=0.5)
        self._display_feedback_statistics(review, sorted_feedback)

        if not sorted_feedback:
            click.echo("\nNo high-confidence suggestions. All files look good!")
            return

        # Generate summary if not already present
        if not review.summary:
            review.summary = self._build_summary_text(review_target, review, sorted_feedback)

        # Edit feedback interactively
        edited_review = self._edit_feedback_interactive(review, sorted_feedback, project_dir, review_id)

        if edited_review is None:
            click.echo("\nReview cancelled. No review will be posted.")
            return

        if len(edited_review.feedback) == 0:
            click.echo("\nNo suggestions remaining after editing. No review will be posted.")
            return

        # Publish review if target supports it
        if review_target.can_publish():
            review_target.publish_review(edited_review)
        else:
            click.echo(f"\n{'='*60}")
            click.echo("Review Complete (Local Mode)")
            click.echo(f"{'='*60}")
            click.echo(f"\nReview completed for {review_target.get_description()}")
            click.echo(f"Total suggestions: {len(edited_review.feedback)}")
            click.echo(f"\nReview saved to file (use --load-review to reload)")

    def _load_review_from_file(self, load_review: str, project_dir: Path) -> Review:
        """Load review feedback from existing file."""
        click.echo(f"\n{'='*60}")
        click.echo(f"Loading Review from File/ID")
        click.echo(f"{'='*60}\n")
        click.echo(f"Loading from: {load_review}")

        result = ReviewFeedbackEditor.load_feedback(load_review, project_dir)

        if result is None:
            click.echo(f"Error: Could not load review from '{load_review}'", err=True)
            click.echo("Make sure the file exists or the ID is correct.", err=True)
            sys.exit(1)

        summary, all_feedback = result
        click.echo(f"Loaded {len(all_feedback)} suggestions")
        if summary:
            click.echo(f"Loaded review summary")

        return Review(summary=summary, feedback=all_feedback)

    def _build_agent_prompt(self, review_target: str, base_ref: str, head_ref: str) -> str:
        """Build the agent prompt for code review."""
        return f"""You are the orchestrator agent for code review: {review_target}

Review Target:
- Base: {base_ref}
- Head: {head_ref}

Worktrees already checked out for you:
- 'review-head': Contains the changes being reviewed (head: {head_ref})
- 'review-base': Contains the base for comparison (base: {base_ref})

**Agent assumptions (applies to all agents and subagents):**
- All tools are functional and will work without error. Do not test tools or make exploratory calls.
- Only call a tool if it is required to complete the task. Every tool call should have a clear purpose.
- Make sure these assumptions are clear to every subagent that is launched.

**CRITICAL: We only want HIGH SIGNAL issues.** Flag issues where:
- The code will fail to compile or parse (syntax errors, type errors, missing imports, unresolved references)
- The code will definitely produce wrong results regardless of inputs (clear logic errors)
- Clear, unambiguous violations of project review guidelines where you can quote the exact rule being broken
- Critical security vulnerabilities (SQL injection, XSS, hardcoded secrets, insecure crypto)

Do NOT flag:
- Code style or quality concerns (unless explicitly required in review guidelines)
- Potential issues that depend on specific inputs or state
- Subjective suggestions or improvements
- Issues that a linter will catch (assume linters are run separately)
- General code quality concerns (e.g., lack of test coverage) unless explicitly required in review guidelines
- Pre-existing issues (only review the changed code)
- Pedantic nitpicks that a senior engineer would not flag

**If you are not certain an issue is real, do not flag it.** False positives erode trust and waste reviewer time.

Your workflow:

1. Read project documentation and review instructions:
   - Read AGENTS.md and CLAUDE.md from review-head worktree (if available)
   - Find review instructions (either via AGENTS.md, or by searching for them)
   - Understand all review criteria from all instruction files
   - When evaluating compliance for a file, only consider review guidelines that apply to that file's path
   - Identify changes using git commands or get_review_diff/get_review_commits tools

2. Understand the changes:
   - Use git diff or get_review_diff to see what changed
   - Use git log or get_review_commits to understand the commits
   - Focus your review on the diff itself - the actual lines that changed

3. Spawn sub-agents for specific review tasks:
   Break down the review into 2-4 parallel tasks based on the changes:

   a) If review guidelines exist: Spawn 1-2 agents for guideline compliance
      - Each independently audits changes for compliance with project review guidelines
      - Only flag clear violations where you can quote the exact rule
      - Consider only guidelines that apply to the changed files

   b) Spawn 1-2 agents for bug detection:
      - Security issues (SQL injection, XSS, hardcoded credentials, etc.)
      - Logic errors that will produce wrong results
      - Syntax errors, type errors, missing imports
      - Missing error handling for critical operations
      - Focus on what's visible in the diff

   For each sub-agent:
   - Use spawn_agent MCP tool
   - Do not let them create new sub-agents (inheritable=False is already set)
   - Give them clear instructions to use record_review_feedback to record findings
   - Provide the relevant review criteria
   - Remind them to focus on HIGH SIGNAL issues only
   - Tell them to self-validate their findings before recording them

4. Wait for sub-agents to complete:
   - Use wait_for_agents MCP tool to wait for all spawned agents
   - Sub-agents will record their findings using the record_review_feedback MCP tool

5. Review and assign probabilities:
   - Use get_review_feedback MCP tool to retrieve all recorded findings
   - Review each finding carefully - sub-agents should have self-validated
   - Use update_feedback MCP tool to:
     * Assign confidence scores (0.0-1.0) with probability_reasoning
       - Clear, validated issues: 0.8-1.0
       - Uncertain but worth flagging: 0.5-0.7
       - Likely false positives: 0.0-0.4
     * Mark duplicate findings (multiple sub-agents may identify the same issue)
       - Look for findings on the same file/line range
       - Look for findings describing the same problem
       - Choose the best/most detailed one, mark others with duplicate_of
       - Provide duplicate_reasoning

7. Return a summary:
   - Summarize the review process and approach taken
   - Report how many sub-agents were spawned and what tasks they performed
   - Report total findings, duplicates identified, and confidence distribution
   - Provide overall assessment of the code quality
   - DO NOT include the detailed findings in output (they're in the feedback store)

The structured output should just be a high-level summary - the detailed findings are accessible via the get_review_feedback MCP tool."""

    def _build_agent_schema(self) -> dict:
        """Build the output schema for agent."""
        return {
            "type": "object",
            "properties": {
                "review_summary": {
                    "type": "string",
                    "description": "High-level summary of the review process and approach taken",
                },
                "documentation_found": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of documentation files found (AGENTS.md, CLAUDE.md, review instructions)",
                },
                "review_criteria_summary": {
                    "type": "string",
                    "description": "Summary of review criteria applied from instruction files or general best practices",
                },
                "sub_agents_spawned": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "task_description": {"type": "string"},
                        },
                        "required": ["agent_id", "task_description"],
                    },
                    "description": "List of sub-agents that were spawned and their tasks",
                },
                "findings_statistics": {
                    "type": "object",
                    "properties": {
                        "total_findings": {"type": "integer"},
                        "duplicates_count": {
                            "type": "integer",
                            "description": "Number of duplicate findings identified and marked",
                        },
                        "unique_findings": {
                            "type": "integer",
                            "description": "Number of unique findings after removing duplicates",
                        },
                        "by_category": {
                            "type": "object",
                            "description": "Count of findings by category (bug, security, etc.)",
                        },
                        "by_severity": {
                            "type": "object",
                            "description": "Count of findings by severity (critical, high, medium, low)",
                        },
                        "high_confidence_count": {
                            "type": "integer",
                            "description": "Number of findings with probability >= 0.8",
                        },
                    },
                    "required": ["total_findings"],
                    "description": "Statistics about findings (detailed findings available via get_review_feedback)",
                },
                "overall_assessment": {
                    "type": "string",
                    "description": "Overall assessment of the PR quality and key takeaways",
                },
            },
            "required": ["review_summary", "documentation_found", "sub_agents_spawned", "findings_statistics", "overall_assessment"],
        }

    def _run_review_workflow(self, runner, review_target: ReviewTarget, network: str, verbose: bool) -> Review:
        """Run the full review workflow with LLM agent."""
        click.echo("\nStarting review agent...")

        base_ref = review_target.get_base_ref()
        head_ref = review_target.get_head_ref()
        agent_prompt = self._build_agent_prompt(review_target.get_description(), base_ref, head_ref)
        agent_schema = self._build_agent_schema()

        # Run agent
        result, all_feedback = asyncio.run(self._execute_async(
            runner, review_target, base_ref, head_ref,
            agent_prompt, agent_schema, network, verbose
        ))

        # Display results
        self._display_agent_results(result)

        # Convert result dict to ReviewMetadata
        metadata = ReviewMetadata.from_dict(result)

        return Review(
            summary=None,  # Will be generated later
            feedback=all_feedback,
            metadata=metadata
        )

    def _display_agent_results(self, result: dict):
        """Display the agent's review results."""
        click.echo(f"\n{'='*60}")
        click.echo("Review Agent Results")
        click.echo(f"{'='*60}")
        click.echo(f"\nReview Summary:")
        click.echo(result["review_summary"])

        click.echo(f"\nDocumentation Found: {len(result['documentation_found'])}")
        if result["documentation_found"]:
            for file in result["documentation_found"]:
                click.echo(f"  - {file}")

        click.echo(f"\nReview Criteria:")
        click.echo(result["review_criteria_summary"])

        click.echo(f"\nSub-Agents Spawned: {len(result['sub_agents_spawned'])}")
        for agent in result["sub_agents_spawned"]:
            click.echo(f"  - {agent['agent_id']}: {agent['task_description']}")

        click.echo(f"\nFindings Statistics:")
        stats = result["findings_statistics"]
        click.echo(f"  Total findings: {stats['total_findings']}")
        if "duplicates_count" in stats:
            click.echo(f"  Duplicates marked: {stats['duplicates_count']}")
        if "unique_findings" in stats:
            click.echo(f"  Unique findings: {stats['unique_findings']}")
        if "by_category" in stats:
            click.echo(f"  By category: {stats['by_category']}")
        if "by_severity" in stats:
            click.echo(f"  By severity: {stats['by_severity']}")
        if "high_confidence_count" in stats:
            click.echo(f"  High confidence (≥0.8): {stats['high_confidence_count']}")

        click.echo(f"\nOverall Assessment:")
        click.echo(result["overall_assessment"])

    def _display_feedback_statistics(self, review: Review, filtered_feedback: List["FeedbackItem"]):
        """Display feedback filtering statistics."""
        stats = review.get_statistics()
        probability_threshold = 0.5

        click.echo(f"\n{'='*60}")
        click.echo(f"Filtering Feedback")
        click.echo(f"{'='*60}")
        click.echo(f"Total findings recorded: {stats['total']}")
        click.echo(f"Duplicates marked: {stats['duplicates']}")
        click.echo(f"Unique findings: {stats['unique']}")
        click.echo(f"After filtering (probability ≥ {probability_threshold}, excluding duplicates): {len(filtered_feedback)}")

        click.echo(f"\n{'='*60}")
        click.echo(f"Review Complete - {len(filtered_feedback)} High-Confidence Suggestions")
        click.echo(f"{'='*60}")

    def _edit_feedback_interactive(self, review: Review, sorted_feedback: List["FeedbackItem"], project_dir: Path, review_id: Optional[str]) -> Optional[Review]:
        """Open editor for user to review and modify feedback."""
        click.echo(f"\n{'='*60}")
        click.echo("Opening editor for review suggestions")
        click.echo(f"{'='*60}\n")
        click.echo("You can now edit the suggestions in your editor (YAML format).")
        click.echo("To remove suggestions:")
        click.echo("  - Delete entire YAML documents (between --- separators)")
        click.echo("  - Delete index lines (e.g., '#   3. file.py:10-20 ...')")
        click.echo("  - Add 'ignore: true' to a document")
        click.echo("To modify suggestions:")
        click.echo("  - Edit any field in the YAML document")
        click.echo("  - Edit the review summary in the first YAML document")
        click.echo("- Save and close when done\n")

        result = ReviewFeedbackEditor.edit_feedback(sorted_feedback, project_dir, feedback_id=review_id, summary=review.summary)

        if result is None:
            return None

        final_summary, accepted_suggestions = result
        return Review(summary=final_summary, feedback=accepted_suggestions, metadata=review.metadata)

    def _build_summary_text(self, review_target: ReviewTarget, review: Review, sorted_feedback: List["FeedbackItem"]) -> str:
        """Build the summary text for the review."""
        stats = review.get_statistics()
        probability_threshold = 0.5

        if review.metadata:
            # Generated with LLM - include detailed summary
            meta = review.metadata
            doc_list = ", ".join(meta.documentation_found) if meta.documentation_found else "None"
            agent_list = "\n".join([f"  - {a.agent_id}: {a.task_description}" for a in meta.sub_agents_spawned])

            return f"""Code review completed for {review_target.get_description()}.

Review approach:
{meta.review_summary[:300]}{"..." if len(meta.review_summary) > 300 else ""}

Documentation found: {doc_list}

Review criteria:
{meta.review_criteria_summary[:250]}{"..." if len(meta.review_criteria_summary) > 250 else ""}

Sub-agents used:
{agent_list}

Findings:
- Total findings: {meta.findings_statistics.total_findings}
- Duplicates marked: {meta.findings_statistics.duplicates_count or 0}
- Unique findings: {meta.findings_statistics.unique_findings or meta.findings_statistics.total_findings}
- High confidence (≥0.8): {meta.findings_statistics.high_confidence_count or 'N/A'}
- After filtering (≥{probability_threshold}, excluding duplicates): {len(sorted_feedback)}

Overall assessment:
{meta.overall_assessment[:300]}{"..." if len(meta.overall_assessment) > 300 else ""}"""
        else:
            # Loaded from file - simple summary
            return f"""Code review completed for {review_target.get_description()}.

Review loaded from saved file."""

    async def _execute_async(
        self,
        runner,
        review_target: ReviewTarget,
        base_ref,
        head_ref,
        agent_prompt,
        agent_schema,
        network,
        verbose
    ):
        """Async execution of code review workflow."""
        async with runner:
            # Setup the sandbox environment
            await runner.setup(network=network)

            # Create checkout tool instance
            checkout_tool = CheckoutCommitTool(runner)

            # Pre-checkout worktrees for head and base
            click.echo("\nChecking out worktrees...")
            click.echo(f"  Creating worktree 'review-head' from {head_ref}...")
            head_result = await checkout_tool.execute({
                "commit": head_ref,
                "worktree_name": "review-head",
            })
            if not head_result["success"]:
                click.echo(f"Error: {head_result['error']}", err=True)
                sys.exit(1)

            click.echo(f"  Creating worktree 'review-base' from {base_ref}...")
            base_result = await checkout_tool.execute({
                "commit": base_ref,
                "worktree_name": "review-base",
            })
            if not base_result["success"]:
                click.echo(f"Error: {base_result['error']}", err=True)
                sys.exit(1)

            click.echo("  Worktrees created successfully!")

            # Create MCP server with all built-in tools + review tools
            mcp_server = PRReviewMCPServer(runner, review_target)

            # Create agent config and run
            agent = AgentConfig(
                prompt=agent_prompt,
                output_schema=agent_schema,
                mcp_server=mcp_server,
            )
            results = await runner.run_agents([agent], verbose=verbose)

            # Get feedback BEFORE exiting context manager (before cleanup clears it)
            all_feedback = list(runner._review_feedback)

            return results[0], all_feedback
