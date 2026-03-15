"""Rebase functionality for applying review suggestions."""

from pathlib import Path
from typing import List, Optional

import click

from llm_sandbox.git_ops import GitOperations
from .models import FeedbackItem, Review


class ReviewRebase:
    """Handles rebasing commits with review suggestions applied."""

    def __init__(self, project_dir: Path, review: Review):
        """Initialize rebase handler.

        Args:
            project_dir: Project root directory
            review: Review object with suggestions
        """
        self.project_dir = project_dir
        self.review = review
        self.git_ops = GitOperations(project_dir)
        self.worktrees_base = project_dir / ".llm-sandbox" / "worktrees"
        self.created_worktrees = []
        self.fixup_worktree: Optional[Path] = None

    def apply_suggestions(self, suggestions: List[FeedbackItem], branch_name: str) -> None:
        """Apply suggestions by creating fixup commits and rebasing.

        Args:
            suggestions: List of FeedbackItem to apply
            branch_name: Name for the new branch to create

        Raises:
            RuntimeError: If rebase operations fail
        """
        if not suggestions:
            click.echo("No suggestions to apply")
            return

        # Verify all suggestions have commits
        for item in suggestions:
            if not item.commit:
                raise RuntimeError(f"Suggestion {item.get_short_id()} has no commit SHA")

        click.echo(f"\nApplying {len(suggestions)} suggestion(s)...")

        # Step 1: Create a single worktree for making fixup commits
        self.fixup_worktree = self.worktrees_base / "llm-review-fixup"
        try:
            # Create worktree at base_ref initially
            self.git_ops.repo.git.worktree("add", "--detach", str(self.fixup_worktree), self.review.base_ref)
            self.created_worktrees.append(self.fixup_worktree)
        except Exception as e:
            self.cleanup()
            raise RuntimeError(f"Failed to create fixup worktree: {e}") from e

        # Step 2: Create fixup commits for each suggestion
        fixup_commits = []
        try:
            for item in suggestions:
                click.echo(f"\n[{item.get_short_id()}] Creating fixup for {item.file}:{item.line_start}-{item.line_end}")
                fixup_commit = self._create_fixup_commit(item)
                fixup_commits.append(fixup_commit)
                click.echo(f"  Created fixup commit: {fixup_commit[:7]}")
        finally:
            # Clean up fixup worktree
            if self.fixup_worktree:
                self.git_ops.remove_worktree(self.fixup_worktree)
                if self.fixup_worktree in self.created_worktrees:
                    self.created_worktrees.remove(self.fixup_worktree)
                self.fixup_worktree = None

        # Step 3: Create new branch at base_ref
        click.echo(f"\n{'='*60}")
        click.echo(f"Creating branch '{branch_name}' at {self.review.base_ref}")
        click.echo(f"{'='*60}")

        # Create worktree for the new branch
        worktree_dir = self.worktrees_base / f"llm-review-{branch_name}"
        try:
            self.git_ops.create_worktree_on_branch(
                self.review.base_ref,
                worktree_dir,
                branch_name
            )
            self.created_worktrees.append(worktree_dir)
        except Exception as e:
            self.cleanup()
            raise RuntimeError(f"Failed to create branch worktree: {e}") from e

        # Step 3: Get all commits from base to head
        try:
            commits = self.git_ops.get_commits(self.review.base_ref, self.review.head_ref)
            commit_shas = [c.hexsha for c in commits]
            click.echo(f"\nCherry-picking {len(commit_shas)} original commits with {len(fixup_commits)} fixup commits")
        except Exception as e:
            self.cleanup()
            raise RuntimeError(f"Failed to get commit list: {e}") from e

        # Step 4: Group fixup commits by their target commit
        # Parse fixup commit messages to find which commit they fix
        fixup_map = {}  # commit_sha -> list of fixup commit shas
        for idx, item in enumerate(suggestions):
            target_commit = item.commit
            if target_commit not in fixup_map:
                fixup_map[target_commit] = []
            fixup_map[target_commit].append(fixup_commits[idx])

        # Step 5: Cherry-pick commits with fixups interleaved
        click.echo(f"\nCherry-picking commits to '{branch_name}'...")
        worktree_repo = self.git_ops.repo.git.with_cwd(str(worktree_dir))

        # Cherry-pick in order: original commit, then any fixups for it
        for commit_sha in reversed(commit_shas):  # Oldest first
            # Cherry-pick original commit
            try:
                worktree_repo.cherry_pick(commit_sha)
                click.echo(f"  ✓ {commit_sha[:7]}")
            except Exception as e:
                click.echo(f"\n⚠ Cherry-pick conflict on {commit_sha[:7]}", err=True)
                click.echo(f"Worktree at: {worktree_dir}", err=True)
                click.echo("Resolve conflicts and run: git cherry-pick --continue", err=True)
                click.echo("Then manually complete the cherry-picks", err=True)
                raise RuntimeError(f"Cherry-pick conflict: {e}") from e

            # Cherry-pick any fixup commits for this commit
            if commit_sha in fixup_map:
                for fixup_commit in fixup_map[commit_sha]:
                    try:
                        worktree_repo.cherry_pick(fixup_commit)
                        click.echo(f"  ✓ {fixup_commit[:7]} (fixup)")
                    except Exception as e:
                        click.echo(f"\n⚠ Cherry-pick conflict on fixup {fixup_commit[:7]}", err=True)
                        click.echo(f"Worktree at: {worktree_dir}", err=True)
                        click.echo("Resolve conflicts and run: git cherry-pick --continue", err=True)
                        raise RuntimeError(f"Fixup cherry-pick conflict: {e}") from e

        click.echo(f"\n{'='*60}")
        click.echo(f"✓ Successfully created branch '{branch_name}'")
        click.echo(f"{'='*60}")
        click.echo(f"Worktree at: {worktree_dir}")
        click.echo(f"\nTo squash fixup commits, run:")
        click.echo(f"  cd {worktree_dir}")
        click.echo(f"  git rebase -i --autosquash {self.review.base_ref}")

    def _create_fixup_commit(self, item: FeedbackItem) -> str:
        """Create a fixup commit for a single suggestion.

        Args:
            item: FeedbackItem with the suggestion to apply

        Returns:
            SHA of the created fixup commit

        Raises:
            RuntimeError: If fixup commit creation fails
        """
        if not self.fixup_worktree:
            raise RuntimeError("Fixup worktree not initialized")

        worktree_repo = self.git_ops.repo.git.with_cwd(str(self.fixup_worktree))

        try:
            # Checkout the suggestion's commit
            worktree_repo.checkout(item.commit)

            # Apply the suggestion
            file_path = self.fixup_worktree / item.file
            if not file_path.exists():
                raise RuntimeError(f"File {item.file} not found in worktree")

            # Read original file
            original_lines = file_path.read_text().splitlines()

            # Apply suggestion (replace lines)
            start_idx = item.line_start - 1
            end_idx = item.line_end
            suggested_lines = item.suggested_code.splitlines() if item.suggested_code else []

            modified_lines = (
                original_lines[:start_idx] +
                suggested_lines +
                original_lines[end_idx:]
            )

            # Write modified file
            file_path.write_text('\n'.join(modified_lines) + '\n')

            # Stage the change
            worktree_repo.add(item.file)

            # Create fixup commit
            fixup_message = f"fixup! {item.commit[:7]} - {item.file}:{item.line_start}-{item.line_end}"
            worktree_repo.commit("-m", fixup_message)

            # Get the commit SHA
            fixup_sha = worktree_repo.rev_parse("HEAD")

            return fixup_sha

        except Exception as e:
            raise RuntimeError(f"Failed to create fixup commit for {item.file}: {e}") from e

    def cleanup(self) -> None:
        """Clean up any created worktrees."""
        for worktree_path in self.created_worktrees:
            try:
                self.git_ops.remove_worktree(worktree_path)
            except Exception:
                pass
        self.created_worktrees.clear()
