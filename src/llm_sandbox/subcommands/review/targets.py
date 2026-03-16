"""Review target abstractions for different review sources (local, GitHub PR, etc)."""

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from llm_sandbox.git_ops import GitOperations
from .models import Review, FeedbackItem


@dataclass
class PublishResult:
    """Result of publishing a review."""
    summary_posted: bool
    inline_comments_posted: int
    inline_comments_failed: int
    failed_suggestions: List[FeedbackItem]


@dataclass
class SampleInlineComment:
    """Sample inline comment for preview."""
    file: str
    line_start: int
    line_end: int
    category: str
    body_preview: List[str]  # First few lines of the comment
    total_lines: int  # Total lines in comment


@dataclass
class PublishPreview:
    """Preview of what will be published."""
    target_description: str  # e.g., "GitHub PR #123"
    repository: Optional[str]  # e.g., "owner/repo"
    pr_url: Optional[str]
    will_post_summary: bool
    will_post_inline_count: int
    summary_body: Optional[str]
    sample_inline_comments: List[SampleInlineComment]


@dataclass
class PublishSuccess:
    """Success information after publishing."""
    pr_url: Optional[str]
    repository: Optional[str]


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
    def fetch_if_needed(self) -> None:
        """Fetch remote refs if needed (e.g., PR from GitHub).

        Raises:
            RuntimeError: If fetching fails

        Uses self.project_dir for repository operations.
        """
        pass

    @abstractmethod
    def get_diff(self) -> str:
        """Get the full diff for the review.

        Returns:
            Unified diff format string showing all changes

        Raises:
            RuntimeError: If diff cannot be obtained
        """
        pass

    @abstractmethod
    def get_commits(self) -> List[Dict[str, Any]]:
        """Get the list of commits in the review.

        Returns:
            List of commit dictionaries with metadata (sha, message, author, etc.)

        Raises:
            RuntimeError: If commits cannot be obtained
        """
        pass

    @abstractmethod
    def can_publish(self) -> bool:
        """Whether this target supports publishing reviews."""
        pass

    @abstractmethod
    def get_publish_preview(self, review: Review) -> PublishPreview:
        """Get preview of what will be published.

        Args:
            review: Review object to preview

        Returns:
            PublishPreview with details of what will be posted

        Raises:
            NotImplementedError: If publishing not supported
        """
        pass

    @abstractmethod
    def get_publish_success(self) -> PublishSuccess:
        """Get success information after publishing.

        Returns:
            PublishSuccess with details about the published review

        Raises:
            NotImplementedError: If publishing not supported
        """
        pass

    @abstractmethod
    def publish_review(self, review: Review, pr_info: Optional[Dict] = None) -> PublishResult:
        """Publish the review to the target (e.g., post to GitHub).

        Args:
            review: Review object to publish
            pr_info: Optional PR info (deprecated, not used)

        Returns:
            PublishResult with details of what was posted

        Raises:
            RuntimeError: If publishing fails
        """
        pass

    @abstractmethod
    def _to_info_impl(self) -> Dict[str, Any]:
        """Serialize target-specific info (without 'type' key). Implemented by subclasses."""
        pass

    def to_info(self) -> Dict[str, Any]:
        """Serialize target to dict for storage. Automatically includes 'type' key."""
        info = self._to_info_impl()

        # Determine type string based on class
        if isinstance(self, LocalReviewTarget):
            info['type'] = 'local'
        elif isinstance(self, GitHubPRTarget):
            info['type'] = 'github_pr'
        else:
            raise NotImplementedError(f"Unknown target type: {type(self).__name__}")

        return info

    @classmethod
    def from_info(cls, info: Dict[str, Any], **kwargs) -> "ReviewTarget":
        """Deserialize target from info dict. Factory method that dispatches to subclasses.

        Args:
            info: Serialized target info (must include 'type' key)
            **kwargs: Additional runtime arguments needed by specific targets (e.g., token, project_dir)

        Returns:
            Appropriate ReviewTarget subclass instance
        """
        target_type = info.get("type")
        if target_type == "local":
            return LocalReviewTarget.from_info(info, **kwargs)
        elif target_type == "github_pr":
            return GitHubPRTarget.from_info(info, **kwargs)
        else:
            raise ValueError(f"Unknown target type: {target_type}")


class LocalReviewTarget(ReviewTarget):
    """Review target for local commits/branches."""

    def __init__(self, base_ref: str, head_ref: str, project_dir: Path):
        self.base_ref = base_ref
        self.head_ref = head_ref
        self.project_dir = project_dir
        self.git_ops = GitOperations(project_dir)

    def get_base_ref(self) -> str:
        return self.base_ref

    def get_head_ref(self) -> str:
        return self.head_ref

    def get_description(self) -> str:
        return f"{self.base_ref}..{self.head_ref}"

    def fetch_if_needed(self) -> None:
        # Local refs, nothing to fetch
        pass

    def get_diff(self) -> str:
        """Get the full diff using GitPython."""
        return self.git_ops.get_diff(self.base_ref, self.head_ref)

    def get_commits(self) -> List[Dict[str, Any]]:
        """Get the list of commits using GitPython."""
        # Get commit objects from GitOperations
        commit_objects = self.git_ops.get_commits(self.base_ref, self.head_ref)

        # Translate to dict format
        commits = []
        for commit in commit_objects:
            commits.append({
                "sha": commit.hexsha,
                "short_sha": commit.hexsha[:7],
                "message": commit.message.strip(),
                "author": commit.author.name,
                "author_email": commit.author.email,
                "date": commit.authored_datetime.isoformat(),
                "committer": commit.committer.name,
            })

        return commits

    def can_publish(self) -> bool:
        return False

    def get_publish_preview(self, review: Review) -> PublishPreview:
        """Local reviews cannot be published."""
        raise NotImplementedError("Cannot publish local reviews to remote")

    def get_publish_success(self) -> PublishSuccess:
        """Local reviews cannot be published."""
        raise NotImplementedError("Cannot publish local reviews to remote")

    def publish_review(self, review: Review, pr_info: Optional[Dict] = None) -> PublishResult:
        raise NotImplementedError("Cannot publish local reviews to remote")

    def _to_info_impl(self) -> Dict[str, Any]:
        """Serialize to dict. Includes base_ref/head_ref for completeness."""
        return {
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
        }

    @classmethod
    def from_info(cls, info: Dict[str, Any], project_dir: Path = None, **kwargs) -> "LocalReviewTarget":
        """Reconstruct LocalReviewTarget from info dict.

        Args:
            info: Serialized target info
            project_dir: Project directory (required)

        Returns:
            LocalReviewTarget instance
        """
        if project_dir is None:
            raise ValueError("project_dir is required to create LocalReviewTarget")

        return cls(
            base_ref=info["base_ref"],
            head_ref=info["head_ref"],
            project_dir=project_dir,
        )


class GitHubPRTarget(ReviewTarget):
    """Review target for GitHub Pull Requests."""

    def __init__(self, pr_number: int, token: str, project_dir: Path):
        self.pr_number = pr_number
        self.token = token
        self.project_dir = project_dir
        self.git_ops = GitOperations(project_dir)
        self.github_client = None
        self.repo_name = None
        self.pr_info = None
        self.pr_head_branch = None

    def get_description(self) -> str:
        return f"PR #{self.pr_number}"

    def fetch_if_needed(self) -> None:
        """Fetch PR information and head branch from GitHub."""
        self.github_client = GitHubClient(self.token)

        # Get repository name
        try:
            self.repo_name = self._get_repo_name(self.project_dir)
        except Exception as e:
            raise RuntimeError(f"Error getting repository: {e}") from e

        # Fetch PR info
        try:
            self.pr_info = self.github_client.get_pull_request(self.repo_name, self.pr_number)
        except Exception as e:
            raise RuntimeError(f"Error fetching PR info: {e}") from e

        # Fetch PR head
        self.pr_head_branch = f"fetch/pr-{self.pr_number}/{self.pr_info['head_ref']}"

        try:
            refspec = f"pull/{self.pr_number}/head:{self.pr_head_branch}"
            self.git_ops.fetch_ref("origin", refspec)
        except RuntimeError as e:
            raise RuntimeError(f"Error fetching PR head: {e}") from e

    def get_base_ref(self) -> str:
        if not self.pr_info:
            raise RuntimeError("Must call fetch_if_needed() first")
        return self.pr_info['base_ref']

    def get_head_ref(self) -> str:
        if not self.pr_head_branch:
            raise RuntimeError("Must call fetch_if_needed() first")
        return self.pr_head_branch

    def get_diff(self) -> str:
        """Get the full PR diff from GitHub API."""
        if not self.github_client or not self.repo_name:
            raise RuntimeError("Must call fetch_if_needed() first")

        try:
            return self.github_client.get_pull_request_diff(self.repo_name, self.pr_number)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch PR diff from GitHub: {e}")

    def get_commits(self) -> List[Dict[str, Any]]:
        """Get the list of commits from GitHub API."""
        if not self.github_client or not self.repo_name:
            raise RuntimeError("Must call fetch_if_needed() first")

        try:
            return self.github_client.get_pull_request_commits(self.repo_name, self.pr_number)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch PR commits from GitHub: {e}")

    def can_publish(self) -> bool:
        return True

    def get_publish_preview(self, review: Review) -> PublishPreview:
        """Get preview of what will be posted to GitHub."""
        if not self.github_client or not self.pr_info:
            raise RuntimeError("Must call fetch_if_needed() first")

        pr_url = f"https://github.com/{self.repo_name}/pull/{self.pr_number}"
        summary_body = None
        if review.summary:
            summary_body = self._format_summary_comment(len(review.feedback), review.summary)

        # Prepare sample inline comments
        sample_comments = []
        active_feedback = review.get_active_feedback()
        for suggestion in active_feedback[:3]:
            comment_body = self._format_inline_comment(suggestion)
            body_lines = comment_body.split('\n')
            sample_comments.append(SampleInlineComment(
                file=suggestion.file,
                line_start=suggestion.line_start,
                line_end=suggestion.line_end,
                category=suggestion.category,
                body_preview=body_lines[:2],
                total_lines=len(body_lines)
            ))

        return PublishPreview(
            target_description=f"GitHub PR #{self.pr_number}",
            repository=self.repo_name,
            pr_url=pr_url,
            will_post_summary=summary_body is not None,
            will_post_inline_count=len(review.feedback),
            summary_body=summary_body,
            sample_inline_comments=sample_comments
        )

    def get_publish_success(self) -> PublishSuccess:
        """Get success information after publishing."""
        pr_url = f"https://github.com/{self.repo_name}/pull/{self.pr_number}"
        return PublishSuccess(
            pr_url=pr_url,
            repository=self.repo_name
        )

    def publish_review(self, review: Review, pr_info: Optional[Dict] = None) -> PublishResult:
        """Post review summary and inline comments to GitHub."""
        if not self.github_client or not self.pr_info:
            raise RuntimeError("Must call fetch_if_needed() first")

        # Format summary for GitHub
        summary_body = None
        summary_posted = False
        if review.summary:
            summary_body = self._format_summary_comment(len(review.feedback), review.summary)

        # Post summary
        if summary_body:
            try:
                self.github_client.post_issue_comment(self.repo_name, self.pr_number, summary_body)
                summary_posted = True
            except Exception:
                pass  # Continue with inline comments even if summary fails

        # Post inline comments
        success_count = 0
        failed_suggestions = []

        active_feedback = review.get_active_feedback()
        for suggestion in active_feedback:
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
                success_count += 1
            except Exception:
                failed_suggestions.append(suggestion)

        return PublishResult(
            summary_posted=summary_posted,
            inline_comments_posted=success_count,
            inline_comments_failed=len(failed_suggestions),
            failed_suggestions=failed_suggestions
        )

    def _get_repo_name(self, project_dir: Path) -> str:
        """Get GitHub repository owner/name from git remote."""
        try:
            remote_url = self.git_ops.get_remote_url("origin")

            # Parse GitHub URL
            if "github.com" in remote_url:
                match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(\.git)?$', remote_url)
                if match:
                    return match.group(1)

            raise ValueError(f"Could not parse GitHub repository from: {remote_url}")
        except RuntimeError as e:
            raise RuntimeError(f"Failed to get git remote: {e}")
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

    def _to_info_impl(self) -> Dict[str, Any]:
        """Serialize to dict with PR number and repo name."""
        return {
            "pr_number": self.pr_number,
            "repo_name": self.repo_name,
        }

    @classmethod
    def from_info(cls, info: Dict[str, Any], token: str = None, project_dir: Path = None, **kwargs) -> "GitHubPRTarget":
        """Reconstruct GitHubPRTarget from info dict.

        Args:
            info: Serialized target info
            token: GitHub API token (required)
            project_dir: Project directory (required)

        Returns:
            GitHubPRTarget instance
        """
        if token is None:
            raise ValueError("token is required to create GitHubPRTarget")
        if project_dir is None:
            raise ValueError("project_dir is required to create GitHubPRTarget")

        target = cls(
            pr_number=info["pr_number"],
            token=token,
            project_dir=project_dir,
        )
        # Set repo_name directly (normally set by fetch_if_needed)
        target.repo_name = info["repo_name"]
        return target

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
