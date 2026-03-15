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
from .rebase import ReviewRebase
from .editor import ReviewEditor


class ReviewSubcommand(Subcommand):
    """Code review with instruction-based criteria using multi-agent workflow."""

    name = "review"
    help = """AI-powered code review for GitHub PRs and local commits.

\b
Supports multiple actions:
  list      - List all saved reviews (default)
  create    - Run a new review (--pr for GitHub, --base/--head for local)
  show      - Display a saved review with optional filtering
  check     - Interactively review suggestions one by one
  edit      - Interactively edit a suggestion's diff
  dismiss   - Mark suggestions as ignored
  undismiss - Un-mark suggestions (restore dismissed suggestions)
  rebase    - Apply suggestions by rebasing commits
  post      - Post a review to GitHub PR
  remove    - Delete a saved review

\b
Examples:
  llm-sandbox review list
  llm-sandbox review create --pr 123
  llm-sandbox review create --base main --head feature
  llm-sandbox review create --pr 123 --probability-threshold 0.7
  llm-sandbox review show my-review
  llm-sandbox review show my-review --diff
  llm-sandbox review show my-review --commit abc123      # Filter by commit
  llm-sandbox review show my-review --commit main..feature
  llm-sandbox review show my-review a1b2c3 d4e5f6        # Show specific suggestions by ID
  llm-sandbox review check my-review                     # Review suggestions interactively
  llm-sandbox review edit my-review a1b2c3               # Edit suggestion interactively
  llm-sandbox review dismiss my-review a1b2c3 d4e5f6     # Dismiss suggestions
  llm-sandbox review undismiss my-review a1b2c3          # Restore dismissed suggestions
  llm-sandbox review rebase my-review a1b2c3 --branch fix/suggestions  # Apply via rebase
  llm-sandbox review post my-review"""

    def add_arguments(self, command):
        """Add custom arguments."""
        # Action argument
        command.params.append(
            click.Argument(
                ["action"],
                required=False,
                default="list",
                type=click.Choice(["list", "create", "remove", "post", "show", "edit", "check", "dismiss", "undismiss", "rebase"], case_sensitive=False),
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
        command.params.append(
            click.Option(
                ["--probability-threshold"],
                type=float,
                default=0.5,
                help="Probability threshold for auto-ignoring low-confidence suggestions (default: 0.5)",
            )
        )

        # Rebase options
        command.params.append(
            click.Option(
                ["--branch"],
                type=str,
                help="Branch name for rebase action (required for rebase)",
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

        # For show/post/edit/check/dismiss/undismiss/rebase, use positional review_id if provided, otherwise fall back to --id
        if action in ("show", "post", "edit", "check", "dismiss", "undismiss", "rebase"):
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
        elif action == "check":
            self._check_suggestions(store, **kwargs)
        elif action == "edit":
            self._edit_suggestion(store, **kwargs)
        elif action == "dismiss":
            self._dismiss_suggestions(store, **kwargs)
        elif action == "undismiss":
            self._undismiss_suggestions(store, **kwargs)
        elif action == "rebase":
            self._rebase_suggestions(store, **kwargs)
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

        # Mark low-confidence suggestions as ignored
        probability_threshold = kwargs.get("probability_threshold", 0.5)
        ignored_count = 0
        for item in review.feedback:
            if item.probability is not None and item.probability < probability_threshold:
                item.ignore = True
                ignored_count += 1

        if ignored_count > 0:
            click.echo(f"\nAutomatically ignored {ignored_count} low-confidence suggestions (probability < {probability_threshold})")

        # Filter and display feedback
        sorted_feedback = review.filter_feedback(probability_threshold=probability_threshold)
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

        # Capture output to decide whether to use pager
        from io import StringIO
        import sys

        class ColorStringIO(StringIO):
            """StringIO that pretends to be a TTY for color output."""
            def isatty(self):
                return True

        output_buffer = ColorStringIO()
        old_stdout = sys.stdout
        sys.stdout = output_buffer
        try:
            self._render_review(review, store, suggestion_ids, commit_filter, show_diff, show_all, review_id)
        finally:
            sys.stdout = old_stdout

        output_text = output_buffer.getvalue()
        output_lines = output_text.count('\n')

        # Get terminal height, default to 24 if not available
        try:
            terminal_height = os.get_terminal_size().lines
        except (AttributeError, OSError):
            terminal_height = 24

        # Use pager if output exceeds terminal height
        if output_lines > terminal_height:
            # Set LESS to interpret color codes
            old_less = os.environ.get('LESS', '')
            if 'R' not in old_less:
                os.environ['LESS'] = old_less + 'R'

            try:
                click.echo_via_pager(output_text, color=True)
            finally:
                if old_less:
                    os.environ['LESS'] = old_less
                else:
                    os.environ.pop('LESS', None)
        else:
            # Output is short, print directly
            click.echo(output_text, nl=False)

    def _render_review(self, review, store, suggestion_ids, commit_filter, show_diff, show_all, review_id):
        """Render review output (either to stdout or captured for pager)."""
        # Build filters
        matching_commits = None
        if commit_filter:
            # Parse commit filter string as a single commit or range
            matching_commits = self._parse_commit_filters((commit_filter,), review, store.project_dir)

        matching_suggestion_ids = set(suggestion_ids) if suggestion_ids else None

        # Create diff generator if needed
        diff_generator = FeedbackDiffGenerator(store.project_dir) if show_diff else None

        # Only show review header if not filtering by specific suggestion IDs
        if not matching_suggestion_ids:
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

        # Start with active feedback if not showing all, otherwise all feedback
        feedback_to_show = review.feedback if show_all else review.get_active_feedback()

        # Count duplicates that were filtered out
        if not show_all:
            duplicates_skipped = len([f for f in review.feedback if f.duplicate_of is not None])

        for item in feedback_to_show:
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
            # Only show header if not filtering by specific suggestion IDs
            if not matching_suggestion_ids:
                click.echo(f"\n{'='*60}")
                if show_all:
                    click.echo(f"All Suggestions by Commit ({total_displayed} items)")
                else:
                    click.echo(f"Suggestions by Commit ({total_displayed} unique, {duplicates_skipped} duplicates hidden)")
                click.echo(f"{'='*60}")

            # Track whether we've shown the first item (for separator placement)
            first_item = True

            # Show commits with suggestions
            for commit_sha, items in sorted(by_commit.items()):
                short_sha = commit_sha[:7] if len(commit_sha) >= 7 else commit_sha
                click.echo(f"\n[{short_sha}] ({len(items)} suggestions):")
                for item in items:
                    self._display_feedback_item(item, diff_generator, is_first=first_item)
                    first_item = False

            # Show items without commit info
            if no_commit:
                click.echo(f"\n[no commit] ({len(no_commit)} suggestions):")
                for item in no_commit:
                    self._display_feedback_item(item, diff_generator, is_first=first_item)
                    first_item = False
        else:
            if matching_suggestion_ids:
                click.echo("No matching suggestions found")
            else:
                click.echo(f"\n{'='*60}")
                click.echo("No suggestions (all filtered)")
                click.echo(f"{'='*60}")

        if not matching_suggestion_ids:
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

    def _display_feedback_item(self, item: FeedbackItem, diff_generator: Optional[FeedbackDiffGenerator], is_first: bool = True):
        """Display a single feedback item, with optional diff.

        Args:
            item: FeedbackItem to display
            diff_generator: Optional diff generator for showing changes
            is_first: Whether this is the first item being displayed (default: True)
        """
        short_id = item.get_short_id()

        if diff_generator is None:
            # Compact format: one line summary with ID
            reason_short = item.reason[:60] + "..." if len(item.reason) > 60 else item.reason
            reason_short = reason_short.replace('\n', ' ').strip()
            click.echo(f"  [{short_id}] {item.file}:{item.line_start} [{item.category}] {reason_short}")
        else:
            # Detailed format: use the shared display method
            self._display_suggestion_full(item, diff_generator, show_separator=not is_first)

    def _dismiss_suggestions(self, store: ReviewStore, **kwargs):
        """Dismiss (ignore) one or more suggestions."""
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for dismiss", err=True)
            sys.exit(1)

        if not suggestion_ids:
            click.echo("Error: at least one suggestion ID is required for dismiss", err=True)
            click.echo("Usage: llm-sandbox review dismiss <review-id> <suggestion-id> [<suggestion-id> ...]", err=True)
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Find and mark suggestions as ignored
        dismissed_count = 0
        not_found = []

        for suggestion_id in suggestion_ids:
            found = False
            for item in review.feedback:
                if item.get_short_id() == suggestion_id:
                    if not item.ignore:
                        item.ignore = True
                        dismissed_count += 1
                    found = True
                    break
            if not found:
                not_found.append(suggestion_id)

        # Report results
        if dismissed_count > 0:
            click.echo(f"Dismissed {dismissed_count} suggestion(s)")

        if not_found:
            click.echo(f"Warning: {len(not_found)} suggestion(s) not found: {', '.join(not_found)}", err=True)

        # Save the updated review
        if dismissed_count > 0:
            store.save(review_id, review)
            click.echo(f"✓ Updated review saved to: {store.reviews_dir / f'{review_id}.yaml'}")

    def _rebase_suggestions(self, store: ReviewStore, **kwargs):
        """Rebase commits with review suggestions applied."""
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())
        branch_name = kwargs.get("branch")

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for rebase", err=True)
            sys.exit(1)

        if not branch_name:
            click.echo("Error: --branch is required for rebase", err=True)
            click.echo("Usage: llm-sandbox review rebase <review-id> [<suggestion-id> ...] --branch <branch-name>", err=True)
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Verify review has base_ref and head_ref
        if not review.base_ref or not review.head_ref:
            click.echo("Error: Review does not have base_ref and head_ref (cannot rebase)", err=True)
            sys.exit(1)

        # Find suggestions by ID, or use all non-ignored suggestions if none specified
        suggestions = []
        not_found = []

        if suggestion_ids:
            # Use specified suggestion IDs
            for suggestion_id in suggestion_ids:
                found = False
                for item in review.feedback:
                    if item.get_short_id() == suggestion_id:
                        if not item.suggested_code:
                            click.echo(f"Warning: Suggestion {suggestion_id} has no suggested code, skipping", err=True)
                        else:
                            suggestions.append(item)
                        found = True
                        break
                if not found:
                    not_found.append(suggestion_id)

            if not_found:
                click.echo(f"Error: {len(not_found)} suggestion(s) not found: {', '.join(not_found)}", err=True)
                sys.exit(1)
        else:
            # Use all active (non-duplicate, non-ignored) suggestions with suggested code
            active_feedback = review.get_active_feedback()
            for item in active_feedback:
                if item.suggested_code:
                    suggestions.append(item)

            if not suggestions:
                click.echo("No active suggestions with code found in review", err=True)
                sys.exit(1)

            click.echo(f"No suggestion IDs specified, using all {len(suggestions)} active suggestions")

        if not suggestions:
            click.echo("Error: No valid suggestions to apply", err=True)
            sys.exit(1)

        # Display what we're about to do
        click.echo(f"\n{'='*60}")
        click.echo(f"Rebase Plan")
        click.echo(f"{'='*60}")
        click.echo(f"Review: {review_id}")
        click.echo(f"Base: {review.base_ref}")
        click.echo(f"Head: {review.head_ref}")
        click.echo(f"New branch: {branch_name}")
        click.echo(f"Suggestions to apply: {len(suggestions)}")
        for item in suggestions:
            click.echo(f"  [{item.get_short_id()}] {item.file}:{item.line_start}-{item.line_end} at {item.commit[:7]}")

        # Confirm
        if not click.confirm("\nProceed with rebase?", default=True):
            click.echo("Cancelled.")
            return

        # Perform the rebase
        rebase = ReviewRebase(store.project_dir, review)
        try:
            rebase.apply_suggestions(suggestions, branch_name)
        except Exception as e:
            click.echo(f"\n✗ Rebase failed: {e}", err=True)
            click.echo("\nYou may need to manually complete the rebase or clean up worktrees.", err=True)
            sys.exit(1)

    def _undismiss_suggestions(self, store: ReviewStore, **kwargs):
        """Un-dismiss (un-ignore) one or more suggestions."""
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for undismiss", err=True)
            sys.exit(1)

        if not suggestion_ids:
            click.echo("Error: at least one suggestion ID is required for undismiss", err=True)
            click.echo("Usage: llm-sandbox review undismiss <review-id> <suggestion-id> [<suggestion-id> ...]", err=True)
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Find and un-mark suggestions as ignored
        undismissed_count = 0
        not_found = []

        for suggestion_id in suggestion_ids:
            found = False
            for item in review.feedback:
                if item.get_short_id() == suggestion_id:
                    if item.ignore:
                        item.ignore = False
                        undismissed_count += 1
                    found = True
                    break
            if not found:
                not_found.append(suggestion_id)

        # Report results
        if undismissed_count > 0:
            click.echo(f"Un-dismissed {undismissed_count} suggestion(s)")

        if not_found:
            click.echo(f"Warning: {len(not_found)} suggestion(s) not found: {', '.join(not_found)}", err=True)

        # Save the updated review
        if undismissed_count > 0:
            store.save(review_id, review)
            click.echo(f"✓ Updated review saved to: {store.reviews_dir / f'{review_id}.yaml'}")

    def _check_suggestions(self, store: ReviewStore, **kwargs):
        """Interactively review suggestions one by one."""
        review_id = kwargs.get("id")

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for check", err=True)
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Get active suggestions
        active_feedback = review.get_active_feedback()
        if not active_feedback:
            click.echo("No active suggestions to review")
            return

        # Create diff generator
        diff_generator = FeedbackDiffGenerator(store.project_dir)

        # Create editor for editing suggestions
        editor = ReviewEditor(store.project_dir, review)

        click.echo(f"\n{'='*60}")
        click.echo(f"Interactive Review: {review_id}")
        click.echo(f"{'='*60}")
        click.echo(f"\nReviewing {len(active_feedback)} active suggestions\n")

        modified = False
        for i, item in enumerate(active_feedback, 1):
            click.echo(f"\n{'='*60}")
            click.echo(f"Suggestion {i}/{len(active_feedback)}")
            click.echo(f"{'='*60}")

            # Display the suggestion with diff
            self._display_suggestion_full(item, diff_generator, show_separator=False)

            # Prompt for action
            while True:
                click.echo()
                action = click.prompt(
                    "Action ([e]dit, [d]ismiss, [a]ccept, [q]uit)",
                    type=click.Choice(['e', 'd', 'a', 'q'], case_sensitive=False),
                    default='a',
                    show_choices=False
                )

                if action == 'q':
                    click.echo("\nQuitting review...")
                    if modified:
                        store.save(review_id, review)
                        click.echo(f"✓ Changes saved to: {store.reviews_dir / f'{review_id}.yaml'}")
                    return

                elif action == 'a':
                    # Accept - just move to next
                    break

                elif action == 'd':
                    # Dismiss
                    item.ignore = True
                    modified = True
                    click.echo(f"✓ Dismissed suggestion {item.get_short_id()}")
                    break

                elif action == 'e':
                    # Edit
                    try:
                        if editor.edit_suggestion(item.get_short_id()):
                            modified = True
                            # Redisplay after edit
                            click.echo(f"\n{'='*60}")
                            click.echo(f"Suggestion {i}/{len(active_feedback)} (after edit)")
                            click.echo(f"{'='*60}")
                            self._display_suggestion_full(item, diff_generator, show_separator=False)
                    except RuntimeError as e:
                        click.echo(f"Error editing: {e}", err=True)
                    # Continue loop to show prompt again
                    continue

        # Save if modified
        if modified:
            store.save(review_id, review)
            click.echo(f"\n✓ Changes saved to: {store.reviews_dir / f'{review_id}.yaml'}")
        else:
            click.echo("\nNo changes made")

    def _edit_suggestion(self, store: ReviewStore, **kwargs):
        """Edit a suggestion interactively."""
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())

        # Note: review_id validation is done in execute() method
        if not review_id:
            click.echo("Error: review ID is required for edit", err=True)
            sys.exit(1)

        if not suggestion_ids or len(suggestion_ids) != 1:
            click.echo("Error: exactly one suggestion ID is required for edit", err=True)
            click.echo("Usage: llm-sandbox review edit <review-id> <suggestion-id>", err=True)
            sys.exit(1)

        suggestion_id = suggestion_ids[0]

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            click.echo(f"Error: Review '{review_id}' not found.", err=True)
            sys.exit(1)

        # Edit the suggestion
        editor = ReviewEditor(store.project_dir, review)
        try:
            modified = editor.edit_suggestion(suggestion_id)
            if modified:
                # Save the updated review
                store.save(review_id, review)
                click.echo(f"✓ Review saved to: {store.reviews_dir / f'{review_id}.yaml'}")
        except RuntimeError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    def _display_suggestion_full(self, item: FeedbackItem, diff_generator: FeedbackDiffGenerator, show_separator: bool = True) -> None:
        """Display a single suggestion with full details and diff.

        Args:
            item: FeedbackItem to display
            diff_generator: Diff generator for showing changes
            show_separator: Whether to show separator before item
        """
        short_id = item.get_short_id()

        # Separator before item
        if show_separator:
            click.echo("═" * 80)

        # Detailed format: show full reason and diff
        click.echo(f"\n[{short_id}] {item.file}:{item.line_start}-{item.line_end} [{item.category}]")
        if item.commit:
            short_commit = item.commit[:7] if len(item.commit) >= 7 else item.commit
            click.echo(f"Commit: {short_commit}")
        click.echo(f"Severity: {item.severity}")
        if item.probability is not None:
            click.echo(f"Confidence: {item.probability:.2f}")

        # Newline before reason
        click.echo()

        # Reason text (indented by 4 spaces)
        for line in item.reason.split('\n'):
            click.echo(f"    {line}")

        # Generate and show diff
        try:
            diff_text = diff_generator.generate_diff(item)
            if diff_text:
                click.echo()  # Blank line before diff
                for line in diff_text.split('\n'):
                    self._display_diff_line(line)
            else:
                click.echo("\n(No diff available)")
        except Exception as e:
            click.echo(f"\n(Error generating diff: {e})")

    def _display_diff_line(self, line: str):
        """Display a diff line with appropriate coloring.

        Args:
            line: Diff line to display
        """
        if line.startswith('+++') or line.startswith('---'):
            # File headers (bold)
            click.secho(line, bold=True)
        elif line.startswith('@@'):
            # Hunk headers (cyan)
            click.secho(line, fg='cyan')
        elif line.startswith('+'):
            # Additions (green)
            click.secho(line, fg='green')
        elif line.startswith('-'):
            # Deletions (red)
            click.secho(line, fg='red')
        else:
            # Context lines (no color)
            click.echo(line)
