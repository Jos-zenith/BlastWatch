"""In-process event bus for live updates (Server-Sent Events).

Producers run in worker threads (scheduler, sensor ingest, the virtual station), consumers are
SSE connections on the server's event loop, so `publish` hands each message to every subscriber's
loop with call_soon_threadsafe. A short history lets a page that just opened show what happened
in the last minutes, and lets a reconnecting browser catch up with Last-Event-ID.

The bus lives in one process. Behind several worker processes each would have its own bus;
the pilot runs a single process (see Procfile), so that is enough.
"""
import asyncio
import itertools
import json
import threading
from collections import deque
from datetime import date, datetime

HISTORY = 200


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class Bus:
    def __init__(self, history: int = HISTORY):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self.history: deque[dict] = deque(maxlen=history)

    def publish(self, kind: str, **data) -> dict:
        """Thread-safe; never blocks and never raises into the producer."""
        with self._lock:
            event = {"id": next(self._ids), "kind": kind, "at": datetime.now().isoformat(timespec="seconds"),
                     "data": json.loads(json.dumps(data, default=_default))}
            self.history.append(event)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, event)
            except RuntimeError:  # loop closed: the connection is gone
                self._discard(loop, queue)
        return event

    def since(self, last_id: int) -> list[dict]:
        with self._lock:
            return [e for e in self.history if e["id"] > last_id]

    def subscribe(self, maxsize: int = 500) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        entry = (asyncio.get_running_loop(), asyncio.Queue(maxsize=maxsize))
        with self._lock:
            self._subscribers.add(entry)
        return entry

    def _discard(self, loop, queue) -> None:
        with self._lock:
            self._subscribers.discard((loop, queue))

    unsubscribe = _discard

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subscribers)


def _offer(queue: asyncio.Queue, event: dict) -> None:
    # A browser that stops reading must not grow memory without bound: drop its oldest event.
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)


def sse(event: dict) -> str:
    """Unnamed SSE message (the kind is inside the data), so one onmessage handler sees everything."""
    return f"id: {event['id']}\ndata: {json.dumps(event, default=_default)}\n\n"


bus = Bus()
publish = bus.publish
