"""Output service interface and implementations for formatted output.

This module provides an abstraction layer for output handling, separating
business logic from presentation concerns. Different output implementations
can format the same data for different contexts (CLI, JSON, quiet mode, testing).

The design follows the Strategy pattern, allowing the CLI layer to select
the appropriate output strategy based on user flags (--verbose, --quiet, --format).
"""

import json
import sys
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import List, Tuple

import click


class OutputLevel(IntEnum):
    """Output verbosity levels.

    Uses IntEnum to support comparison operators (>=, <=, etc.).
    """

    QUIET = 0  # Errors only
    NORMAL = 1  # Standard output
    VERBOSE = 2  # Detailed output


class OutputService(ABC):
    """Abstract interface for output handling.

    All output in the CLI layer should go through an OutputService implementation,
    never directly to click.echo or print. This allows business logic to remain
    pure and testable.
    """

    def __init__(self, level: OutputLevel):
        """Initialize output service with verbosity level.

        Args:
            level: OutputLevel controlling what messages are displayed
        """
        self.level = level

    @abstractmethod
    def info(self, message: str) -> None:
        """Display normal information message.

        Args:
            message: The message to display
        """
        pass

    @abstractmethod
    def success(self, message: str) -> None:
        """Display success message (typically green/with checkmark).

        Args:
            message: The success message to display
        """
        pass

    @abstractmethod
    def warning(self, message: str) -> None:
        """Display warning message.

        Args:
            message: The warning message to display
        """
        pass

    @abstractmethod
    def error(self, message: str) -> None:
        """Display error message.

        Args:
            message: The error message to display
        """
        pass

    @abstractmethod
    def verbose(self, message: str) -> None:
        """Display verbose-only message.

        Args:
            message: The verbose message to display
        """
        pass


class ClickOutputService(OutputService):
    """Click-based output with styling and colors.

    This is the standard implementation for CLI usage, providing
    colored output with Unicode symbols for better visual feedback.
    """

    def info(self, message: str) -> None:
        """Display info message in normal verbosity."""
        if self.level >= OutputLevel.NORMAL:
            click.echo(message)

    def success(self, message: str) -> None:
        """Display success message with green checkmark."""
        if self.level >= OutputLevel.NORMAL:
            click.secho(f"✓ {message}", fg="green")

    def warning(self, message: str) -> None:
        """Display warning message in yellow."""
        if self.level >= OutputLevel.NORMAL:
            click.secho(f"Warning: {message}", fg="yellow")

    def error(self, message: str) -> None:
        """Display error message in red to stderr."""
        click.secho(f"Error: {message}", fg="red", err=True)

    def verbose(self, message: str) -> None:
        """Display verbose message in dim text."""
        if self.level >= OutputLevel.VERBOSE:
            click.secho(message, dim=True)


class JSONOutputService(OutputService):
    """JSON output service for machine-readable output.

    Collects events as structured data and outputs JSON at the end.
    Useful for integration with other tools or for structured logging.
    """

    def __init__(self, level: OutputLevel):
        """Initialize JSON output service.

        Args:
            level: OutputLevel controlling what messages are collected
        """
        super().__init__(level)
        self.events: List[dict] = []

    def info(self, message: str) -> None:
        """Collect info event."""
        if self.level >= OutputLevel.NORMAL:
            self.events.append({"type": "info", "message": message})

    def success(self, message: str) -> None:
        """Collect success event."""
        if self.level >= OutputLevel.NORMAL:
            self.events.append({"type": "success", "message": message})

    def warning(self, message: str) -> None:
        """Collect warning event."""
        if self.level >= OutputLevel.NORMAL:
            self.events.append({"type": "warning", "message": message})

    def error(self, message: str) -> None:
        """Collect error event."""
        self.events.append({"type": "error", "message": message})

    def verbose(self, message: str) -> None:
        """Collect verbose event."""
        if self.level >= OutputLevel.VERBOSE:
            self.events.append({"type": "verbose", "message": message})

    def flush(self) -> None:
        """Output collected events as JSON to stderr.

        This should be called at the end of execution to output all
        collected events as a JSON array.
        """
        if self.events:
            print(json.dumps(self.events, indent=2), file=sys.stderr)


class QuietOutputService(OutputService):
    """Quiet output service - errors only.

    Suppresses all output except errors. Useful for scripting or when
    only the final result matters.
    """

    def __init__(self):
        """Initialize quiet output service (always QUIET level)."""
        super().__init__(OutputLevel.QUIET)

    def info(self, message: str) -> None:
        """Suppress info messages."""
        pass

    def success(self, message: str) -> None:
        """Suppress success messages."""
        pass

    def warning(self, message: str) -> None:
        """Suppress warning messages."""
        pass

    def error(self, message: str) -> None:
        """Display error to stderr."""
        click.echo(f"Error: {message}", err=True)

    def verbose(self, message: str) -> None:
        """Suppress verbose messages."""
        pass


class CaptureOutputService(OutputService):
    """Capture output service for testing.

    Captures all output messages with their levels, allowing tests
    to verify what would be displayed without actually printing anything.

    Note: Named CaptureOutputService instead of TestOutputService to avoid
    pytest trying to collect it as a test class.
    """

    def __init__(self):
        """Initialize capture output service (always VERBOSE level)."""
        super().__init__(OutputLevel.VERBOSE)
        self.messages: List[Tuple[str, str]] = []

    def info(self, message: str) -> None:
        """Capture info message."""
        self.messages.append(("info", message))

    def success(self, message: str) -> None:
        """Capture success message."""
        self.messages.append(("success", message))

    def warning(self, message: str) -> None:
        """Capture warning message."""
        self.messages.append(("warning", message))

    def error(self, message: str) -> None:
        """Capture error message."""
        self.messages.append(("error", message))

    def verbose(self, message: str) -> None:
        """Capture verbose message."""
        self.messages.append(("verbose", message))

    def get_messages(self, level: str = None) -> List[Tuple[str, str] | str]:
        """Get captured messages, optionally filtered by level.

        Args:
            level: Optional level to filter by (info, success, warning, error, verbose)

        Returns:
            If level is specified, returns list of message strings for that level.
            If level is None, returns list of (level, message) tuples.
        """
        if level:
            return [msg for lvl, msg in self.messages if lvl == level]
        return self.messages

    def clear(self) -> None:
        """Clear all captured messages."""
        self.messages.clear()


def create_output_service(
    format: str = "text", verbose: bool = False, quiet: bool = False
) -> OutputService:
    """Factory function to create the appropriate output service.

    Args:
        format: Output format ("text" or "json")
        verbose: Enable verbose output
        quiet: Enable quiet mode (errors only)

    Returns:
        Configured OutputService instance

    Raises:
        ValueError: If format is not recognized
    """
    if quiet:
        return QuietOutputService()

    level = OutputLevel.VERBOSE if verbose else OutputLevel.NORMAL

    if format == "text":
        return ClickOutputService(level)
    elif format == "json":
        return JSONOutputService(level)
    else:
        raise ValueError(f"Unknown output format: {format}")
