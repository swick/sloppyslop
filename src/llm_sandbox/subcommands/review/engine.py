"""MCP tools and workflow orchestration for code review functionality."""

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from llm_sandbox import Agent
from llm_sandbox.events import EventEmitter
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


@dataclass
class ReviewAgentStarted:
    """Event: Review agent started."""
    pass


@dataclass
class ReviewWorktreeCheckoutStarted:
    """Event: Worktree checkout started."""
    pass


@dataclass
class ReviewWorktreeCreating:
    """Event: Creating review worktree."""
    worktree_name: str
    ref: str


@dataclass
class ReviewWorktreesReady:
    """Event: All review worktrees created successfully."""
    pass


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
            description="Get the full diff of the code changes being reviewed. Returns the unified diff format showing all changes. For GitHub PRs, this fetches from the GitHub API. For local reviews, this runs git diff locally.",
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
            description="Get the list of commits in the code review. Returns commit SHAs, messages, authors, and timestamps. For GitHub PRs, this fetches from the GitHub API. For local reviews, this runs git log locally.",
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
                    "commit": {
                        "type": "string",
                        "description": "Git commit SHA of the version the file refers to",
                    },
                    "line_start": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed) of the code to replace",
                    },
                    "line_end": {
                        "type": "integer",
                        "description": "Ending line number (1-indexed) of the code to replace (inclusive)",
                    },
                    "suggested_code": {
                        "type": "string",
                        "description": "Replacement code for lines line_start through line_end (inclusive). This code will replace the entire range. Can be empty to suggest deletion.",
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
                "required": ["file", "commit", "line_start", "line_end", "reason", "category"],
            },
        )
        self.runner = runner

    async def execute(self, arguments: Dict[str, Any], mcp_server: Optional["MCPServer"] = None) -> Dict[str, Any]:
        """Record review feedback with validation."""
        file = arguments["file"]
        commit = arguments["commit"]
        line_start = arguments["line_start"]
        line_end = arguments["line_end"]

        # Validate commit is in the review
        if hasattr(self.runner, '_review_commits') and commit not in self.runner._review_commits:
            return {
                "success": False,
                "error": f"Commit '{commit[:7]}' is not in the review. Use get_review_commits to see valid commits.",
            }

        # Validate file exists at that commit and lines are in-bound
        try:
            # Get file content at commit
            file_content = self.runner.git_ops.repo.git.show(f"{commit}:{file}")
            lines = file_content.splitlines()
            total_lines = len(lines)

            # Validate line numbers
            if line_start < 1:
                return {
                    "success": False,
                    "error": f"line_start must be >= 1 (got {line_start})",
                }
            if line_end > total_lines:
                return {
                    "success": False,
                    "error": f"line_end {line_end} exceeds file length ({total_lines} lines) in {file} at {commit[:7]}",
                }
            if line_start > line_end:
                return {
                    "success": False,
                    "error": f"line_start ({line_start}) must be <= line_end ({line_end})",
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"File '{file}' does not exist at commit {commit[:7]} or cannot be read: {str(e)}",
            }

        # Create and store feedback item
        feedback_item = FeedbackItem(
            file=file,
            commit=commit,
            line_start=line_start,
            line_end=line_end,
            suggested_code=arguments.get("suggested_code", ""),
            reason=arguments["reason"],
            category=arguments["category"],
            severity=arguments.get("severity", "medium"),
        )

        self.runner._review_feedback.append(feedback_item)

        return {
            "success": True,
            "message": f"Recorded feedback for {file}:{line_start}-{line_end} at commit {commit[:7]}",
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

    def __init__(self):
        """Initialize review workflow."""
        self.events = EventEmitter()

    async def run(self, runner, review_target: "ReviewTarget") -> Review:
        """Run the full review workflow with LLM agent.

        Expects runner to already be setup (caller must call runner.setup() first).

        Args:
            runner: SandboxRunner instance (already setup)
            review_target: ReviewTarget instance (provides diff/commits data)

        Returns:
            Review object with feedback and metadata
        """
        self.events.emit(ReviewAgentStarted())

        base_ref = review_target.get_base_ref()
        head_ref = review_target.get_head_ref()
        agent_prompt = self._build_agent_prompt(review_target.get_description(), base_ref, head_ref)
        agent_schema = self._build_agent_schema()

        # Run agent
        result, all_feedback = await self._execute_async(
            runner, review_target, base_ref, head_ref,
            agent_prompt, agent_schema
        )

        # Convert result dict to ReviewMetadata
        metadata = ReviewMetadata.from_dict(result)

        return Review(
            summary=None,  # Will be generated later by caller
            feedback=all_feedback,
            base_ref=base_ref,
            head_ref=head_ref,
            target_info=review_target.to_info(),
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
   - Note: get_review_commits returns a list with commit SHAs - use these when recording feedback

3. Spawn sub-agents for specific review tasks:

   a) For each review instruction file found: Spawn one sub-agent per file
      - Pass the review file path to the agent in the task description
      - The agent should read the review file and apply its criteria to the changes
      - Each agent independently audits changes for compliance with their review file's guidelines
      - Only flag clear violations where you can quote the exact rule from the review file
      - Consider only guidelines that apply to the changed files
      - Example task: "Review the changes for compliance with guidelines in docs/review/security.md"

   b) Spawn 1-2 agents for general bug detection (no review file needed):
      - Security issues (SQL injection, XSS, hardcoded credentials, etc.)
      - Logic errors that will produce wrong results
      - Syntax errors, type errors, missing imports
      - Missing error handling for critical operations
      - Focus on what's visible in the diff

   For each sub-agent:
   - Use spawn_agent MCP tool
   - Do not let them create new sub-agents (inheritable=False is already set)
   - Give them clear instructions to use record_review_feedback to record findings
   - Tell them they MUST provide the commit SHA when recording feedback (use commits from get_review_commits)
   - Tell them that suggested_code REPLACES lines line_start through line_end (inclusive)
     - Do NOT include context lines before or after in suggested_code
     - Only include the exact replacement code
   - For review file agents: provide the file path and tell them to read it
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
        agent_schema
    ):
        """Async execution of code review workflow.

        Expects runner to already be setup.
        """
        # Create checkout tool instance
        checkout_tool = CheckoutCommitTool(runner)

        # Pre-checkout worktrees for head and base
        self.events.emit(ReviewWorktreeCheckoutStarted())

        self.events.emit(ReviewWorktreeCreating(worktree_name="review-head", ref=head_ref))
        head_result = await checkout_tool.execute({
            "commit": head_ref,
            "worktree_name": "review-head",
        })
        if not head_result["success"]:
            raise RuntimeError(f"Failed to create worktree 'review-head': {head_result['error']}")

        self.events.emit(ReviewWorktreeCreating(worktree_name="review-base", ref=base_ref))
        base_result = await checkout_tool.execute({
            "commit": base_ref,
            "worktree_name": "review-base",
        })
        if not base_result["success"]:
            raise RuntimeError(f"Failed to create worktree 'review-base': {base_result['error']}")

        self.events.emit(ReviewWorktreesReady())

        # Get valid commits for validation
        commits = review_target.get_commits()
        runner._review_commits = {c["sha"] for c in commits}

        # Create MCP server with all built-in tools + review tools
        mcp_server = PRReviewMCPServer(runner, review_target)

        # Create and execute agent
        agent = Agent(
            runner=runner,
            prompt=agent_prompt,
            output_schema=agent_schema,
            mcp_server=mcp_server,
        )
        await agent.execute()
        result = await agent.wait()

        # Get feedback (no need to worry about cleanup clearing it - that's caller's responsibility)
        all_feedback = list(runner._review_feedback)

        return result, all_feedback
