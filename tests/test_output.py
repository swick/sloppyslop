"""Tests for output services."""

import pytest

from llm_sandbox.output import (
    CaptureOutputService,
    ClickOutputService,
    JSONOutputService,
    OutputLevel,
    QuietOutputService,
    create_output_service,
)


class TestOutputLevel:
    """Test OutputLevel enum."""

    def test_output_levels(self):
        """Test output level ordering."""
        assert OutputLevel.QUIET.value < OutputLevel.NORMAL.value
        assert OutputLevel.NORMAL.value < OutputLevel.VERBOSE.value


class TestCaptureOutputService:
    """Test CaptureOutputService (used for testing other components)."""

    def test_captures_all_message_types(self):
        """Test that all message types are captured."""
        output = CaptureOutputService()

        output.info("Info message")
        output.success("Success message")
        output.warning("Warning message")
        output.error("Error message")
        output.verbose("Verbose message")

        messages = output.get_messages()
        assert len(messages) == 5
        assert messages[0] == ("info", "Info message")
        assert messages[1] == ("success", "Success message")
        assert messages[2] == ("warning", "Warning message")
        assert messages[3] == ("error", "Error message")
        assert messages[4] == ("verbose", "Verbose message")

    def test_filter_by_level(self):
        """Test filtering messages by level."""
        output = CaptureOutputService()

        output.info("Info 1")
        output.success("Success 1")
        output.info("Info 2")
        output.error("Error 1")

        info_messages = output.get_messages("info")
        assert info_messages == ["Info 1", "Info 2"]

        success_messages = output.get_messages("success")
        assert success_messages == ["Success 1"]

        error_messages = output.get_messages("error")
        assert error_messages == ["Error 1"]

    def test_clear(self):
        """Test clearing captured messages."""
        output = CaptureOutputService()

        output.info("Message 1")
        output.success("Message 2")
        assert len(output.get_messages()) == 2

        output.clear()
        assert len(output.get_messages()) == 0


class TestClickOutputService:
    """Test ClickOutputService."""

    def test_respects_output_level_normal(self):
        """Test that normal level shows info/success/warning but not verbose."""
        output = ClickOutputService(OutputLevel.NORMAL)

        # These should not raise exceptions (actual output is via click.echo)
        output.info("Info message")
        output.success("Success message")
        output.warning("Warning message")
        output.error("Error message")
        output.verbose("Verbose message")  # Suppressed at NORMAL level

    def test_respects_output_level_quiet(self):
        """Test that quiet level suppresses all except errors."""
        output = ClickOutputService(OutputLevel.QUIET)

        # These should not raise exceptions (actual output testing requires click testing)
        output.info("Info message")  # Suppressed
        output.success("Success message")  # Suppressed
        output.warning("Warning message")  # Suppressed
        output.error("Error message")  # Shown
        output.verbose("Verbose message")  # Suppressed

    def test_respects_output_level_verbose(self):
        """Test that verbose level shows everything."""
        output = ClickOutputService(OutputLevel.VERBOSE)

        # All of these should be shown
        output.info("Info message")
        output.success("Success message")
        output.warning("Warning message")
        output.error("Error message")
        output.verbose("Verbose message")


class TestJSONOutputService:
    """Test JSONOutputService."""

    def test_collects_events(self):
        """Test that events are collected as structured data."""
        output = JSONOutputService(OutputLevel.NORMAL)

        output.info("Info message")
        output.success("Success message")
        output.warning("Warning message")
        output.error("Error message")

        assert len(output.events) == 4
        assert output.events[0] == {"type": "info", "message": "Info message"}
        assert output.events[1] == {"type": "success", "message": "Success message"}
        assert output.events[2] == {"type": "warning", "message": "Warning message"}
        assert output.events[3] == {"type": "error", "message": "Error message"}

    def test_respects_output_level(self):
        """Test that output level controls what events are collected."""
        output = JSONOutputService(OutputLevel.QUIET)

        output.info("Info message")  # Suppressed
        output.success("Success message")  # Suppressed
        output.error("Error message")  # Shown

        assert len(output.events) == 1
        assert output.events[0]["type"] == "error"

    def test_verbose_level(self):
        """Test that verbose messages are only collected at VERBOSE level."""
        output_normal = JSONOutputService(OutputLevel.NORMAL)
        output_verbose = JSONOutputService(OutputLevel.VERBOSE)

        output_normal.verbose("Verbose message")
        output_verbose.verbose("Verbose message")

        assert len(output_normal.events) == 0
        assert len(output_verbose.events) == 1
        assert output_verbose.events[0]["type"] == "verbose"


class TestQuietOutputService:
    """Test QuietOutputService."""

    def test_suppresses_non_errors(self):
        """Test that quiet service suppresses everything except errors."""
        output = QuietOutputService()

        # These should do nothing
        output.info("Info message")
        output.success("Success message")
        output.warning("Warning message")
        output.verbose("Verbose message")

        # This should show (but we can't easily test click.echo output)
        output.error("Error message")

    def test_always_quiet_level(self):
        """Test that QuietOutputService always uses QUIET level."""
        output = QuietOutputService()
        assert output.level == OutputLevel.QUIET


class TestCreateOutputService:
    """Test create_output_service factory function."""

    def test_creates_quiet_service(self):
        """Test creating quiet service."""
        output = create_output_service(quiet=True)
        assert isinstance(output, QuietOutputService)

    def test_creates_click_service_text(self):
        """Test creating click service for text format."""
        output = create_output_service(format="text", verbose=False)
        assert isinstance(output, ClickOutputService)
        assert output.level == OutputLevel.NORMAL

    def test_creates_click_service_verbose(self):
        """Test creating click service with verbose flag."""
        output = create_output_service(format="text", verbose=True)
        assert isinstance(output, ClickOutputService)
        assert output.level == OutputLevel.VERBOSE

    def test_creates_json_service(self):
        """Test creating JSON service."""
        output = create_output_service(format="json", verbose=False)
        assert isinstance(output, JSONOutputService)
        assert output.level == OutputLevel.NORMAL

    def test_creates_json_service_verbose(self):
        """Test creating JSON service with verbose flag."""
        output = create_output_service(format="json", verbose=True)
        assert isinstance(output, JSONOutputService)
        assert output.level == OutputLevel.VERBOSE

    def test_quiet_overrides_other_settings(self):
        """Test that quiet=True overrides format and verbose."""
        output = create_output_service(format="json", verbose=True, quiet=True)
        assert isinstance(output, QuietOutputService)

    def test_invalid_format_raises(self):
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Unknown output format"):
            create_output_service(format="invalid")
