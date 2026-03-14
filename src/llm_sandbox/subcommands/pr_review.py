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
import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click
import requests

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

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
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

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
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
        feedback_item = {
            "file": arguments["file"],
            "line_start": arguments["line_start"],
            "line_end": arguments["line_end"],
            "current_code": arguments.get("current_code", ""),
            "suggested_code": arguments.get("suggested_code", ""),
            "reason": arguments["reason"],
            "category": arguments["category"],
            "severity": arguments.get("severity", "medium"),
        }

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
            feedback = [f for f in feedback if f["file"] == file_filter]

        if category_filter:
            feedback = [f for f in feedback if f["category"] == category_filter]

        if severity_filter:
            feedback = [f for f in feedback if f["severity"] == severity_filter]

        return {
            "success": True,
            "feedback": feedback,
            "count": len(feedback),
            "total_recorded": len(self.runner._review_feedback),
        }


class AssignFeedbackProbabilityTool(MCPTool):
    """Tool for assigning probability/confidence to review feedback items."""

    def __init__(self, runner: "SandboxRunner"):
        """
        Initialize assign feedback probability tool.

        Args:
            runner: SandboxRunner instance
        """
        super().__init__(
            name="assign_feedback_probability",
            description="Assign a probability/confidence score to review feedback items based on your analysis. Use this after sub-agents have recorded their findings to indicate how confident you are that each finding is valid.",
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
                        "description": "Probability/confidence score (0.0 to 1.0) for these feedback items",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Explanation of why this probability was assigned",
                    },
                },
                "required": ["feedback_indices", "probability"],
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Assign probability to feedback items."""
        indices = arguments["feedback_indices"]
        probability = arguments["probability"]
        reasoning = arguments.get("reasoning", "")

        updated_count = 0
        errors = []

        for idx in indices:
            if idx < 0 or idx >= len(self.runner._review_feedback):
                errors.append(f"Index {idx} out of range (0-{len(self.runner._review_feedback)-1})")
                continue

            self.runner._review_feedback[idx]["probability"] = probability
            self.runner._review_feedback[idx]["probability_reasoning"] = reasoning
            updated_count += 1

        return {
            "success": len(errors) == 0,
            "updated_count": updated_count,
            "errors": errors if errors else None,
            "message": f"Updated probability for {updated_count} feedback item(s) to {probability:.2f}",
        }


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
        self.add_tool(ReadFileTool(runner))
        self.add_tool(GlobTool(runner))
        self.add_tool(GrepTool(runner))
        self.add_tool(SpawnAgentTool(runner, inheritable=False))
        self.add_tool(WaitForAgentsTool(runner))
        # Add PR review specific tools
        self.add_tool(RecordReviewFeedbackTool(runner))
        self.add_tool(GetReviewFeedbackTool(runner))
        self.add_tool(AssignFeedbackProbabilityTool(runner))
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

        agent_prompt = f"""You are the orchestrator agent for PR #{pr_number} review.

PR Information:
- Title: {pr_info['title']}
- Head branch: {pr_info['head_ref']}
- Base branch: {pr_info['base_ref']}
- Author: {pr_info['author']}

Worktrees already checked out for you:
- 'pr-head': Contains the PR changes (head: {pr_info['head_ref']})
- 'pr-base': Contains the base branch (base: {pr_info['base_ref']})

Your workflow:

1. Read project documentation and review instructions:
   - Read AGENTS.md and CLAUDE.md from pr-head worktree (if available)
   - Look for review instruction files as specified in those docs
   - If not specified, search common patterns: review/, docs/review/, .github/review/
   - Understand all review criteria from all instruction files
   - Get PR changes summary using the MCP tools get_pull_request_commits and get_pull_request_diff

2. Spawn sub-agents for specific review tasks:
   - Break down the review into specific tasks based on:
     * Review instruction categories (if found)
     * Common areas: security, performance, bugs, style, documentation
     * Changed file types or modules
   - Use the spawn_agent MCP tool to create a sub-agent for each task
     * Do not let them create new sub-agents
   - Give each sub-agent:
     * The list of tools available to them
     * Instructions to use record_review_feedback MCP tools to record findings
     * A clear, specific task description
     * The relevant review criteria for that task
   - Example tasks: "Review security aspects", "Review database changes", "Review API endpoints"

3. Wait for sub-agents to complete:
   - Use wait_for_agents MCP tool to wait for all spawned agents
   - Sub-agents will record their findings using the record_review_feedback MCP tool

4. Review and assign probabilities to findings:
   - Use get_review_feedback MCP tool to retrieve all recorded findings
   - Analyze each finding for validity and accuracy
   - Use assign_feedback_probability MCP tool to assign confidence scores (0.0-1.0)
   - Consider: Is the issue real? Is the suggested fix appropriate? Does it align with review criteria?

5. Return a summary:
   - Summarize the review process and findings
   - Report how many sub-agents were spawned and what tasks they performed
   - Report total findings and their confidence distribution
   - DO NOT include the detailed findings in output (they're in the feedback store)

The structured output should just be a high-level summary - the detailed findings are accessible via the get_review_feedback MCP tool."""

        agent_schema = {
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

        # Run single agent with custom tools using async context manager
        result, all_feedback = asyncio.run(self._execute_async(
            runner,
            pr_head_branch,
            pr_info,
            repo_name,
            pr_number,
            agent_prompt,
            agent_schema,
            network,
            verbose
        ))

        # Show results
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
        if "by_category" in stats:
            click.echo(f"  By category: {stats['by_category']}")
        if "by_severity" in stats:
            click.echo(f"  By severity: {stats['by_severity']}")
        if "high_confidence_count" in stats:
            click.echo(f"  High confidence (≥0.8): {stats['high_confidence_count']}")

        click.echo(f"\nOverall Assessment:")
        click.echo(result["overall_assessment"])

        # Filter out low-probability items (threshold: 0.5)
        probability_threshold = 0.5
        filtered_feedback = [
            f for f in all_feedback
            if f.get("probability", 1.0) >= probability_threshold
        ]

        click.echo(f"\n{'='*60}")
        click.echo(f"Filtering Feedback")
        click.echo(f"{'='*60}")
        click.echo(f"Total findings recorded: {len(all_feedback)}")
        click.echo(f"After filtering (probability ≥ {probability_threshold}): {len(filtered_feedback)}")

        # Sort by probability (highest first)
        sorted_feedback = sorted(
            filtered_feedback,
            key=lambda x: x.get("probability", 1.0),
            reverse=True
        )

        click.echo(f"\n{'='*60}")
        click.echo(f"Review Complete - {len(sorted_feedback)} High-Confidence Suggestions")
        click.echo(f"{'='*60}")

        if not sorted_feedback:
            click.echo("\nNo high-confidence suggestions. All files look good!")
            return

        # Step 5: Interactive approval
        click.echo(f"\n{'='*60}")
        click.echo("Review Suggestions (sorted by probability)")
        click.echo(f"{'='*60}\n")

        accepted_suggestions = []
        for i, suggestion in enumerate(sorted_feedback, 1):
            probability = suggestion.get("probability", 1.0)
            click.echo(f"\nSuggestion {i}/{len(sorted_feedback)}")
            click.echo(f"  File: {suggestion['file']}")
            click.echo(f"  Lines: {suggestion['line_start']}-{suggestion['line_end']}")
            click.echo(f"  Category: {suggestion['category']}")
            click.echo(f"  Severity: {suggestion['severity']}")
            click.echo(f"  Probability: {probability:.2f}")
            if suggestion.get("probability_reasoning"):
                click.echo(f"  Confidence reasoning: {suggestion['probability_reasoning']}")
            click.echo(f"  Reason: {suggestion['reason']}")

            if suggestion.get('current_code'):
                click.echo(f"\n  Current code:")
                click.echo(f"    {self._indent_code(suggestion['current_code'], 4)}")

            if suggestion.get('suggested_code'):
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
        doc_list = ", ".join(result['documentation_found']) if result['documentation_found'] else "None"
        agent_list = "\n".join([f"  - {a['agent_id']}: {a['task_description']}" for a in result['sub_agents_spawned']])

        summary = f"""Code review completed for PR #{pr_number}.

Review approach:
{result['review_summary'][:300]}{"..." if len(result['review_summary']) > 300 else ""}

Documentation found: {doc_list}

Review criteria:
{result['review_criteria_summary'][:250]}{"..." if len(result['review_criteria_summary']) > 250 else ""}

Sub-agents used:
{agent_list}

Findings:
- Total findings: {stats['total_findings']}
- High confidence (≥0.8): {stats.get('high_confidence_count', 'N/A')}
- After filtering (≥{probability_threshold}): {len(sorted_feedback)}

Overall assessment:
{result['overall_assessment'][:300]}{"..." if len(result['overall_assessment']) > 300 else ""}

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

    async def _execute_async(
        self,
        runner,
        pr_head_branch,
        pr_info,
        repo_name,
        pr_number,
        agent_prompt,
        agent_schema,
        network,
        verbose
    ):
        """Async execution of PR review workflow."""
        async with runner:
            # Setup the sandbox environment
            await runner.setup(network=network)

            # Create checkout tool instance
            checkout_tool = CheckoutCommitTool(runner)

            # Pre-checkout worktrees for PR head and base
            click.echo("\nChecking out worktrees...")
            click.echo(f"  Creating worktree 'pr-head' from {pr_head_branch}...")
            head_result = await checkout_tool.execute({
                "commit": pr_head_branch,
                "worktree_name": "pr-head",
            })
            if not head_result["success"]:
                click.echo(f"Error: {head_result['error']}", err=True)
                sys.exit(1)

            # Use the base ref directly (like "main" or "master")
            # The base commit is already available from the PR head fetch
            click.echo(f"  Creating worktree 'pr-base' from {pr_info['base_ref']}...")
            base_result = await checkout_tool.execute({
                "commit": pr_info['base_ref'],
                "worktree_name": "pr-base",
            })
            if not base_result["success"]:
                click.echo(f"Error: {base_result['error']}", err=True)
                sys.exit(1)

            click.echo("  Worktrees created successfully!")

            # Create MCP server with all built-in tools + GitHub API tools
            mcp_server = PRReviewMCPServer(runner, self.github, repo_name, pr_number)

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
