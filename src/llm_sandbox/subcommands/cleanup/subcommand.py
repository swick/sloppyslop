"""Clean up llm-sandbox worktrees and branches."""

import shutil
import sys
from pathlib import Path

from llm_sandbox.git_ops import GitOperations
from llm_sandbox.output import OutputService
from llm_sandbox.subcommand import Subcommand


class CleanupSubcommand(Subcommand):
    """Clean up all llm-sandbox worktrees and llm-container branches."""

    name = "cleanup"
    help = "Clean up all llm-sandbox worktrees and llm-container branches."

    def execute(self, **kwargs):
        """Execute the cleanup command."""
        from llm_sandbox.output import create_output_service

        project_dir = Path.cwd()
        output = create_output_service(format="text", verbose=False)

        output.info("Cleaning up llm-sandbox worktrees and branches")
        output.info(f"Project directory: {project_dir}")

        try:
            git_ops = GitOperations(project_dir)
        except ValueError as e:
            output.error(str(e))
            sys.exit(1)

        # Find all worktrees under .llm-sandbox/worktrees/
        worktrees_base = project_dir / ".llm-sandbox" / "worktrees"

        if worktrees_base.exists():
            output.info(f"\nRemoving worktrees from: {worktrees_base}")

            # Iterate through instance directories
            for instance_dir in worktrees_base.iterdir():
                if instance_dir.is_dir():
                    output.info(f"  Instance: {instance_dir.name}")

                    # Find all worktree directories recursively (they might be nested)
                    # A directory is a worktree if it has a .git file
                    for worktree_path in instance_dir.rglob("*"):
                        if worktree_path.is_dir() and (worktree_path / ".git").exists():
                            try:
                                # Get relative path from instance dir for display
                                rel_path = worktree_path.relative_to(instance_dir)
                                output.info(f"    Removing worktree: {rel_path}")
                                git_ops.remove_worktree(worktree_path)
                            except Exception as e:
                                output.warning(f"    Failed to remove {rel_path}: {e}")

            # Remove the entire worktrees directory
            try:
                shutil.rmtree(worktrees_base)
                output.success("Removed worktrees directory")
            except Exception as e:
                output.warning(f"Failed to remove worktrees directory: {e}")
        else:
            output.info("\nNo worktrees directory found")

        # Find and delete all llm-container/* branches
        output.info("\nDeleting llm-container branches")

        try:
            # Get all branches
            branches = [ref.name for ref in git_ops.repo.refs if ref.name.startswith("llm-container/")]

            if branches:
                for branch_name in branches:
                    try:
                        output.info(f"  Deleting branch: {branch_name}")
                        git_ops.delete_branch(branch_name)
                    except Exception as e:
                        output.warning(f"  Failed to delete {branch_name}: {e}")

                output.success(f"Deleted {len(branches)} branch(es)")
            else:
                output.info("  No llm-container branches found")

        except Exception as e:
            output.error(f"Error listing branches: {e}")
            sys.exit(1)

        output.success("\nCleanup complete")
