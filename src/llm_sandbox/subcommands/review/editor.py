"""Interactive editor for review suggestions."""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Callable

from llm_sandbox.git_ops import GitOperations
from .diff_generator import FeedbackDiffGenerator
from .models import FeedbackItem, Review


class Editor:
    """Base class for interactive editing using $EDITOR."""

    def edit(self, initial_content: str, suffix: str = '.txt') -> Optional[str]:
        """Open content in editor and return edited result.

        Args:
            initial_content: Initial content to edit
            suffix: File suffix for temp file

        Returns:
            Edited content if modified, None if unchanged or cancelled
        """
        # Create temp file with initial content
        with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(initial_content)

        try:
            # Get original content for comparison
            with open(tmp_path, 'r') as f:
                original_content = f.read()

            # Open editor
            editor = os.environ.get('EDITOR', 'vi')
            subprocess.run([editor, tmp_path], check=True)

            # Read edited content
            with open(tmp_path, 'r') as f:
                edited_content = f.read()

            # Check if file was modified
            if edited_content == original_content:
                return None

            return edited_content

        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


class ReviewEditor(Editor):
    """Handles interactive editing of review suggestions."""

    def __init__(self, project_dir: Path, review: Review):
        """Initialize the review editor.

        Args:
            project_dir: Project root directory
            review: Review object containing suggestions
        """
        super().__init__()
        self.project_dir = project_dir
        self.review = review
        self.git_ops = GitOperations(project_dir)
        self.diff_generator = FeedbackDiffGenerator(project_dir)

    def edit_suggestion(self, suggestion_id: str) -> bool:
        """Edit a suggestion interactively.

        Args:
            suggestion_id: Short ID of the suggestion to edit

        Returns:
            True if suggestion was modified, False otherwise

        Raises:
            RuntimeError: If editing fails
        """
        # Find the suggestion
        item = None
        item_index = None
        for idx, feedback in enumerate(self.review.feedback):
            if feedback.get_short_id() == suggestion_id:
                item = feedback
                item_index = idx
                break

        if item is None:
            raise RuntimeError(f"Suggestion '{suggestion_id}' not found")

        # Generate the diff
        diff_text = self.diff_generator.generate_diff(item, context_lines=7)
        if not diff_text:
            raise RuntimeError(f"Could not generate diff for suggestion {suggestion_id}")

        # Create initial content for editor
        initial_content = self._format_suggestion_for_edit(item, suggestion_id, diff_text)

        # Open in editor
        edited_content = self.edit(initial_content, suffix='.diff')
        if edited_content is None:
            return False

        # Parse the edited diff (remove comment lines)
        edited_diff = self._parse_edited_diff(edited_content)
        if not edited_diff:
            return False

            # Store original state for comparison
            original_suggested_code = item.suggested_code
            original_range = (item.line_start, item.line_end)

            # Apply both diffs and compare the full resulting files
            original_file_content = self.git_ops.repo.git.show(f"{item.commit}:{item.file}")
            original_result = self._apply_unified_diff(original_file_content, diff_text)
            edited_result = self._apply_unified_diff(original_file_content, edited_diff)

            # Check if the FULL FILE result changed
            full_file_changed = (original_result != edited_result)

            # Apply the edited diff and regenerate the suggestion
            self._apply_edited_diff(item, edited_diff, original_diff=diff_text)

            # Check if the extracted suggestion changed
            code_changed = item.suggested_code != original_suggested_code
            range_changed = (item.line_start, item.line_end) != original_range

            # Update the review
            self.review.feedback[item_index] = item

            if full_file_changed or code_changed or range_changed:
                return True
            else:
                return False

    def edit_review_summary(self) -> bool:
        """Edit the review summary.

        Returns:
            True if modified, False otherwise
        """
        new_summary = self.edit(self.review.summary or "", suffix='.md')

        if new_summary is not None:
            self.review.summary = new_summary
            return True
        else:
            return False

    def edit_item_reason(self, suggestion_id: str) -> bool:
        """Edit a suggestion's reason/summary.

        Args:
            suggestion_id: Short ID of the suggestion

        Returns:
            True if modified, False otherwise

        Raises:
            RuntimeError: If suggestion not found
        """
        # Find the suggestion
        item = None
        for feedback in self.review.feedback:
            if feedback.get_short_id() == suggestion_id:
                item = feedback
                break

        if item is None:
            raise RuntimeError(f"Suggestion '{suggestion_id}' not found")

        # Format initial content
        initial_content = self._format_reason_for_edit(item, suggestion_id)

        # Open in editor
        edited_content = self.edit(initial_content, suffix='.txt')
        if edited_content is None:
            return False

        # Parse edited content (remove comment lines)
        new_reason = self._parse_edited_reason(edited_content)
        if new_reason == item.reason:
            return False

        # Update the item
        item.reason = new_reason
        return True

    def _format_reason_for_edit(self, item: FeedbackItem, suggestion_id: str) -> str:
        """Format item reason for editing.

        Args:
            item: FeedbackItem being edited
            suggestion_id: Short ID of suggestion

        Returns:
            Formatted content for editor
        """
        lines = []
        lines.append(f"# Editing reason for suggestion: {suggestion_id}")
        lines.append(f"# File: {item.file}:{item.line_start}-{item.line_end}")
        lines.append(f"# Category: {item.category}")
        lines.append(f"# Severity: {item.severity}")
        lines.append("#")
        lines.append("# Edit the reason below. Lines starting with # are ignored.")
        lines.append("")
        lines.append(item.reason)
        return '\n'.join(lines)

    def _parse_edited_reason(self, content: str) -> str:
        """Parse edited reason, removing comment lines.

        Args:
            content: Edited file content

        Returns:
            Reason text without comments
        """
        lines = []
        for line in content.splitlines():
            # Skip comment lines
            if line.startswith('#'):
                continue
            lines.append(line)

        return '\n'.join(lines).strip()

    def _format_suggestion_for_edit(self, item: FeedbackItem, suggestion_id: str, diff_text: str) -> str:
        """Format suggestion and diff for editing.

        Args:
            item: FeedbackItem being edited
            suggestion_id: Short ID of suggestion
            diff_text: Unified diff text

        Returns:
            Formatted content for editor
        """
        lines = []
        lines.append(f"# Suggestion: {suggestion_id}")
        lines.append(f"# File: {item.file}:{item.line_start}-{item.line_end}")
        lines.append(f"# Commit: {item.commit}")
        lines.append("#")
        lines.append("# Reason:")
        for line in item.reason.splitlines():
            lines.append(f"#   {line}")
        lines.append("#")
        lines.append("# Edit the diff below. Lines starting with # are ignored.")
        lines.append("# To cancel editing, exit without saving changes.")
        lines.append("")
        lines.append(diff_text)
        lines.append("")
        return '\n'.join(lines)

    def _parse_edited_diff(self, content: str) -> Optional[str]:
        """Parse edited content, removing comment lines.

        Args:
            content: Edited file content

        Returns:
            Unified diff text without comments, or None if invalid
        """
        lines = []
        for line in content.splitlines():
            # Skip comment lines
            if line.startswith('#'):
                continue
            lines.append(line)

        diff_text = '\n'.join(lines).strip()
        if not diff_text:
            return None

        # Verify it looks like a unified diff
        if not diff_text.startswith('---') and not diff_text.startswith('@@'):
            return None

        return diff_text

    def _apply_edited_diff(self, item: FeedbackItem, edited_diff: str, original_diff: Optional[str] = None) -> None:
        """Apply edited diff and update the suggestion.

        Args:
            item: FeedbackItem to update
            edited_diff: Edited unified diff text
            original_diff: Original diff (for comparison/debugging)

        Raises:
            RuntimeError: If diff cannot be applied
        """
        # Get the original file content at the commit
        try:
            original_content = self.git_ops.repo.git.show(f"{item.commit}:{item.file}")
        except Exception as e:
            raise RuntimeError(f"Failed to get {item.file} at {item.commit}: {e}") from e

        original_lines = original_content.splitlines()

        # Parse the unified diff to extract the new content
        try:
            new_content = self._apply_unified_diff(original_content, edited_diff)
        except Exception as e:
            raise RuntimeError(f"Failed to apply diff: {e}") from e

        new_lines = new_content.splitlines()

        # Find the range to replace in original and what to extract from new
        first_orig, last_orig, first_new, last_new = self._find_changed_range(original_lines, new_lines)

        if first_orig is None:
            raise RuntimeError("No changes detected in edited diff")

        # Trim identical lines from the beginning
        while first_orig <= last_orig and first_new <= last_new:
            if original_lines[first_orig] == new_lines[first_new]:
                first_orig += 1
                first_new += 1
            else:
                break

        # Trim identical lines from the end
        while first_orig <= last_orig and first_new <= last_new:
            if original_lines[last_orig] == new_lines[last_new]:
                last_orig -= 1
                last_new -= 1
            else:
                break

        # Check if we trimmed everything (no actual changes)
        if first_orig > last_orig and first_new > last_new:
            raise RuntimeError("No effective changes after trimming identical lines")

        # Extract the suggested code (changed lines from the NEW file)
        suggested_lines = new_lines[first_new:last_new + 1]

        if not suggested_lines:
            raise RuntimeError("No suggested lines extracted from diff")

        # Update suggestion - use ORIGINAL file range, not new file range
        item.suggested_code = '\n'.join(suggested_lines)
        item.line_start = first_orig + 1  # 1-based
        item.line_end = last_orig + 1     # 1-based (inclusive)

    def _apply_unified_diff(self, original: str, diff: str) -> str:
        """Apply a unified diff to original content using difflib-style parsing.

        Args:
            original: Original file content
            diff: Unified diff text

        Returns:
            Modified file content

        Raises:
            RuntimeError: If diff cannot be applied
        """
        original_lines = original.splitlines()
        diff_lines = diff.splitlines()

        # Find all hunks in the diff
        hunks = []
        i = 0
        while i < len(diff_lines):
            line = diff_lines[i]
            if line.startswith('@@'):
                # Parse hunk header
                match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                if match:
                    old_start = int(match.group(1)) - 1  # 0-based
                    old_count = int(match.group(2)) if match.group(2) else 1
                    new_start = int(match.group(3)) - 1  # 0-based
                    new_count = int(match.group(4)) if match.group(4) else 1

                    # Collect hunk lines
                    hunk_lines = []
                    i += 1
                    while i < len(diff_lines) and not diff_lines[i].startswith('@@'):
                        if diff_lines[i].startswith(('---', '+++')):
                            i += 1
                            continue
                        hunk_lines.append(diff_lines[i])
                        i += 1

                    hunks.append({
                        'old_start': old_start,
                        'old_count': old_count,
                        'new_start': new_start,
                        'new_count': new_count,
                        'lines': hunk_lines
                    })
                    continue
            i += 1

        # Apply hunks to reconstruct the file
        result_lines = []
        original_idx = 0

        for hunk in hunks:
            # Copy lines before this hunk
            while original_idx < hunk['old_start']:
                result_lines.append(original_lines[original_idx])
                original_idx += 1

            # Process hunk lines
            for line in hunk['lines']:
                if not line:
                    continue
                if line.startswith(' '):
                    # Context line - appears in both old and new
                    result_lines.append(line[1:])
                    original_idx += 1
                elif line.startswith('-'):
                    # Removed line - skip it in original
                    original_idx += 1
                elif line.startswith('+'):
                    # Added line - add to result
                    result_lines.append(line[1:])

        # Copy any remaining lines after the last hunk
        while original_idx < len(original_lines):
            result_lines.append(original_lines[original_idx])
            original_idx += 1

        return '\n'.join(result_lines)

    def _find_changed_range(self, original_lines: list, new_lines: list) -> tuple:
        """Find the range to replace in original file and what to replace it with.

        We need to find:
        - first_orig, last_orig: range in ORIGINAL file to replace
        - The suggested code is extracted from NEW file such that:
          original[:first_orig] + suggested + original[last_orig+1:] == new

        Args:
            original_lines: Original file lines
            new_lines: Modified file lines

        Returns:
            Tuple of (first_orig_idx, last_orig_idx, first_new_idx, last_new_idx)
            where orig range gets replaced with new range, or (None, None, None, None) if no changes
        """
        # Find first changed line from top
        first_changed = None
        min_len = min(len(original_lines), len(new_lines))

        for i in range(min_len):
            if original_lines[i] != new_lines[i]:
                first_changed = i
                break

        # If all common lines match, check if length differs
        if first_changed is None:
            if len(original_lines) != len(new_lines):
                first_changed = min_len
            else:
                # No changes at all
                return (None, None, None, None)

        # Find last changed line from bottom (work backwards from both ends)
        orig_idx = len(original_lines) - 1
        new_idx = len(new_lines) - 1

        while orig_idx >= first_changed and new_idx >= first_changed:
            if original_lines[orig_idx] != new_lines[new_idx]:
                break
            orig_idx -= 1
            new_idx -= 1

        # Range in original to replace: first_changed to orig_idx (inclusive)
        # Range in new to extract: first_changed to new_idx (inclusive)
        return (first_changed, orig_idx, first_changed, new_idx)
