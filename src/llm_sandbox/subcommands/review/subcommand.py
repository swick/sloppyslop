"""Code review subcommand with support for GitHub PRs and local commits."""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

import click

from llm_sandbox import AgentConfig
from llm_sandbox.config import load_config
from llm_sandbox.mcp_tools import CheckoutCommitTool
from llm_sandbox.runner import SandboxRunner
from llm_sandbox.subcommand import Subcommand
from .models import (
    Review,
    ReviewMetadata,
    ReviewStore,
    FeedbackItem,
)
from .targets import (
    ReviewTarget,
    LocalReviewTarget,
    GitHubPRTarget,
)
from .engine import ReviewWorkflow
from .diff_generator import FeedbackDiffGenerator


class ReviewSubcommand(Subcommand):
    """Code review with instruction-based criteria using multi-agent workflow."""

    name = "review"
    help = """AI-powered code review for GitHub PRs and local commits.

\b
Supports multiple actions:
  list   - List all saved reviews (default)
  create - Run a new review (--pr for GitHub, --base/--head for local)
  show   - Display a saved review with optional filtering
  post   - Post a review to GitHub PR
  remove - Delete a saved review

\b
Examples:
  llm-sandbox review list
  llm-sandbox review create --pr 123
  llm-sandbox review create --base main --head feature
  llm-sandbox review show my-review
  llm-sandbox review show my-review --diff
  llm-sandbox review show my-review --commit abc123      # Filter by commit
  llm-sandbox review show my-review --commit main..feature
  llm-sandbox review show my-review a1b2c3 d4e5f6        # Show specific suggestions by ID
  llm-sandbox review post my-review"""

    def add_arguments(self, command):
        """Add custom arguments."""
        # Action argument
        command.params.append(
            click.Argument(
                ["action"],
                required=False,
                default="list",
                type=click.Choice(["list", "create", "remove", "post", "show"], case_sensitive=False),
                metavar="ACTION",
            )
        )

        # Review ID (positional for show/post, option for create/remove)
        command.params.append(
            click.Argument(
                ["review_id"],
                required=False,
                type=str,
                metavar="[REVIEW_ID]",
            )
        )

        # Keep --id as fallback/alternative
        command.params.append(
            click.Option(
                ["--id"],
                type=str,
                help="Review ID for remove action (required); optional for create (auto-generated if omitted)",
            )
        )

        # Create options
        command.params.append(
            click.Option(
                ["--pr"],
                type=int,
                help="GitHub PR number to review (for create action)",
            )
        )
        command.params.append(
            click.Option(
                ["--base"],
                type=str,
                help="Base commit/branch for local review (for create action, requires --head)",
            )
        )
        command.params.append(
            click.Option(
                ["--head"],
                type=str,
                help="Head commit/branch for local review (for create action, requires --base)",
            )
        )
        command.params.append(
            click.Option(
                ["--with-token"],
                type=str,
                help="GitHub token for create/post actions (default: $GH_TOKEN)",
            )
        )

        # Suggestion ID filters (for show command)
        command.params.append(
            click.Argument(
                ["suggestion_ids"],
                nargs=-1,
                required=False,
                type=str,
                metavar="[SUGGESTION_ID...]",
            )
        )
        command.params.append(
            click.Option(
                ["--commit"],
                type=str,
                help="Filter suggestions by commit SHA or range (for show action; e.g., abc123 or main..feature)",
            )
        )
        command.params.append(
            click.Option(
                ["--diff"],
                is_flag=True,
                help="Display unified diffs for each suggestion (for show action)",
            )
        )
        command.params.append(
            click.Option(
                ["--all"],
                is_flag=True,
                help="Include duplicate findings (for show action; default: unique only)",
            )
        )

        return command

    def execute(self, project_dir: Path, **kwargs):
        """Execute review command, routing to appropriate sub-sub-command."""
        action = kwargs.get("action", "list")
        store = ReviewStore(project_dir)

        # For show/post, use positional review_id if provided, otherwise fall back to --id
        if action in ("show", "post"):
            positional_id = kwargs.get("review_id")
            option_id = kwargs.get("id")
            if positional_id:
                kwargs["id"] = positional_id
            elif not option_id:
                click.echo(f"Error: review ID is required for {action}", err=True)
                sys.exit(1)

        if action == "list":
            self._list_reviews(store, **kwargs)
        elif action == "remove":
            self._remove_review(store, **kwargs)
        elif action == "create":
            self._create_review(store, **kwargs)
        elif action == "post":
            self._post_review(store, **kwargs)
        elif action == "show":
            self._show_review(store, **kwargs)
        else:
            click.echo(f"Error: Unknown action '{action}'", err=True)
            sys.exit(1)

    def _list_reviews(self, store: ReviewStore, **kwargs):
        """List all available reviews."""
        review_ids = store.list_ids()

        if not review_ids:
            click.echo("No reviews found.")
            return

        click.echo(f"\n{'='*60}")
        click.echo("Available Reviews")
        click.echo(f"{'='*60}\n")

        for review_id in review_ids:
            # Try to load review to get metadata
            try:
                review = store.load(review_id)
                feedback_count = len(review.feedback)

                # Extract target info from summary if available
                target_info = "Unknown target"
                if review.summary:
                    # Try to extract target from summary first line
                    first_line = review.summary.split('\n')[0]
                    if 'PR #' in first_line:
                        target_info = first_line.split('for ')[1].rstrip('.') if 'for ' in first_line else first_line
                    elif '..' in first_line:
                        target_info = first_line.split('for ')[1].rstrip('.') if 'for ' in first_line else first_line

                click.echo(f"  {review_id}")
                click.echo(f"    Target: {target_info}")
                click.echo(f"    Findings: {feedback_count}")
                click.echo()
            except Exception as e:
                click.echo(f"  {review_id} (error loading: {e})")
                click.echo()

    def _remove_review(self, store: ReviewStore, **kwargs):
        """Remove a review by ID."""
        review_id = kwargs.get("id")
        if not review_id:
            click.echo("Error: --id is required for remove", err=True)
            sys.exit(1)

        try:
            store.remove(review_id)
            click.echo(f"✓ Removed review: {review_id}")
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

    def _create_review(self, store: ReviewStore, **kwargs):
        """Create a new review."""
        pr_number = kwargs.get("pr")
        base_commit = kwargs.get("base")
        head_commit = kwargs.get("head")
        token = kwargs.get("with_token") or os.getenv("GH_TOKEN")
        network = kwargs["network"]
        verbose = kwargs["verbose"]
        review_id = kwargs.get("id")
        project_dir = store.project_dir

        # Load config and create runner
        config = load_config(project_dir)
        runner = SandboxRunner(project_dir, config)

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
                    "  llm-sandbox review create --pr 123 --with-token ghp_xxxxxxxxxxxx",
                    err=True
                )
                sys.exit(1)
            review_target = GitHubPRTarget(pr_number, token, project_dir)
        else:
            review_target = LocalReviewTarget(base_commit, head_commit, project_dir)

        click.echo(f"\n{'='*60}")
        click.echo(f"Multi-Agent Code Review: {review_target.get_description()}")
        click.echo(f"{'='*60}\n")

        # Fetch remote data if needed (PR mode)
        review_target.fetch_if_needed()

        # Run review workflow
        workflow = ReviewWorkflow()
        review = workflow.run(runner, review_target, network, verbose)

        # Display agent results
        if review.metadata:
            self._display_agent_results(review.metadata.to_dict())

        # Filter and display feedback
        sorted_feedback = review.filter_feedback(probability_threshold=0.5)
        self._display_feedback_statistics(review, sorted_feedback)

        # Generate summary (even if no high-confidence suggestions)
        if sorted_feedback:
            review.summary = workflow.build_summary_text(review_target, review, sorted_feedback)
        else:
            # Generate minimal summary for reviews with no high-confidence issues
            review.summary = f"Review completed for {review_target.get_description()}.\n\nNo high-confidence suggestions found. All files look good!"

        # Generate review ID if not specified
        if not review_id:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if pr_number:
                review_id = f"pr-{pr_number}-{timestamp}"
            else:
                # Sanitize branch names for use in filename
                base_safe = re.sub(r'[^\w-]', '_', base_commit)
                head_safe = re.sub(r'[^\w-]', '_', head_commit)
                review_id = f"{base_safe}-{head_safe}-{timestamp}"

        # Save review (always save, even if no high-confidence suggestions)
        output_file = store.save(review_id, review)

        click.echo(f"\n{'='*60}")
        click.echo("Review Complete")
        click.echo(f"{'='*60}")
        click.echo(f"\nReview ID: {review_id}")
        click.echo(f"Review completed for {review_target.get_description()}")

        stats = review.get_statistics()
        click.echo(f"Total findings: {stats['total']}")
        click.echo(f"High-confidence suggestions: {len(sorted_feedback)}")

        if not sorted_feedback:
            click.echo("\nNo high-confidence suggestions. All files look good!")

        click.echo(f"\n✓ Review saved to: {output_file}")


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

    def _display_feedback_statistics(self, review: Review, filtered_feedback: List[FeedbackItem]):
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

    def _post_review(self, store: ReviewStore, **kwargs):
        """Post a review to its target."""
        review_id = kwargs.get("id")
        token = kwargs.get("with_token") or os.getenv("GH_TOKEN")
        project_dir = store.project_dir

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for post", err=True)
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Check if review has target info
        if not review.target_info or not review.target_info.get("type"):
            click.echo("Error: Review does not have target information (cannot determine where to post)", err=True)
            sys.exit(1)

        # Reconstruct the target
        from .targets import ReviewTarget

        try:
            target = ReviewTarget.from_info(review.target_info, token=token, project_dir=project_dir)
            # Fetch PR info if needed (for GitHub PRs)
            target.fetch_if_needed()
        except Exception as e:
            click.echo(f"Error: Failed to reconstruct review target: {e}", err=True)
            sys.exit(1)

        # Check if target can publish
        if not target.can_publish():
            click.echo(f"Error: Target type '{review.target_info['type']}' does not support publishing", err=True)
            sys.exit(1)

        # Display preview
        click.echo(f"\nReview ID: {review_id}")
        try:
            target.print_publish_preview(review)
        except Exception as e:
            click.echo(f"Error: Failed to generate preview: {e}", err=True)
            sys.exit(1)

        # Confirm
        click.echo(f"\n{'='*60}")
        if not click.confirm("Post review?", default=True):
            click.echo("\nCancelled. Review not posted.")
            return

        # Post the review
        click.echo(f"\n{'='*60}")
        click.echo("Posting Review")
        click.echo(f"{'='*60}\n")

        try:
            target.publish_review(review)
            target.print_published_success()
        except Exception as e:
            click.echo(f"\n✗ Error posting review: {e}", err=True)
            sys.exit(1)

    def _show_review(self, store: ReviewStore, **kwargs):
        """Show a review with summary and suggestions by commit."""
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())  # Tuple of suggestion IDs
        commit_filter = kwargs.get("commit")  # Single commit or range string
        show_diff = kwargs.get("diff", False)
        show_all = kwargs.get("all", False)

        # If suggestion IDs are specified, show diff by default
        if suggestion_ids and not show_diff:
            show_diff = True

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for show", err=True)
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Build filters
        matching_commits = None
        if commit_filter:
            # Parse commit filter string as a single commit or range
            matching_commits = self._parse_commit_filters((commit_filter,), review, store.project_dir)

        matching_suggestion_ids = set(suggestion_ids) if suggestion_ids else None

        # Create diff generator if needed
        diff_generator = FeedbackDiffGenerator(store.project_dir) if show_diff else None

        # Display header
        click.echo(f"\n{'='*60}")
        click.echo(f"Review: {review_id}")
        click.echo(f"{'='*60}")

        # Target info
        if review.target_info and review.target_info.get("type"):
            target_type = review.target_info["type"]
            click.echo(f"\nTarget: {target_type}")
            if target_type == "github_pr":
                pr_number = review.target_info.get("pr_number")
                repo_name = review.target_info.get("repo_name")
                if pr_number and repo_name:
                    click.echo(f"  PR: #{pr_number} ({repo_name})")
            elif target_type == "local":
                if review.base_ref and review.head_ref:
                    click.echo(f"  Range: {review.base_ref}..{review.head_ref}")

        # Summary
        if review.summary:
            click.echo(f"\n{'='*60}")
            click.echo("Summary")
            click.echo(f"{'='*60}")
            # Show first 300 chars
            summary_text = review.summary
            if len(summary_text) > 300:
                click.echo(summary_text[:300] + "...")
            else:
                click.echo(summary_text)

        # Statistics
        stats = review.get_statistics()
        click.echo(f"\n{'='*60}")
        click.echo("Statistics")
        click.echo(f"{'='*60}")
        click.echo(f"Total findings: {stats['total']}")
        click.echo(f"Unique findings: {stats['unique']}")
        click.echo(f"Duplicates marked: {stats['duplicates']}")
        click.echo(f"Ignored: {stats['ignored']}")
        if not show_all and stats['duplicates'] > 0:
            click.echo(f"\nShowing unique findings only (use --all to show {stats['duplicates']} duplicates)")

        # Group feedback by commit
        from collections import defaultdict
        by_commit = defaultdict(list)
        no_commit = []
        duplicates_skipped = 0

        for item in review.feedback:
            # Always skip ignored items
            if item.ignore:
                continue

            # Skip duplicates unless --all is specified
            if not show_all and item.duplicate_of is not None:
                duplicates_skipped += 1
                continue

            # Apply suggestion ID filter if specified
            if matching_suggestion_ids is not None:
                if item.get_short_id() not in matching_suggestion_ids:
                    continue  # Skip items that don't match suggestion ID filter

            # Backward compatibility: handle old reviews without commit
            if not item.commit:
                no_commit.append(item)
                continue

            # Apply commit filter if specified
            if matching_commits is not None:
                if item.commit not in matching_commits:
                    continue  # Skip items that don't match commit filter

            by_commit[item.commit].append(item)

        # Count total items being displayed
        total_displayed = sum(len(items) for items in by_commit.values()) + len(no_commit)

        # Display suggestions by commit
        if by_commit or no_commit:
            click.echo(f"\n{'='*60}")
            if show_all:
                click.echo(f"All Suggestions by Commit ({total_displayed} items)")
            else:
                click.echo(f"Suggestions by Commit ({total_displayed} unique, {duplicates_skipped} duplicates hidden)")
            click.echo(f"{'='*60}")

            # Show commits with suggestions
            for commit_sha, items in sorted(by_commit.items()):
                short_sha = commit_sha[:7] if len(commit_sha) >= 7 else commit_sha
                click.echo(f"\n[{short_sha}] ({len(items)} suggestions):")
                for item in items:
                    self._display_feedback_item(item, diff_generator)

            # Show items without commit info
            if no_commit:
                click.echo(f"\n[no commit] ({len(no_commit)} suggestions):")
                for item in no_commit:
                    self._display_feedback_item(item, diff_generator)
        else:
            click.echo(f"\n{'='*60}")
            click.echo("No suggestions (all filtered)")
            click.echo(f"{'='*60}")

        click.echo()  # Empty line at end

    def _parse_commit_filters(self, commit_filters: tuple, review: Review, project_dir: Path) -> set:
        """Parse commit filters into set of matching commit SHAs.

        Args:
            commit_filters: Tuple of commit SHAs or ranges (e.g., "abc123", "abc123..def456")
            review: Review object with feedback items
            project_dir: Project directory for git operations

        Returns:
            Set of matching commit SHAs from the review's feedback
        """
        matching = set()

        # Get all unique commits from feedback
        all_commits = {item.commit for item in review.feedback if item.commit}

        if not all_commits:
            return matching

        for filter_str in commit_filters:
            if '..' in filter_str:
                # Range: use git to expand, then filter against feedback commits
                try:
                    from llm_sandbox.git_ops import GitOperations
                    git_ops = GitOperations(project_dir)

                    # Parse the range
                    parts = filter_str.split('..')
                    if len(parts) != 2:
                        click.echo(f"Warning: Invalid range format '{filter_str}', skipping", err=True)
                        continue

                    base, head = parts[0].strip(), parts[1].strip()

                    # Get commits in range
                    range_commits = git_ops.get_commits(base, head)
                    range_shas = {c.hexsha for c in range_commits}

                    # Find matches in feedback commits
                    for commit in all_commits:
                        if commit in range_shas:
                            matching.add(commit)
                except Exception as e:
                    click.echo(f"Warning: Failed to expand range '{filter_str}': {e}", err=True)
                    continue
            else:
                # Single commit: match by prefix
                for commit in all_commits:
                    if commit.startswith(filter_str):
                        matching.add(commit)

        return matching

    def _display_feedback_item(self, item: FeedbackItem, diff_generator: Optional[FeedbackDiffGenerator]):
        """Display a single feedback item, with optional diff.

        Args:
            item: FeedbackItem to display
            diff_generator: Optional diff generator for showing changes
        """
        short_id = item.get_short_id()

        if diff_generator is None:
            # Compact format: one line summary with ID
            reason_short = item.reason[:60] + "..." if len(item.reason) > 60 else item.reason
            reason_short = reason_short.replace('\n', ' ').strip()
            click.echo(f"  [{short_id}] {item.file}:{item.line_start} [{item.category}] {reason_short}")
        else:
            # Detailed format: show full reason and diff
            click.echo(f"\n  [{short_id}] {item.file}:{item.line_start}-{item.line_end} [{item.category}]")
            click.echo(f"    Severity: {item.severity}")
            if item.probability is not None:
                click.echo(f"    Confidence: {item.probability:.2f}")
            click.echo(f"\n    Reason:")
            for line in item.reason.split('\n'):
                click.echo(f"      {line}")

            # Generate and show diff
            try:
                diff_text = diff_generator.generate_diff(item)
                if diff_text:
                    click.echo(f"\n    Diff:")
                    for line in diff_text.split('\n'):
                        self._display_diff_line(line)
                else:
                    click.echo(f"\n    (No diff available)")
            except Exception as e:
                click.echo(f"\n    (Error generating diff: {e})")

            click.echo()  # Blank line between items

    def _display_diff_line(self, line: str):
        """Display a diff line with appropriate coloring.

        Args:
            line: Diff line to display
        """
        indent = "      "

        if line.startswith('+++') or line.startswith('---'):
            # File headers (bold)
            click.secho(f"{indent}{line}", bold=True)
        elif line.startswith('@@'):
            # Hunk headers (cyan)
            click.secho(f"{indent}{line}", fg='cyan')
        elif line.startswith('+'):
            # Additions (green)
            click.secho(f"{indent}{line}", fg='green')
        elif line.startswith('-'):
            # Deletions (red)
            click.secho(f"{indent}{line}", fg='red')
        else:
            # Context lines (no color)
            click.echo(f"{indent}{line}")
