"""Interactive editor for review suggestions."""

import difflib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import click

from llm_sandbox.git_ops import GitOperations
from .diff_generator import FeedbackDiffGenerator
from .models import FeedbackItem, Review


class ReviewEditor:
    """Handles interactive editing of review suggestions."""

    def __init__(self, project_dir: Path, review: Review):
        """Initialize the review editor.

        Args:
            project_dir: Project root directory
            review: Review object containing suggestions
        """
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

        # Create temporary file with summary and diff
        with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(f"# Suggestion: {suggestion_id}\n")
            tmp.write(f"# File: {item.file}:{item.line_start}-{item.line_end}\n")
            tmp.write(f"# Commit: {item.commit}\n")
            tmp.write(f"#\n")
            tmp.write(f"# Reason:\n")
            for line in item.reason.splitlines():
                tmp.write(f"#   {line}\n")
            tmp.write(f"#\n")
            tmp.write(f"# Edit the diff below. Lines starting with # are ignored.\n")
            tmp.write(f"# To cancel editing, exit without saving changes.\n")
            tmp.write(f"\n")
            tmp.write(diff_text)
            tmp.write('\n')

        try:
            # Get original content for comparison
            original_content = self._get_file_content_before_edit(tmp_path)

            # Open editor
            editor = os.environ.get('EDITOR', 'vi')
            subprocess.run([editor, tmp_path], check=True)

            # Read edited content
            with open(tmp_path, 'r') as f:
                edited_content = f.read()

            # Check if file was modified
            if edited_content == original_content:
                click.echo("No changes made")
                return False

            # Parse the edited diff (remove comment lines)
            edited_diff = self._parse_edited_diff(edited_content)
            if not edited_diff:
                click.echo("No valid diff found in edited file")
                return False

            # Apply the edited diff and update the suggestion
            self._apply_edited_diff(item, edited_diff)

            # Update the review
            self.review.feedback[item_index] = item

            click.echo(f"✓ Updated suggestion {suggestion_id}")
            return True

        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _get_file_content_before_edit(self, tmp_path: str) -> str:
        """Get the original content of temp file before editing."""
        with open(tmp_path, 'r') as f:
            return f.read()

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

    def _apply_edited_diff(self, item: FeedbackItem, edited_diff: str) -> None:
        """Apply edited diff and update the suggestion.

        Args:
            item: FeedbackItem to update
            edited_diff: Edited unified diff text

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

        # Find the range of changed lines
        first_changed, last_changed = self._find_changed_range(original_lines, new_lines)

        if first_changed is None or last_changed is None:
            raise RuntimeError("No changes detected in edited diff")

        # Extract the suggested code (changed lines)
        suggested_lines = new_lines[first_changed:last_changed + 1]

        if not suggested_lines:
            raise RuntimeError("No suggested lines extracted from diff")

        # Store old values for debug output
        old_start = item.line_start
        old_end = item.line_end
        old_code_lines = len(item.suggested_code.splitlines()) if item.suggested_code else 0

        # Update suggestion
        item.suggested_code = '\n'.join(suggested_lines)
        item.line_start = first_changed + 1
        item.line_end = last_changed + 1

        # Debug output
        click.echo(f"  Old range: {item.file}:{old_start}-{old_end} ({old_code_lines} lines)")
        click.echo(f"  New range: {item.file}:{item.line_start}-{item.line_end} ({len(suggested_lines)} lines)")

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
        """Find the range of changed lines.

        Args:
            original_lines: Original file lines
            new_lines: Modified file lines

        Returns:
            Tuple of (first_changed_idx, last_changed_idx) or (None, None) if no changes
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
                return (None, None)

        # Find last changed line from bottom
        last_changed = len(new_lines) - 1

        # Work backwards from the end to find where changes stop
        orig_idx = len(original_lines) - 1
        new_idx = len(new_lines) - 1

        while orig_idx >= first_changed and new_idx >= first_changed:
            if original_lines[orig_idx] != new_lines[new_idx]:
                break
            orig_idx -= 1
            new_idx -= 1
            last_changed = new_idx

        return (first_changed, last_changed)
