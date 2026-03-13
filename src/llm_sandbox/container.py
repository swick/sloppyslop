"""Podman container management."""

import json
import subprocess
from pathlib import Path
from typing import Optional, Tuple


class ContainerManager:
    """Manages podman containers for isolated execution."""

    def __init__(self):
        """Initialize container manager."""
        self._check_podman()

    def _check_podman(self) -> None:
        """Check if podman is available."""
        try:
            subprocess.run(
                ["podman", "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(
                "podman is not installed or not available. "
                "Please install podman: https://podman.io/getting-started/installation"
            ) from e

    def build_image(
        self,
        containerfile_path: Path,
        context_path: Path,
        tag: str,
    ) -> str:
        """
        Build container image.

        Args:
            containerfile_path: Path to Containerfile
            context_path: Build context directory
            tag: Image tag

        Returns:
            Image ID
        """
        cmd = [
            "podman",
            "build",
            "-f",
            str(containerfile_path),
            "-t",
            tag,
            str(context_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )

            # Get image ID
            image_id = self._get_image_id(tag)
            return image_id

        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to build image: {e.stderr}\n"
                f"Command: {' '.join(cmd)}"
            ) from e

    def _get_image_id(self, tag: str) -> str:
        """Get image ID from tag."""
        cmd = ["podman", "images", "-q", tag]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        image_id = result.stdout.strip()
        if not image_id:
            raise RuntimeError(f"Image not found: {tag}")
        return image_id

    def create_container(
        self,
        image_id: str,
        project_mount: Path,
        worktree_mount: Path,
        network: str = "none",
    ) -> str:
        """
        Create container with mounts.

        Args:
            image_id: Container image ID or tag
            project_mount: Project directory to mount (read-only)
            worktree_mount: Worktree directory to mount (read-write)
            network: Network mode ("none" or "bridge")

        Returns:
            Container ID
        """
        cmd = [
            "podman",
            "create",
            f"--network={network}",
            "-v",
            f"{project_mount}:/project:ro",  # Read-only project mount
            "-v",
            f"{worktree_mount}:/workspace:rw",  # Read-write worktree mount
            "-w",
            "/workspace",  # Set working directory
            "--rm",  # Auto-remove on stop
            image_id,
            "sleep",
            "infinity",  # Keep container running
        ]

        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
            container_id = result.stdout.strip()
            return container_id

        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to create container: {e.stderr}\n"
                f"Command: {' '.join(cmd)}"
            ) from e

    def start_container(self, container_id: str) -> None:
        """Start container."""
        cmd = ["podman", "start", container_id]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to start container: {e.stderr}") from e

    def exec_command(
        self,
        container_id: str,
        command: str,
        workdir: str = "/workspace",
    ) -> Tuple[int, str, str]:
        """
        Execute command in container.

        Args:
            container_id: Container ID
            command: Command to execute (no filtering applied)
            workdir: Working directory for command

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        cmd = [
            "podman",
            "exec",
            "-w",
            workdir,
            container_id,
            "sh",
            "-c",
            command,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        return result.returncode, result.stdout, result.stderr

    def stop_container(self, container_id: str) -> None:
        """Stop container."""
        cmd = ["podman", "stop", container_id, "-t", "10"]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            # Continue even if stop fails
            pass

    def remove_container(self, container_id: str) -> None:
        """Remove container."""
        cmd = ["podman", "rm", "-f", container_id]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            # Continue even if remove fails
            pass

    def cleanup(self, container_id: str) -> None:
        """
        Stop and remove container.

        Args:
            container_id: Container ID to cleanup
        """
        self.stop_container(container_id)
        self.remove_container(container_id)

    def image_exists(self, tag: str) -> bool:
        """Check if image exists."""
        cmd = ["podman", "images", "-q", tag]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return bool(result.stdout.strip())
