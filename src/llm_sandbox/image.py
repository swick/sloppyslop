"""Container image management."""

from pathlib import Path
from typing import Optional

import click

from llm_sandbox.config import ImageConfig
from llm_sandbox.container import ContainerManager


class Image:
    """Manages container image lifecycle - checking, building, and rebuilding."""

    # Default image to use when no image or build config is specified
    DEFAULT_IMAGE = "registry.fedoraproject.org/fedora-toolbox"

    def __init__(
        self,
        image_config: ImageConfig,
        project_path: Path,
        container_manager: ContainerManager,
    ):
        """
        Initialize image manager.

        Args:
            image_config: Image configuration
            project_path: Path to project directory
            container_manager: Container manager for building images
        """
        self.config = image_config
        self.project_path = project_path
        self.container_manager = container_manager

    def get_image(self) -> str:
        """
        Get image name/tag, building if necessary.

        Returns:
            Image name or tag to use with container manager

        Raises:
            RuntimeError: If build is required but fails
        """
        if self.config.build is not None:
            # Build mode: build from Containerfile
            return self._ensure_built_image()
        else:
            # Pre-built mode: use specified image or default
            image = self.config.image or self.DEFAULT_IMAGE
            click.echo(f"Using image: {image}")
            return image

    def build(self, force: bool = False) -> str:
        """
        Build image from Containerfile.

        Args:
            force: If True, force rebuild even if image is up-to-date

        Returns:
            Image tag

        Raises:
            RuntimeError: If no build configuration or build fails
        """
        if self.config.build is None:
            raise RuntimeError(
                "No build configuration found. Project is configured to use a pre-built image.\n"
                f"Run 'llm-sandbox gen-containerfile <image-name>' to set up a Containerfile."
            )

        return self._ensure_built_image(force_rebuild=force)

    def _ensure_built_image(self, force_rebuild: bool = False) -> str:
        """
        Ensure built image exists, building/rebuilding as needed.

        Args:
            force_rebuild: If True, always rebuild regardless of auto_rebuild setting

        Returns:
            Image tag
        """
        build_config = self.config.build
        containerfile_path = self.project_path / build_config.containerfile

        # Validate Containerfile exists
        if not containerfile_path.exists():
            raise RuntimeError(
                f"Containerfile not found: {containerfile_path}\n"
                f"Run 'llm-sandbox gen-containerfile <image-name>' to set up the project."
            )

        # Get image tag from configuration
        if not self.config.image:
            raise RuntimeError(
                "No image name configured. Run 'llm-sandbox gen-containerfile <image-name>' to configure."
            )
        image_tag = self.config.image

        # Determine if we should build
        should_build = False

        if force_rebuild:
            # Force rebuild requested
            should_build = True
            reason = "Forced rebuild"
        else:
            # Check if image exists
            image_exists = self.container_manager.image_exists(image_tag)

            if not image_exists:
                # Image doesn't exist, must build
                should_build = True
                reason = "Image does not exist"
            elif build_config.auto_rebuild:
                # auto_rebuild is enabled, check if Containerfile is newer
                if self._is_containerfile_newer(image_tag, containerfile_path):
                    should_build = True
                    reason = "Containerfile is newer than image"
                else:
                    reason = "Image is up-to-date"
            else:
                # auto_rebuild is disabled, use existing image
                reason = "Using cached image (auto_rebuild disabled)"

        # Build if needed
        if should_build:
            click.echo(f"Building image: {image_tag} ({reason})")
            self.container_manager.build_image(
                containerfile_path,
                self.project_path,
                image_tag,
            )
        else:
            click.echo(f"Using image: {image_tag} ({reason})")

        return image_tag

    def _is_containerfile_newer(self, image_tag: str, containerfile_path: Path) -> bool:
        """
        Check if Containerfile is newer than the image.

        Args:
            image_tag: Image tag to check
            containerfile_path: Path to Containerfile

        Returns:
            True if Containerfile is newer, False otherwise
        """
        try:
            # Get image creation time from container manager
            image_created = self.container_manager.get_image_created_time(image_tag)

            # Get Containerfile modification time
            containerfile_mtime = containerfile_path.stat().st_mtime

            # Compare: if Containerfile modified after image created, rebuild
            return containerfile_mtime > image_created

        except Exception as e:
            # If we can't determine, err on the side of rebuilding
            click.echo(f"Warning: Cannot determine image age: {e}")
            return True
