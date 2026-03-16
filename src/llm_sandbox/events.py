"""Type-safe event emitter system using dataclass events.

This module provides a simple event emitter that uses dataclasses as event types,
enabling type-safe event emission and subscription. Events are defined as dataclasses
in the same file as the class that emits them for better modularity.

Example usage:
    # Define events (in the same file as the emitter)
    @dataclass
    class UserCreated:
        user_id: str
        timestamp: datetime

    @dataclass
    class EmailSent:
        recipient: str
        subject: str

    # Create emitter
    emitter = EventEmitter()

    # Subscribe to events
    def on_user_created(event: UserCreated):
        print(f"User created: {event.user_id}")

    emitter.on(UserCreated, on_user_created)

    # Emit events
    emitter.emit(UserCreated(user_id="123", timestamp=datetime.now()))
"""

from typing import Any, Callable, Dict, List, Type


class EventEmitter:
    """Type-safe event emitter using dataclass events.

    This class allows subscribing to and emitting typed events. Events are
    instances of dataclasses, providing type safety and autocomplete support.
    """

    def __init__(self):
        """Initialize the event emitter with an empty listener registry."""
        self._listeners: Dict[Type, List[Callable]] = {}

    def on(self, event_type: Type, handler: Callable) -> Callable:
        """Subscribe to an event type.

        Args:
            event_type: The dataclass type to listen for (e.g., UserCreated)
            handler: Callable that accepts the event instance as its argument

        Returns:
            The handler (for convenience, allows using as decorator)

        Example:
            @emitter.on(UserCreated)
            def handle_user_created(event: UserCreated):
                print(event.user_id)
        """
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(handler)
        return handler

    def emit(self, event: Any) -> None:
        """Emit a typed event.

        Calls all registered handlers for the event's type in the order
        they were registered.

        Args:
            event: A dataclass instance representing the event

        Example:
            emitter.emit(UserCreated(user_id="123", timestamp=datetime.now()))
        """
        event_type = type(event)
        if event_type in self._listeners:
            for handler in self._listeners[event_type]:
                handler(event)

    def off(self, event_type: Type, handler: Callable) -> None:
        """Unsubscribe from an event type.

        Args:
            event_type: The dataclass type to stop listening to
            handler: The specific handler to remove

        Raises:
            ValueError: If the handler is not registered for this event type
        """
        if event_type not in self._listeners:
            raise ValueError(
                f"Handler {handler} not registered for event type {event_type}"
            )

        try:
            self._listeners[event_type].remove(handler)
        except ValueError:
            raise ValueError(
                f"Handler {handler} not registered for event type {event_type}"
            )

    def clear(self, event_type: Type = None) -> None:
        """Clear all listeners for an event type, or all listeners if no type specified.

        Args:
            event_type: Optional event type to clear. If None, clears all listeners.
        """
        if event_type is None:
            self._listeners.clear()
        elif event_type in self._listeners:
            self._listeners[event_type].clear()

    def listener_count(self, event_type: Type) -> int:
        """Get the number of listeners for an event type.

        Args:
            event_type: The event type to count listeners for

        Returns:
            Number of registered listeners for this event type
        """
        return len(self._listeners.get(event_type, []))
