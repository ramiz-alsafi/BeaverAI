import logging
from typing import Callable, List

logger = logging.getLogger("beaver")

# Type for stream callbacks: func(line: str, stream: 'stdout'|'stderr')
StreamCallback = Callable[[str, str], None]


class EventBus:
    def __init__(self):
        self._listeners: List[StreamCallback] = []

    def subscribe(self, callback: StreamCallback):
        """Attach a TUI logger widget to receive streaming lines."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback: StreamCallback):
        """Detach listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def emit_stream(self, line: str, stream_type: str = "stdout"):
        """Broadcast live stdout/stderr line to all subscribers."""
        for listener in list(self._listeners):
            try:
                listener(line, stream_type)
            except Exception as e:  # FIX-1: log instead of silently discard
                logger.debug(
                    "[TUI_BUS] listener %r raised on emit_stream: %s",
                    listener, e,
                )


# Global Singleton Event Bus
tui_bus = EventBus()