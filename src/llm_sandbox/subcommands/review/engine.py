"""MCP tools and workflow orchestration for code review functionality."""

import asyncio
import sys
from typing import Any, Dict, List, Optional

import click

from llm_sandbox import AgentConfig
from llm_sandbox.mcp_tools import (
    MCPTool,
    MCPServer,
    ExecuteCommandTool,
    CheckoutCommitTool,
    ReadFileTool,
    GlobTool,
    GrepTool,
    SpawnAgentTool,
    WaitForAgentsTool,
)
from .models import FeedbackItem, Review, ReviewMetadata


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

class ReviewWorkflow:
    """Orchestrates the code review workflow using LLM agents."""

    def run(self, runner, review_target: "ReviewTarget", network: str, verbose: bool) -> Review:
        """Run the full review workflow with LLM agent.

        Args:
            runner: SandboxRunner instance
            review_target: ReviewTarget instance (provides diff/commits data)
            network: Network mode ("isolated" or "enabled")
            verbose: Whether to show verbose output

        Returns:
            Review object with feedback and metadata
        """
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

        # Convert result dict to ReviewMetadata
        metadata = ReviewMetadata.from_dict(result)

        return Review(
            summary=None,  # Will be generated later by caller
            feedback=all_feedback,
            metadata=metadata
        )

    def build_summary_text(self, review_target: "ReviewTarget", review: Review, sorted_feedback: List[FeedbackItem]) -> str:
        """Build the summary text for the review.

        Args:
            review_target: ReviewTarget instance
            review: Review object with metadata
            sorted_feedback: Filtered feedback items

        Returns:
            Formatted summary text
        """
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

    async def _execute_async(
        self,
        runner,
        review_target: "ReviewTarget",
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
