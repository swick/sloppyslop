"""Tests for the EventEmitter class."""

from dataclasses import dataclass
from datetime import datetime

import pytest

from llm_sandbox.events import EventEmitter


# Test event types
@dataclass
class UserCreated:
    """Test event for user creation."""

    user_id: str
    timestamp: datetime


@dataclass
class EmailSent:
    """Test event for email sending."""

    recipient: str
    subject: str


@dataclass
class OrderPlaced:
    """Test event for order placement."""

    order_id: str
    amount: float


class TestEventEmitter:
    """Test suite for EventEmitter."""

    def test_event_emission_basic(self):
        """Test basic event emission and handling."""
        emitter = EventEmitter()
        received_events = []

        def handler(event: UserCreated):
            received_events.append(event)

        emitter.on(UserCreated, handler)
        event = UserCreated(user_id="123", timestamp=datetime.now())
        emitter.emit(event)

        assert len(received_events) == 1
        assert received_events[0] is event
        assert received_events[0].user_id == "123"

    def test_multiple_handlers(self):
        """Test multiple handlers for the same event type."""
        emitter = EventEmitter()
        handler1_called = []
        handler2_called = []

        def handler1(event: UserCreated):
            handler1_called.append(event)

        def handler2(event: UserCreated):
            handler2_called.append(event)

        emitter.on(UserCreated, handler1)
        emitter.on(UserCreated, handler2)

        event = UserCreated(user_id="456", timestamp=datetime.now())
        emitter.emit(event)

        assert len(handler1_called) == 1
        assert len(handler2_called) == 1
        assert handler1_called[0] is event
        assert handler2_called[0] is event

    def test_multiple_event_types(self):
        """Test handling different event types independently."""
        emitter = EventEmitter()
        user_events = []
        email_events = []

        emitter.on(UserCreated, lambda e: user_events.append(e))
        emitter.on(EmailSent, lambda e: email_events.append(e))

        user_event = UserCreated(user_id="789", timestamp=datetime.now())
        email_event = EmailSent(recipient="test@example.com", subject="Hello")

        emitter.emit(user_event)
        emitter.emit(email_event)

        assert len(user_events) == 1
        assert len(email_events) == 1
        assert user_events[0].user_id == "789"
        assert email_events[0].recipient == "test@example.com"

    def test_no_handlers_no_error(self):
        """Test that emitting with no handlers doesn't cause errors."""
        emitter = EventEmitter()
        event = OrderPlaced(order_id="ORD-001", amount=99.99)

        # Should not raise
        emitter.emit(event)

    def test_unsubscribe(self):
        """Test removing event handlers."""
        emitter = EventEmitter()
        received_events = []

        def handler(event: UserCreated):
            received_events.append(event)

        emitter.on(UserCreated, handler)
        emitter.emit(UserCreated(user_id="1", timestamp=datetime.now()))

        assert len(received_events) == 1

        emitter.off(UserCreated, handler)
        emitter.emit(UserCreated(user_id="2", timestamp=datetime.now()))

        # Should still be 1, not 2
        assert len(received_events) == 1

    def test_unsubscribe_nonexistent_handler(self):
        """Test that removing a non-existent handler raises ValueError."""
        emitter = EventEmitter()

        def handler(event: UserCreated):
            pass

        with pytest.raises(ValueError, match="not registered"):
            emitter.off(UserCreated, handler)

    def test_clear_specific_event_type(self):
        """Test clearing handlers for a specific event type."""
        emitter = EventEmitter()
        user_events = []
        email_events = []

        emitter.on(UserCreated, lambda e: user_events.append(e))
        emitter.on(UserCreated, lambda e: user_events.append(e))  # Second handler
        emitter.on(EmailSent, lambda e: email_events.append(e))

        assert emitter.listener_count(UserCreated) == 2
        assert emitter.listener_count(EmailSent) == 1

        emitter.clear(UserCreated)

        assert emitter.listener_count(UserCreated) == 0
        assert emitter.listener_count(EmailSent) == 1

    def test_clear_all_handlers(self):
        """Test clearing all handlers."""
        emitter = EventEmitter()

        emitter.on(UserCreated, lambda e: None)
        emitter.on(EmailSent, lambda e: None)
        emitter.on(OrderPlaced, lambda e: None)

        assert emitter.listener_count(UserCreated) == 1
        assert emitter.listener_count(EmailSent) == 1
        assert emitter.listener_count(OrderPlaced) == 1

        emitter.clear()

        assert emitter.listener_count(UserCreated) == 0
        assert emitter.listener_count(EmailSent) == 0
        assert emitter.listener_count(OrderPlaced) == 0

    def test_listener_count(self):
        """Test counting listeners for event types."""
        emitter = EventEmitter()

        assert emitter.listener_count(UserCreated) == 0

        emitter.on(UserCreated, lambda e: None)
        assert emitter.listener_count(UserCreated) == 1

        emitter.on(UserCreated, lambda e: None)
        assert emitter.listener_count(UserCreated) == 2

    def test_handler_execution_order(self):
        """Test that handlers are called in registration order."""
        emitter = EventEmitter()
        call_order = []

        def handler1(event: UserCreated):
            call_order.append(1)

        def handler2(event: UserCreated):
            call_order.append(2)

        def handler3(event: UserCreated):
            call_order.append(3)

        emitter.on(UserCreated, handler1)
        emitter.on(UserCreated, handler2)
        emitter.on(UserCreated, handler3)

        emitter.emit(UserCreated(user_id="test", timestamp=datetime.now()))

        assert call_order == [1, 2, 3]

    def test_on_returns_handler(self):
        """Test that on() returns the handler for convenience."""
        emitter = EventEmitter()

        def handler(event: UserCreated):
            pass

        # on() should return the handler
        returned = emitter.on(UserCreated, handler)
        assert returned is handler

        # Handler should be registered
        assert emitter.listener_count(UserCreated) == 1
