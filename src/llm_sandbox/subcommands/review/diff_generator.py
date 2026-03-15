"""Generate diffs for feedback items showing suggested changes."""

import difflib
from pathlib import Path
from typing import Optional

from llm_sandbox.git_ops import GitOperations
from .models import FeedbackItem


class FeedbackDiffGenerator:
    """Generates unified diffs for feedback items."""

    def __init__(self, project_dir: Path):
        """Initialize the diff generator.

        Args:
            project_dir: Project root directory
        """
        self.project_dir = project_dir
        self.git_ops = GitOperations(project_dir)

    def generate_diff(self, item: FeedbackItem, context_lines: int = 3) -> Optional[str]:
        """Generate a unified diff for a feedback item.

        Args:
            item: FeedbackItem with suggested changes
            context_lines: Number of context lines to show around changes

        Returns:
            Unified diff string, or None if diff cannot be generated
        """
        # Validate that we have the necessary information
        if not item.suggested_code:
            return None

        if not item.commit:
            return None

        # Get the file content at the specified commit
        try:
            file_content = self._get_file_at_commit(item.file, item.commit)
        except Exception:
            # If we can't get the file at that commit, return None
            return None

        # Split into lines
        original_lines = file_content.splitlines(keepends=True)

        # Validate line numbers
        if item.line_start < 1 or item.line_end > len(original_lines):
            return None

        # Create modified version by replacing the lines
        modified_lines = self._apply_suggestion(original_lines, item)

        # Generate unified diff
        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{item.file}",
            tofile=f"b/{item.file}",
            lineterm='',
            n=context_lines
        )

        diff_text = '\n'.join(diff)

        # Return None if diff is empty (no actual changes)
        if not diff_text.strip():
            return None

        return diff_text

    def _get_file_at_commit(self, file_path: str, commit: str) -> str:
        """Get file content at a specific commit.

        Args:
            file_path: Path to file relative to repository root
            commit: Commit SHA

        Returns:
            File content as string

        Raises:
            RuntimeError: If file cannot be retrieved
        """
        try:
            # Use git show to get file at commit
            content = self.git_ops.repo.git.show(f"{commit}:{file_path}")
            return content
        except Exception as e:
            raise RuntimeError(f"Failed to get {file_path} at {commit}: {e}") from e

    def _apply_suggestion(self, original_lines: list, item: FeedbackItem) -> list:
        """Apply suggested changes to original lines.

        Args:
            original_lines: List of original file lines (with line endings)
            item: FeedbackItem with line range and suggested code

        Returns:
            List of modified lines
        """
        # Convert to 0-based indexing
        start_idx = item.line_start - 1
        end_idx = item.line_end

        # Split suggested code into lines, preserving line endings
        suggested_lines = item.suggested_code.splitlines(keepends=True)

        # Ensure last line has newline if original did
        if suggested_lines and not suggested_lines[-1].endswith('\n'):
            if end_idx < len(original_lines) or (start_idx < len(original_lines) and original_lines[start_idx].endswith('\n')):
                suggested_lines[-1] += '\n'

        # Build modified content: before + suggested + after
        modified_lines = (
            original_lines[:start_idx] +
            suggested_lines +
            original_lines[end_idx:]
        )

        return modified_lines
