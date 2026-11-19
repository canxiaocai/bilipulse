import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional


class LiveEventBus:
    """In-process pub/sub event bus powering Server-Sent Events (SSE) live dashboard sync."""

    def __init__(self, max_queue_size: int = 32) -> None:
        self._max_queue_size = max(1, int(max_queue_size))
        self._subscribers: set[asyncio.Queue[Optional[dict[str, Any]]]] = set()
        self._revision: int = 1
        self._closed: bool = False

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(
        self,
        kind: str,
        *,
        uid: Optional[str] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Increment revision and broadcast an update event to active SSE subscribers."""
        self._revision += 1
        event: dict[str, Any] = {
            "rev": self._revision,
            "kind": kind,
            "uid": (uid or "").strip(),
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        if extra:
            event.update(extra)

        if self._closed or not self._subscribers:
            return event

        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        return event

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Optional[dict[str, Any]]]]:
        queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(
            maxsize=self._max_queue_size
        )
        if self._closed:
            queue.put_nowait(None)
            yield queue
            return

        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def close_all(self) -> None:
        """Signal all active SSE streams to close cleanly during app shutdown."""
        self._closed = True
        for queue in tuple(self._subscribers):
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
