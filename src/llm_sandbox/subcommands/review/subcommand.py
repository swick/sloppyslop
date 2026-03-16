"""Code review subcommand with support for GitHub PRs and local commits."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import click

from llm_sandbox.config import load_config
from llm_sandbox.event_handlers import wire_up_all_events
from llm_sandbox.mcp_tools import CheckoutCommitTool
from llm_sandbox.output import create_output_service
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
Subcommands:
  list      - List all saved reviews
  create    - Run a new review
  show      - Display a saved review
  check     - Interactively review suggestions
  edit      - Edit a suggestion's diff
  dismiss   - Mark suggestions as ignored
  undismiss - Restore dismissed suggestions
  rebase    - Apply suggestions via rebase
  post      - Post review to GitHub PR
  remove    - Delete a saved review

\b
Examples:
  llm-sandbox review list
  llm-sandbox review create --pr 123
  llm-sandbox review create --base main --head feature --probability-threshold 0.7
  llm-sandbox review show my-review --diff
  llm-sandbox review show my-review --commit abc123
  llm-sandbox review show my-review a1b2c3 d4e5f6
  llm-sandbox review check my-review
  llm-sandbox review edit my-review a1b2c3
  llm-sandbox review edit my-review a1b2c3 --reason
  llm-sandbox review edit my-review --summary
  llm-sandbox review dismiss my-review a1b2c3 d4e5f6
  llm-sandbox review undismiss my-review a1b2c3
  llm-sandbox review rebase my-review --branch fix/suggestions
  llm-sandbox review rebase my-review a1b2c3 d4e5f6 --branch fix/suggestions
  llm-sandbox review post my-review"""

    def add_arguments(self, command):
        """Add custom arguments."""
        # Keep using action-based approach for compatibility
        # But organize options by which action they belong to

        command.params.append(
            click.Argument(
                ["action"],
                required=False,
                default="list",
                type=click.Choice(["list", "create", "remove", "post", "show", "edit", "check", "dismiss", "undismiss", "rebase"], case_sensitive=False),
                metavar="ACTION",
            )
        )

        command.params.append(
            click.Argument(
                ["review_id"],
                required=False,
                type=str,
                metavar="[REVIEW_ID]",
            )
        )

        command.params.append(
            click.Argument(
                ["suggestion_ids"],
                nargs=-1,
                required=False,
                type=str,
                metavar="[SUGGESTION_ID...]",
            )
        )

        # CREATE options
        command.params.append(click.Option(["--pr"], type=int, help="[create] GitHub PR number"))
        command.params.append(click.Option(["--base"], type=str, help="[create] Base commit/branch (requires --head)"))
        command.params.append(click.Option(["--head"], type=str, help="[create] Head commit/branch (requires --base)"))
        command.params.append(click.Option(["--probability-threshold"], type=float, default=0.5,
                                          help="[create] Probability threshold for auto-ignore (default: 0.5)"))

        # SHOW options
        command.params.append(click.Option(["--commit"], type=str, help="[show] Filter by commit SHA or range"))
        command.params.append(click.Option(["--diff"], is_flag=True, help="[show] Display unified diffs"))
        command.params.append(click.Option(["--all"], is_flag=True, help="[show] Include duplicates"))

        # REBASE options
        command.params.append(click.Option(["--branch"], type=str, help="[rebase] Branch name (required)"))

        # EDIT options
        command.params.append(click.Option(["--summary"], is_flag=True, help="[edit] Edit the review summary instead of a suggestion"))
        command.params.append(click.Option(["--reason"], is_flag=True, help="[edit] Edit suggestion reason/summary instead of diff"))

        # Shared options
        command.params.append(click.Option(["--id"], type=str, help="Review ID (alternative to positional)"))
        command.params.append(click.Option(["--with-token"], type=str, help="[create/post] GitHub token (default: $GH_TOKEN)"))

        return command

    def execute(self, project_dir: Path, **kwargs):
        """Execute review command, routing to appropriate sub-sub-command."""
        action = kwargs.get("action", "list")
        store = ReviewStore(project_dir)

        # Create output service (quiet by default for review subcommands unless verbose)
        verbose = kwargs.get("verbose", False)
        output = create_output_service(format="text", verbose=verbose)
        kwargs["output"] = output  # Pass to sub-methods

        # For actions that need review_id, use positional if provided, otherwise fall back to --id
        if action in ("show", "post", "edit", "check", "dismiss", "undismiss", "rebase", "remove"):
            positional_id = kwargs.get("review_id")
            option_id = kwargs.get("id")
            if positional_id:
                kwargs["id"] = positional_id
            elif not option_id and action != "remove":
                output.error(f"review ID is required for {action}")
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
            output.error(f"Unknown action '{action}'")
            sys.exit(1)

    def _list_reviews(self, store: ReviewStore, **kwargs):
        """List all available reviews."""
        output = kwargs.get("output")
        review_ids = store.list_ids()

        if not review_ids:
            output.info("No reviews found.")
            return

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

                output.info(f"  {review_id}")
                output.info(f"    Target: {target_info}")
                output.info(f"    Findings: {feedback_count}")
                output.info("")
            except Exception as e:
                output.warning(f"  {review_id} (error loading: {e})")
                output.info("")

    def _remove_review(self, store: ReviewStore, **kwargs):
        """Remove a review by ID."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")
        if not review_id:
            output.error("--id is required for remove")
            sys.exit(1)

        try:
            store.remove(review_id)
            output.success(f"Removed review: {review_id}")
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
            sys.exit(1)

    def _create_review(self, store: ReviewStore, **kwargs):
        """Create a new review."""
        output = kwargs.get("output")
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
        runner = SandboxRunner(project_dir, config, verbose=verbose, network=network)

        # Wire up event handlers
        wire_up_all_events(runner, output)

        # Validate arguments
        if pr_number and (base_commit or head_commit):
            output.error("Cannot use --pr with --base/--head. Choose one mode.")
            sys.exit(1)

        if not pr_number and not (base_commit and head_commit):
            output.error("Either --pr OR both --base and --head must be provided.")
            sys.exit(1)

        if (base_commit and not head_commit) or (head_commit and not base_commit):
            output.error("Both --base and --head must be provided together.")
            sys.exit(1)

        # Create appropriate ReviewTarget
        if pr_number is not None:
            if not token:
                output.error(
                    "GitHub token not found.\n"
                    "\n"
                    "Set GH_TOKEN environment variable or use --with-token option:\n"
                    "  export GH_TOKEN=ghp_xxxxxxxxxxxx\n"
                    "  or\n"
                    "  llm-sandbox review create --pr 123 --with-token ghp_xxxxxxxxxxxx"
                )
                sys.exit(1)
            review_target = GitHubPRTarget(pr_number, token, project_dir)
        else:
            review_target = LocalReviewTarget(base_commit, head_commit, project_dir)

        output.info(f"\n{'='*60}")
        output.info(f"Multi-Agent Code Review: {review_target.get_description()}")
        output.info(f"{'='*60}\n")

        # Fetch remote data if needed (PR mode)
        try:
            review_target.fetch_if_needed()
        except RuntimeError as e:
            output.error(str(e))
            sys.exit(1)

        # Run review workflow
        workflow = ReviewWorkflow()

        # Wire up workflow events
        from llm_sandbox.subcommands.review.engine import (
            ReviewAgentStarted,
            ReviewWorktreeCheckoutStarted,
            ReviewWorktreeCreating,
            ReviewWorktreesReady
        )
        workflow.events.on(ReviewAgentStarted, lambda e: output.info("\nStarting review agent..."))
        workflow.events.on(ReviewWorktreeCheckoutStarted, lambda e: output.info("\nChecking out worktrees..."))
        workflow.events.on(ReviewWorktreeCreating, lambda e: output.info(f"  Creating worktree '{e.worktree_name}' from {e.ref}..."))
        workflow.events.on(ReviewWorktreesReady, lambda e: output.info("  Worktrees created successfully!"))

        # Execute workflow with async context manager
        async def run_workflow():
            async with runner:
                return await workflow.run(runner, review_target)

        review = asyncio.run(run_workflow())

        # Display agent results
        if review.metadata:
            self._display_agent_results(review.metadata.to_dict(), output)

        # Mark low-confidence suggestions as ignored
        probability_threshold = kwargs.get("probability_threshold", 0.5)
        ignored_count = 0
        for item in review.feedback:
            if item.probability is not None and item.probability < probability_threshold:
                item.ignore = True
                ignored_count += 1

        if ignored_count > 0:
            output.info(f"\nAutomatically ignored {ignored_count} low-confidence suggestions (probability < {probability_threshold})")

        # Filter and display feedback
        sorted_feedback = review.filter_feedback(probability_threshold=probability_threshold)
        self._display_feedback_statistics(review, sorted_feedback, output)

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

        output.info(f"\n{'='*60}")
        output.info("Review Complete")
        output.info(f"{'='*60}")
        output.info(f"\nReview ID: {review_id}")
        output.info(f"Review completed for {review_target.get_description()}")

        stats = review.get_statistics()
        output.info(f"Total findings: {stats['total']}")
        output.info(f"High-confidence suggestions: {len(sorted_feedback)}")

        if not sorted_feedback:
            output.info("\nNo high-confidence suggestions. All files look good!")

        output.success(f"\n✓ Review saved to: {output_file}")


    def _display_agent_results(self, result: dict, output: OutputService):
        """Display the agent's review results."""
        output.info(f"\n{'='*60}")
        output.info("Review Agent Results")
        output.info(f"{'='*60}")
        output.info(f"\nReview Summary:")
        output.info(result["review_summary"])

        output.info(f"\nDocumentation Found: {len(result['documentation_found'])}")
        if result["documentation_found"]:
            for file in result["documentation_found"]:
                output.info(f"  - {file}")

        output.info(f"\nReview Criteria:")
        output.info(result["review_criteria_summary"])

        output.info(f"\nSub-Agents Spawned: {len(result['sub_agents_spawned'])}")
        for agent in result["sub_agents_spawned"]:
            output.info(f"  - {agent['agent_id']}: {agent['task_description']}")

        output.info(f"\nFindings Statistics:")
        stats = result["findings_statistics"]
        output.info(f"  Total findings: {stats['total_findings']}")
        if "duplicates_count" in stats:
            output.info(f"  Duplicates marked: {stats['duplicates_count']}")
        if "unique_findings" in stats:
            output.info(f"  Unique findings: {stats['unique_findings']}")
        if "by_category" in stats:
            output.info(f"  By category: {stats['by_category']}")
        if "by_severity" in stats:
            output.info(f"  By severity: {stats['by_severity']}")
        if "high_confidence_count" in stats:
            output.info(f"  High confidence (≥0.8): {stats['high_confidence_count']}")

        output.info(f"\nOverall Assessment:")
        output.info(result["overall_assessment"])

    def _display_feedback_statistics(self, review: Review, filtered_feedback: List[FeedbackItem], output: OutputService):
        """Display feedback filtering statistics."""
        stats = review.get_statistics()
        probability_threshold = 0.5

        output.info(f"\n{'='*60}")
        output.info(f"Filtering Feedback")
        output.info(f"{'='*60}")
        output.info(f"Total findings recorded: {stats['total']}")
        output.info(f"Duplicates marked: {stats['duplicates']}")
        output.info(f"Unique findings: {stats['unique']}")
        output.info(f"After filtering (probability ≥ {probability_threshold}, excluding duplicates): {len(filtered_feedback)}")

        output.info(f"\n{'='*60}")
        output.info(f"Review Complete - {len(filtered_feedback)} High-Confidence Suggestions")
        output.info(f"{'='*60}")

    def _post_review(self, store: ReviewStore, **kwargs):
        """Post a review to its target."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")
        token = kwargs.get("with_token") or os.getenv("GH_TOKEN")
        project_dir = store.project_dir

        # Note: review_id validation is done in execute() method
        if not review_id:
            output.error("review ID is required for post")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
            sys.exit(1)

        # Check if review has target info
        if not review.target_info or not review.target_info.get("type"):
            output.error("Review does not have target information (cannot determine where to post)")
            sys.exit(1)

        # Reconstruct the target
        from .targets import ReviewTarget

        try:
            target = ReviewTarget.from_info(review.target_info, token=token, project_dir=project_dir)
            # Fetch PR info if needed (for GitHub PRs)
            try:
                target.fetch_if_needed()
            except RuntimeError as e:
                output.error(f"Failed to fetch target data: {e}")
                sys.exit(1)
        except Exception as e:
            output.error(f"Failed to reconstruct review target: {e}")
            sys.exit(1)

        # Check if target can publish
        if not target.can_publish():
            output.error(f"Target type '{review.target_info['type']}' does not support publishing")
            sys.exit(1)

        # Display preview
        output.info(f"\nReview ID: {review_id}")
        try:
            preview = target.get_publish_preview(review)

            # Display preview using OutputService
            output.info(f"\n{'='*60}")
            output.info("Review Post Preview")
            output.info(f"{'='*60}")
            output.info(f"\nTarget: {preview.target_description}")
            if preview.repository:
                output.info(f"Repository: {preview.repository}")
            if preview.pr_url:
                output.info(f"PR URL: {preview.pr_url}")

            output.info(f"\nWill post:")
            if preview.will_post_summary:
                output.info(f"  • 1 summary comment")
            output.info(f"  • {preview.will_post_inline_count} inline comments")

            if preview.summary_body:
                output.info(f"\n{'='*60}")
                output.info("Summary Comment")
                output.info(f"{'='*60}\n")
                output.info(preview.summary_body)
                output.info(f"\n{'='*60}")

            # Show sample inline comments
            if preview.sample_inline_comments:
                output.info(f"\nSample inline comments ({len(preview.sample_inline_comments)} of {preview.will_post_inline_count}):")
                for i, sample in enumerate(preview.sample_inline_comments, 1):
                    output.info(f"\n  {i}. {sample.file}:{sample.line_start}-{sample.line_end} [{sample.category}]")
                    for line in sample.body_preview:
                        output.info(f"     {line}")
                    if sample.total_lines > len(sample.body_preview):
                        output.info(f"     ... ({sample.total_lines - len(sample.body_preview)} more lines)")
        except Exception as e:
            output.error(f"Failed to generate preview: {e}")
            sys.exit(1)

        # Confirm
        output.info(f"\n{'='*60}")
        if not click.confirm("Post review?", default=True):
            output.info("\nCancelled. Review not posted.")
            return

        # Post the review
        output.info(f"\n{'='*60}")
        output.info("Posting Review")
        output.info(f"{'='*60}\n")

        try:
            result = target.publish_review(review)

            # Display results
            if result.summary_posted:
                output.success("✓ Posted review summary")

            output.info(f"\n{'='*60}")
            output.info(f"Posted {result.inline_comments_posted}/{result.inline_comments_posted + result.inline_comments_failed} inline comments")
            output.info(f"{'='*60}")

            if result.failed_suggestions:
                output.warning(f"\n⚠ Failed to post {len(result.failed_suggestions)} suggestions as inline comments")
                output.warning("These suggestions could not be posted (file may not exist in PR diff):")
                for s in result.failed_suggestions:
                    output.warning(f"  - {s.file}:{s.line_start}-{s.line_end}")

            # Display success message
            success = target.get_publish_success()
            output.info(f"\n{'='*60}")
            output.success("✓ Review posted successfully!")
            output.info(f"{'='*60}")
            if success.pr_url:
                output.info(f"\nView at: {success.pr_url}")
        except Exception as e:
            output.error(f"\n✗ Error posting review: {e}")
            sys.exit(1)

    def _show_review(self, store: ReviewStore, **kwargs):
        """Show a review with summary and suggestions by commit."""
        output = kwargs.get("output")
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
            output.error("review ID is required for show")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
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
            print(f"\n{'='*60}")
            print(f"Review: {review_id}")
            print(f"{'='*60}")

            # Target info
            if review.target_info and review.target_info.get("type"):
                target_type = review.target_info["type"]
                print(f"\nTarget: {target_type}")
                if target_type == "github_pr":
                    pr_number = review.target_info.get("pr_number")
                    repo_name = review.target_info.get("repo_name")
                    if pr_number and repo_name:
                        print(f"  PR: #{pr_number} ({repo_name})")
                elif target_type == "local":
                    if review.base_ref and review.head_ref:
                        print(f"  Range: {review.base_ref}..{review.head_ref}")

            # Summary
            if review.summary:
                print(f"\n{'='*60}")
                print("Summary")
                print(f"{'='*60}")
                # Show first 300 chars
                summary_text = review.summary
                if len(summary_text) > 300:
                    print(summary_text[:300] + "...")
                else:
                    print(summary_text)

            # Statistics
            stats = review.get_statistics()
            print(f"\n{'='*60}")
            print("Statistics")
            print(f"{'='*60}")
            print(f"Total findings: {stats['total']}")
            print(f"Unique findings: {stats['unique']}")
            print(f"Duplicates marked: {stats['duplicates']}")
            print(f"Ignored: {stats['ignored']}")
            if not show_all and stats['duplicates'] > 0:
                print(f"\nShowing unique findings only (use --all to show {stats['duplicates']} duplicates)")

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
                print(f"\n{'='*60}")
                print(f"Suggestions by Commit")
                print(f"{'='*60}")

            # Track whether we've shown the first item (for separator placement)
            first_item = True

            # Show commits with suggestions
            for commit_sha, items in sorted(by_commit.items()):
                short_sha = commit_sha[:7] if len(commit_sha) >= 7 else commit_sha
                print(f"\n[{short_sha}] ({len(items)} suggestions):")
                for item in items:
                    self._display_feedback_item(item, diff_generator, is_first=first_item)
                    first_item = False

            # Show items without commit info
            if no_commit:
                print(f"\n[no commit] ({len(no_commit)} suggestions):")
                for item in no_commit:
                    self._display_feedback_item(item, diff_generator, is_first=first_item)
                    first_item = False
        else:
            if matching_suggestion_ids:
                print("No matching suggestions found")
            else:
                print(f"\n{'='*60}")
                print("No suggestions (all filtered)")
                print(f"{'='*60}")

        if not matching_suggestion_ids:
            print()  # Empty line at end

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
                        print(f"Warning: Invalid range format '{filter_str}', skipping", file=sys.stderr)
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
                    print(f"Warning: Failed to expand range '{filter_str}': {e}", file=sys.stderr)
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
            print(f"  [{short_id}] {item.file}:{item.line_start} [{item.category}] {reason_short}")
        else:
            # Detailed format: use the shared display method
            self._display_suggestion_full(item, diff_generator, show_separator=not is_first)

    def _dismiss_suggestions(self, store: ReviewStore, **kwargs):
        """Dismiss (ignore) one or more suggestions."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())

        # Note: review_id validation is done in execute() method
        if not review_id:
            output.error("review ID is required for dismiss")
            sys.exit(1)

        if not suggestion_ids:
            output.error("at least one suggestion ID is required for dismiss")
            output.error("Usage: llm-sandbox review dismiss <review-id> <suggestion-id> [<suggestion-id> ...]")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
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
            output.info(f"Dismissed {dismissed_count} suggestion(s)")

        if not_found:
            output.warning(f"{len(not_found)} suggestion(s) not found: {', '.join(not_found)}")

        # Save the updated review
        if dismissed_count > 0:
            store.save(review_id, review)
            output.success(f"✓ Updated review saved to: {store.reviews_dir / f'{review_id}.yaml'}")

    def _rebase_suggestions(self, store: ReviewStore, **kwargs):
        """Rebase commits with review suggestions applied."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())
        branch_name = kwargs.get("branch")

        # Note: review_id validation is done in execute() method
        if not review_id:
            output.error("review ID is required for rebase")
            sys.exit(1)

        if not branch_name:
            output.error("--branch is required for rebase")
            output.error("Usage: llm-sandbox review rebase <review-id> [<suggestion-id> ...] --branch <branch-name>")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
            sys.exit(1)

        # Verify review has base_ref and head_ref
        if not review.base_ref or not review.head_ref:
            output.error("Review does not have base_ref and head_ref (cannot rebase)")
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
                            output.warning(f"Suggestion {suggestion_id} has no suggested code, skipping")
                        else:
                            suggestions.append(item)
                        found = True
                        break
                if not found:
                    not_found.append(suggestion_id)

            if not_found:
                output.error(f"{len(not_found)} suggestion(s) not found: {', '.join(not_found)}")
                sys.exit(1)
        else:
            # Use all active (non-duplicate, non-ignored) suggestions with suggested code
            active_feedback = review.get_active_feedback()
            for item in active_feedback:
                if item.suggested_code:
                    suggestions.append(item)

            if not suggestions:
                output.error("No active suggestions with code found in review")
                sys.exit(1)

            output.info(f"No suggestion IDs specified, using all {len(suggestions)} active suggestions")

        if not suggestions:
            output.error("No valid suggestions to apply")
            sys.exit(1)

        # Display what we're about to do
        output.info(f"\n{'='*60}")
        output.info(f"Rebase Plan")
        output.info(f"{'='*60}")
        output.info(f"Review: {review_id}")
        output.info(f"Base: {review.base_ref}")
        output.info(f"Head: {review.head_ref}")
        output.info(f"New branch: {branch_name}")
        output.info(f"Suggestions to apply: {len(suggestions)}")
        for item in suggestions:
            output.info(f"  [{item.get_short_id()}] {item.file}:{item.line_start}-{item.line_end} at {item.commit[:7]}")

        # Confirm
        if not click.confirm("\nProceed with rebase?", default=True):
            output.info("Cancelled.")
            return

        # Define conflict resolver callback
        def resolve_conflicts(request) -> bool:
            """Interactive conflict resolution callback."""
            commit_type = "fixup" if request.is_fixup else "commit"
            output.warning(f"\n{'='*60}")
            output.warning(f"⚠ Cherry-pick conflict on {commit_type} {request.commit_sha[:7]}")
            output.warning(f"{'='*60}")

            editor = os.environ.get('EDITOR', 'vi')

            while True:
                # Show conflicted files
                output.info(f"\nConflicted files ({len(request.conflicted_files)}):")
                for f in request.conflicted_files:
                    output.info(f"  {f}")

                output.info(f"\nOpening conflicted files in {editor}...")
                output.info("Resolve conflicts and save. The files will be staged automatically.")
                output.info("Press Enter to continue, or Ctrl+C to abort.")

                try:
                    input()
                except KeyboardInterrupt:
                    output.error("\n\nAborting cherry-pick.")
                    request.repo.git.cherry_pick('--abort')
                    return False

                # Open each conflicted file in the editor
                for conflicted_file in request.conflicted_files:
                    file_path = request.worktree_dir / conflicted_file
                    if not file_path.exists():
                        output.warning(f"{conflicted_file} not found, skipping")
                        continue

                    try:
                        subprocess.run([editor, str(file_path)])
                    except Exception as e:
                        output.error(f"Error opening {conflicted_file}: {e}")
                        continue

                    # Stage the file after editing
                    try:
                        request.repo.git.add(conflicted_file)
                        output.success(f"✓ Staged {conflicted_file}")
                    except Exception as e:
                        output.warning(f"Failed to stage {conflicted_file}: {e}")

                # Check if conflicts remain
                try:
                    remaining_conflicts = request.repo.git.diff('--name-only', '--diff-filter=U').strip()
                    if remaining_conflicts:
                        output.info(f"\nUnresolved conflicts remain:")
                        output.info(remaining_conflicts)
                        output.info("Continuing to resolve...")
                        continue
                except Exception:
                    pass

                # No more conflicts
                break

            return True

        # Perform the rebase
        rebase = ReviewRebase(store.project_dir, review, conflict_resolver=resolve_conflicts)
        try:
            output.info(f"\nApplying {len(suggestions)} suggestion(s)...")
            rebase.apply_suggestions(suggestions, branch_name)
            output.info("\nCleaning up worktrees...")
            output.success(f"\n✓ Successfully created branch '{branch_name}'")
            output.info(f"\n{'='*60}")
            output.info(f"To squash fixup commits:")
            output.info(f"  git checkout {branch_name}")
            output.info(f"  git rebase -i --autosquash {review.base_ref}")
            output.info(f"\nTo push the branch:")
            output.info(f"  git push origin {branch_name}")
        except Exception as e:
            output.error(f"\n✗ Rebase failed: {e}")
            output.error("\nYou may need to manually complete the rebase or clean up worktrees.")
            sys.exit(1)

    def _undismiss_suggestions(self, store: ReviewStore, **kwargs):
        """Un-dismiss (un-ignore) one or more suggestions."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())

        # Note: review_id validation is done in execute() method
        if not review_id:
            output.error("review ID is required for undismiss")
            sys.exit(1)

        if not suggestion_ids:
            output.error("at least one suggestion ID is required for undismiss")
            output.error("Usage: llm-sandbox review undismiss <review-id> <suggestion-id> [<suggestion-id> ...]")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
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
            output.info(f"Un-dismissed {undismissed_count} suggestion(s)")

        if not_found:
            output.warning(f"{len(not_found)} suggestion(s) not found: {', '.join(not_found)}")

        # Save the updated review
        if undismissed_count > 0:
            store.save(review_id, review)
            output.success(f"✓ Updated review saved to: {store.reviews_dir / f'{review_id}.yaml'}")

    def _check_suggestions(self, store: ReviewStore, **kwargs):
        """Interactively review suggestions one by one."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")

        # Note: review_id validation is done in execute() method
        if not review_id:
            output.error("review ID is required for check")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
            sys.exit(1)

        # Get active suggestions
        active_feedback = review.get_active_feedback()
        if not active_feedback:
            output.info("No active suggestions to review")
            return

        # Create diff generator
        diff_generator = FeedbackDiffGenerator(store.project_dir)

        # Create editor for editing suggestions
        editor = ReviewEditor(store.project_dir, review)

        output.info(f"\n{'='*60}")
        output.info(f"Interactive Review: {review_id}")
        output.info(f"{'='*60}")
        output.info(f"\nReviewing {len(active_feedback)} active suggestions\n")

        # Offer to edit summary first
        modified = False
        if click.confirm("Edit review summary first?", default=False):
            if editor.edit_review_summary():
                modified = True
                output.success("✓ Summary updated")

        for i, item in enumerate(active_feedback, 1):
            output.info(f"\n{'='*60}")
            output.info(f"Suggestion {i}/{len(active_feedback)}")
            output.info(f"{'='*60}")

            # Display the suggestion with diff
            self._display_suggestion_full(item, diff_generator, show_separator=False, output=output)

            # Prompt for action
            while True:
                output.info("")
                action = click.prompt(
                    "Action (edit [d]iff, edit [r]eason, [i]gnore, [a]ccept, [q]uit)",
                    type=click.Choice(['d', 'r', 'i', 'a', 'q'], case_sensitive=False),
                    default='a',
                    show_choices=False
                )

                if action == 'q':
                    output.info("\nQuitting review...")
                    if modified:
                        store.save(review_id, review)
                        output.success(f"✓ Changes saved to: {store.reviews_dir / f'{review_id}.yaml'}")
                    return

                elif action == 'a':
                    # Accept - just move to next
                    break

                elif action == 'i':
                    # Ignore (dismiss)
                    item.ignore = True
                    modified = True
                    output.success(f"✓ Dismissed suggestion {item.get_short_id()}")
                    break

                elif action == 'd':
                    # Edit diff
                    try:
                        if editor.edit_suggestion(item.get_short_id()):
                            modified = True
                            # Redisplay after edit
                            output.info(f"\n{'='*60}")
                            output.info(f"Suggestion {i}/{len(active_feedback)} (after edit)")
                            output.info(f"{'='*60}")
                            self._display_suggestion_full(item, diff_generator, show_separator=False, output=output)
                    except RuntimeError as e:
                        output.error(f"Error editing diff: {e}")
                    # Continue loop to show prompt again
                    continue

                elif action == 'r':
                    # Edit reason
                    try:
                        if editor.edit_item_reason(item.get_short_id()):
                            modified = True
                            # Redisplay after edit
                            output.info(f"\n{'='*60}")
                            output.info(f"Suggestion {i}/{len(active_feedback)} (after edit)")
                            output.info(f"{'='*60}")
                            self._display_suggestion_full(item, diff_generator, show_separator=False, output=output)
                    except RuntimeError as e:
                        output.error(f"Error editing reason: {e}")
                    # Continue loop to show prompt again
                    continue

        # Save if modified
        if modified:
            store.save(review_id, review)
            output.success(f"\n✓ Changes saved to: {store.reviews_dir / f'{review_id}.yaml'}")
        else:
            output.info("\nNo changes made")

    def _edit_suggestion(self, store: ReviewStore, **kwargs):
        """Edit a suggestion or summary interactively."""
        output = kwargs.get("output")
        review_id = kwargs.get("id")
        suggestion_ids = kwargs.get("suggestion_ids", ())
        edit_summary = kwargs.get("summary", False)
        edit_reason = kwargs.get("reason", False)

        # Note: review_id validation is done in execute() method
        if not review_id:
            output.error("review ID is required for edit")
            sys.exit(1)

        # Load the review
        try:
            review = store.load(review_id)
        except FileNotFoundError:
            output.error(f"Review '{review_id}' not found.")
            sys.exit(1)

        # Edit summary, reason, or suggestion
        editor = ReviewEditor(store.project_dir, review)

        if edit_summary:
            # Edit the review summary
            modified = editor.edit_review_summary()
            if modified:
                output.success("✓ Summary updated")
                store.save(review_id, review)
                output.success(f"✓ Review saved to: {store.reviews_dir / f'{review_id}.yaml'}")
            else:
                output.info("No changes made to summary")
        else:
            # Edit a suggestion
            if not suggestion_ids or len(suggestion_ids) != 1:
                output.error("exactly one suggestion ID is required for edit")
                output.error("Usage: llm-sandbox review edit <review-id> <suggestion-id>")
                output.error("   or: llm-sandbox review edit <review-id> <suggestion-id> --reason")
                output.error("   or: llm-sandbox review edit <review-id> --summary")
                sys.exit(1)

            suggestion_id = suggestion_ids[0]

            try:
                if edit_reason:
                    # Edit the suggestion reason
                    modified = editor.edit_item_reason(suggestion_id)
                    if modified:
                        output.success(f"✓ Updated reason for {suggestion_id}")
                    else:
                        output.info("No changes to reason")
                else:
                    # Edit the suggestion diff
                    modified = editor.edit_suggestion(suggestion_id)
                    if modified:
                        output.success(f"✓ Updated suggestion {suggestion_id}")
                    else:
                        output.info("No changes made to editor file")

                if modified:
                    # Save the updated review
                    store.save(review_id, review)
                    output.success(f"✓ Review saved to: {store.reviews_dir / f'{review_id}.yaml'}")
            except RuntimeError as e:
                output.error(str(e))
                sys.exit(1)

    def _display_suggestion_full(self, item: FeedbackItem, diff_generator: FeedbackDiffGenerator, show_separator: bool = True, output: Optional['OutputService'] = None) -> None:
        """Display a single suggestion with full details and diff.

        Args:
            item: FeedbackItem to display
            diff_generator: Diff generator for showing changes
            show_separator: Whether to show separator before item
            output: Optional OutputService (uses print() if None for pager compatibility)
        """
        short_id = item.get_short_id()

        # Choose output method
        out = output.info if output else print

        # Separator before item
        if show_separator:
            out("═" * 80)

        # Detailed format: show full reason and diff
        out(f"\n[{short_id}] {item.file}:{item.line_start}-{item.line_end} [{item.category}]")
        if item.commit:
            short_commit = item.commit[:7] if len(item.commit) >= 7 else item.commit
            out(f"Commit: {short_commit}")
        out(f"Severity: {item.severity}")
        if item.probability is not None:
            out(f"Confidence: {item.probability:.2f}")

        # Newline before reason
        out("")

        # Reason text (indented by 4 spaces)
        for line in item.reason.split('\n'):
            out(f"    {line}")

        # Generate and show diff
        try:
            diff_text = diff_generator.generate_diff(item)
            if diff_text:
                out("")  # Blank line before diff
                for line in diff_text.split('\n'):
                    self._display_diff_line(line, output=output)
            else:
                out("(No diff available)")
        except Exception as e:
            out(f"(Error generating diff: {e})")

    def _display_diff_line(self, line: str, output: Optional['OutputService'] = None):
        """Display a diff line with appropriate coloring.

        Args:
            line: Diff line to display
            output: Optional OutputService (uses print() if None for pager compatibility)
        """
        # ANSI color codes
        BOLD = '\033[1m'
        CYAN = '\033[36m'
        GREEN = '\033[32m'
        RED = '\033[31m'
        RESET = '\033[0m'

        # Choose output method
        out = output.info if output else print

        if line.startswith('+++') or line.startswith('---'):
            # File headers (bold)
            out(f"{BOLD}{line}{RESET}")
        elif line.startswith('@@'):
            # Hunk headers (cyan)
            out(f"{CYAN}{line}{RESET}")
        elif line.startswith('+'):
            # Additions (green)
            out(f"{GREEN}{line}{RESET}")
        elif line.startswith('-'):
            # Deletions (red)
            out(f"{RED}{line}{RESET}")
        else:
            # Context lines (no color)
            out(line)
