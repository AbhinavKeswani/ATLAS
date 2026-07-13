"""In-memory event pub/sub for live dashboard updates.

Same shape as Vein's TranscriptBus: each WebSocket subscriber gets its own
asyncio.Queue; slow subscribers drop oldest rather than back-pressuring producers.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass


@dataclass
class Event:
    # e.g. "todo_added" | "todo_updated" | "todo_removed" | "pay_updated"
    # | "networth_updated" | "food_added" | "workout_added" | "claude_busy"
    type: str
    data: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=128)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def publish(self, type: str, data: dict | None = None) -> None:
        event = Event(type=type, data=data)
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
