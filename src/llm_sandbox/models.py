"""Data models for business logic return values.

This module contains dataclasses representing structured data returned by
business logic methods. These replace raw strings and primitives, making
the code more type-safe and self-documenting.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


# Container-related models
@dataclass
class ContainerInfo:
    """Information about a created container."""

    container_id: str
    image: str
    created_at: datetime
    project_mount: Path
    worktrees_mount: Path
    network: str


@dataclass
class ImageInfo:
    """Information about a container image."""

    image_id: str
    tag: str
    created_at: datetime


# Git/Worktree-related models
@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    path: Path
    branch: str
    commit: str


# Runner result models
@dataclass
class SetupResult:
    """Result of sandbox setup operation."""

    instance_id: str
    container_id: str
    worktrees: List[WorktreeInfo] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class CleanupResult:
    """Result of cleanup operation."""

    worktrees_removed: int
    branches_deleted: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
