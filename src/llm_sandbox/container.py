"""Podman container management via REST API."""

import asyncio
import io
import json
import os
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

import requests_unixsocket
from requests_futures.sessions import FuturesSession

from llm_sandbox.events import EventEmitter
from llm_sandbox.models import ContainerInfo


# Exceptions

class PodmanConnectionError(Exception):
    """Cannot connect to podman API."""

    def __init__(self, socket_path: str, original_error: Exception):
        self.socket_path = socket_path
        self.original_error = original_error
        super().__init__(f"Cannot connect to podman API at {socket_path}: {original_error}")


# Image dataclass

@dataclass
class Image:
    """Represents a single, existing container image."""

    tag: str
    """Image tag (e.g., 'python:3.11' or 'my-image:latest')"""

    image_id: str
    """Image ID (hash)"""

    created_at: datetime
    """When the image was created"""

    size: Optional[int] = None
    """Image size in bytes"""


# ContainerManager event types

class ImagePullState(Enum):
    """State of an image pull operation."""
    STARTED = "started"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ImagePullProgress:
    """Progress update for image pull operation."""
    state: ImagePullState
    reference: str
    message: Optional[str] = None
    error: Optional[str] = None


class ImageBuildState(Enum):
    """State of an image build operation."""
    STARTED = "started"
    BUILDING = "building"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ImageBuildProgress:
    """Progress update for image build operation."""
    state: ImageBuildState
    tag: str
    log_line: Optional[str] = None
    image_id: Optional[str] = None
    error: Optional[str] = None


class ContainerManager:
    """Manages podman containers for isolated execution via REST API."""

    def __init__(self):
        """Initialize container manager."""
        self.session = requests_unixsocket.Session()
        self.socket_path = self._get_podman_socket_path()
        self.base_url = f"http+unix://{self.socket_path.replace('/', '%2F')}"

        # Create async session using FuturesSession with ThreadPoolExecutor
        self.async_session = FuturesSession(
            executor=ThreadPoolExecutor(max_workers=10),
            session=requests_unixsocket.Session()
        )

        # Event emitter for progress and status updates
        self.events = EventEmitter()

        self._check_podman()

    def _get_podman_socket_path(self) -> str:
        """Get the podman socket path (rootless only)."""
        uid = os.getuid()
        return f"/run/user/{uid}/podman/podman.sock"

    def _check_podman(self) -> None:
        """Check if podman API is available.

        Raises:
            PodmanConnectionError: If cannot connect to podman API
        """
        try:
            response = self.session.get(f"{self.base_url}/v4.0.0/libpod/version")
            response.raise_for_status()
        except Exception as e:
            raise PodmanConnectionError(self.socket_path, e) from e

    def build_image(
        self,
        containerfile_path: Path,
        context_path: Path,
        tag: str,
    ) -> Image:
        """
        Build container image.

        Args:
            containerfile_path: Path to Containerfile
            context_path: Build context directory
            tag: Image tag

        Returns:
            Image object for the built image
        """
        self.events.emit(ImageBuildProgress(
            state=ImageBuildState.STARTED,
            tag=tag
        ))

        # Create tar archive of build context
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w:gz') as tar:
            tar.add(context_path, arcname='.')

        tar_buffer.seek(0)

        # Build image via API
        containerfile_relative = containerfile_path.relative_to(context_path)
        params = {
            't': tag,
            'dockerfile': str(containerfile_relative),
        }

        try:
            response = self.session.post(
                f"{self.base_url}/v4.0.0/libpod/build",
                params=params,
                data=tar_buffer,
                headers={'Content-Type': 'application/x-tar'},
                stream=True,
            )
            response.raise_for_status()

            # Parse build output to get image ID
            image_id = None
            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        if 'stream' in data:
                            # Emit progress event with build log line
                            self.events.emit(ImageBuildProgress(
                                state=ImageBuildState.BUILDING,
                                tag=tag,
                                log_line=data['stream']
                            ))
                        if 'aux' in data and 'ID' in data['aux']:
                            image_id = data['aux']['ID']
                    except json.JSONDecodeError:
                        continue

            if not image_id:
                # Fallback: get image ID from tag
                image_id = self._get_image_id(tag)

            # Get full image info
            image = self.get_image(tag)
            if not image:
                raise RuntimeError(f"Image built but not found: {tag}")

            self.events.emit(ImageBuildProgress(
                state=ImageBuildState.COMPLETED,
                tag=tag,
                image_id=image.image_id
            ))

            return image

        except Exception as e:
            self.events.emit(ImageBuildProgress(
                state=ImageBuildState.FAILED,
                tag=tag,
                error=str(e)
            ))
            raise RuntimeError(f"Failed to build image: {e}") from e

    def _get_image_id(self, tag: str) -> str:
        """Get image ID from tag."""
        try:
            response = self.session.get(
                f"{self.base_url}/v4.0.0/libpod/images/{tag}/json"
            )
            response.raise_for_status()
            data = response.json()
            return data['Id']
        except Exception as e:
            raise RuntimeError(f"Image not found: {tag}") from e

    def get_image(self, tag: str) -> Optional[Image]:
        """
        Get image information if it exists locally.

        Args:
            tag: Image tag to look up

        Returns:
            Image object if found, None otherwise
        """
        try:
            response = self.session.get(
                f"{self.base_url}/v4.0.0/libpod/images/{tag}/json"
            )
            response.raise_for_status()
            data = response.json()

            # Parse created timestamp
            created_str = data['Created']
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))

            return Image(
                tag=tag,
                image_id=data['Id'],
                created_at=created_dt,
                size=data.get('Size')
            )
        except Exception:
            return None

    def create_container(
        self,
        image_id: str,
        project_mount: Path,
        worktrees_mount: Path,
        network: str = "none",
    ) -> ContainerInfo:
        """
        Create container with mounts.

        Args:
            image_id: Container image ID or tag
            project_mount: Project directory to mount (read-only)
            worktrees_mount: Worktrees directory to mount (read-write, can be empty)
            network: Network mode ("none" or "bridge")

        Returns:
            ContainerInfo with container details

        Raises:
            RuntimeError: If image doesn't exist or container creation fails
        """
        # Check if image exists, error out if not
        if not self.image_exists(image_id):
            raise RuntimeError(
                f"Image '{image_id}' not found locally. "
                f"Pull the image first using pull_image() or build it using build_image()."
            )

        config = {
            "image": image_id,
            "command": ["sleep", "infinity"],
            "work_dir": "/worktrees",
            "mounts": [
                {
                    "type": "bind",
                    "source": str(project_mount.absolute()),
                    "destination": "/project",
                    "options": ["ro", "z"],
                },
                {
                    "type": "bind",
                    "source": str(worktrees_mount.absolute()),
                    "destination": "/worktrees",
                    "options": ["rw", "z"],
                },
            ],
            "netns": {
                "nsmode": network,
            },
            "remove": True,  # Auto-remove on stop
        }

        try:
            response = self.session.post(
                f"{self.base_url}/v4.0.0/libpod/containers/create",
                json=config,
            )
            response.raise_for_status()
            data = response.json()
            container_id = data['Id']

            return ContainerInfo(
                container_id=container_id,
                image=image_id,
                created_at=datetime.now(),
                project_mount=project_mount,
                worktrees_mount=worktrees_mount,
                network=network,
            )

        except Exception as e:
            raise RuntimeError(f"Failed to create container: {e}") from e

    def start_container(self, container_id: str) -> None:
        """Start container."""
        try:
            response = self.session.post(
                f"{self.base_url}/v4.0.0/libpod/containers/{container_id}/start"
            )
            response.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Failed to start container: {e}") from e

    def stop_container(self, container_id: str) -> None:
        """Stop container."""
        try:
            response = self.session.post(
                f"{self.base_url}/v4.0.0/libpod/containers/{container_id}/stop",
                params={'t': 10},
            )
            # Don't raise on error, continue cleanup
        except Exception:
            pass

    def remove_container(self, container_id: str) -> None:
        """Remove container."""
        try:
            response = self.session.delete(
                f"{self.base_url}/v4.0.0/libpod/containers/{container_id}",
                params={'force': True},
            )
            # Don't raise on error, continue cleanup
        except Exception:
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
        try:
            response = self.session.get(
                f"{self.base_url}/v4.0.0/libpod/images/{tag}/exists"
            )
            return response.status_code == 204
        except Exception:
            return False

    def pull_image(self, reference: str) -> Image:
        """
        Pull container image from registry.

        Args:
            reference: Image reference (e.g., "docker.io/library/python:3.11")

        Returns:
            Image object for the pulled image

        Raises:
            RuntimeError: If image pull fails
        """
        self.events.emit(ImagePullProgress(
            state=ImagePullState.STARTED,
            reference=reference
        ))

        try:
            response = self.session.post(
                f"{self.base_url}/v4.0.0/libpod/images/pull",
                params={"reference": reference},
                stream=True,
            )
            response.raise_for_status()

            # Stream progress output
            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        if 'stream' in data:
                            progress_text = data['stream'].rstrip()
                            self.events.emit(ImagePullProgress(
                                state=ImagePullState.DOWNLOADING,
                                reference=reference,
                                message=progress_text
                            ))
                        elif 'id' in data:
                            # Show progress for layers
                            progress_text = f"{data.get('id', '')}: {data.get('status', '')}"
                            self.events.emit(ImagePullProgress(
                                state=ImagePullState.DOWNLOADING,
                                reference=reference,
                                message=progress_text
                            ))
                    except json.JSONDecodeError:
                        continue

            # Get image info after pulling
            image = self.get_image(reference)
            if not image:
                raise RuntimeError(f"Image pulled but not found: {reference}")

            self.events.emit(ImagePullProgress(
                state=ImagePullState.COMPLETED,
                reference=reference
            ))

            return image

        except Exception as e:
            self.events.emit(ImagePullProgress(
                state=ImagePullState.FAILED,
                reference=reference,
                error=str(e)
            ))
            raise RuntimeError(f"Failed to pull image: {e}") from e

    def get_image_created_time(self, tag: str) -> float:
        """
        Get image creation timestamp.

        Args:
            tag: Image tag

        Returns:
            Unix timestamp of image creation

        Raises:
            RuntimeError: If cannot get image info
        """
        try:
            response = self.session.get(
                f"{self.base_url}/v4.0.0/libpod/images/{tag}/json"
            )
            response.raise_for_status()
            data = response.json()

            # Get Created timestamp (RFC3339 format)
            created_str = data['Created']

            # Parse RFC3339 timestamp to Unix timestamp
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            return created_dt.timestamp()

        except Exception as e:
            raise RuntimeError(f"Failed to get image info: {e}") from e

    # Async version using FuturesSession

    async def exec_command(
        self,
        container_id: str,
        command: str,
        workdir: str = "/workspace",
    ) -> Tuple[int, str, str]:
        """
        Execute command in container using FuturesSession.

        Args:
            container_id: Container ID
            command: Command to execute (no filtering applied)
            workdir: Working directory for command

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        # Step 1: Create exec instance
        exec_config = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Cmd": ["sh", "-c", command],
            "WorkingDir": workdir,
        }

        try:
            # Create exec instance (async)
            future = self.async_session.post(
                f"{self.base_url}/v4.0.0/libpod/containers/{container_id}/exec",
                json=exec_config,
            )
            response = await asyncio.wrap_future(future)
            response.raise_for_status()
            exec_id = response.json()['Id']

            # Step 2: Start exec instance and get output (async)
            start_config = {
                "Detach": False,
                "Tty": False,
            }
            future = self.async_session.post(
                f"{self.base_url}/v4.0.0/libpod/exec/{exec_id}/start",
                json=start_config,
            )
            response = await asyncio.wrap_future(future)
            response.raise_for_status()

            # Get all content (this is blocking, so wrap in thread)
            raw_content = await asyncio.to_thread(lambda: response.content)

            # Parse the stream format
            stdout_data = []
            stderr_data = []

            i = 0
            while i < len(raw_content):
                if i + 8 > len(raw_content):
                    break

                # Podman uses Docker's stream format:
                # [8]byte header: 1 byte stream type, 3 bytes padding, 4 bytes size
                # followed by payload
                stream_type = raw_content[i]
                size = int.from_bytes(raw_content[i+4:i+8], 'big')

                if i + 8 + size > len(raw_content):
                    break

                payload = raw_content[i+8:i+8+size]

                if stream_type == 1:  # stdout
                    stdout_data.append(payload)
                elif stream_type == 2:  # stderr
                    stderr_data.append(payload)

                i += 8 + size

            stdout = b''.join(stdout_data).decode('utf-8', errors='replace')
            stderr = b''.join(stderr_data).decode('utf-8', errors='replace')

            # Step 3: Get exit code (async)
            future = self.async_session.get(
                f"{self.base_url}/v4.0.0/libpod/exec/{exec_id}/json"
            )
            inspect_response = await asyncio.wrap_future(future)
            inspect_response.raise_for_status()
            exit_code = inspect_response.json().get('ExitCode', 0)

            return exit_code, stdout, stderr

        except Exception as e:
            return 1, "", f"Failed to execute command: {e}"
