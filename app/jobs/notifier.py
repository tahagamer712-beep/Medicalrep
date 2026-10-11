"""Notifier interface. The scheduler never talks to Telegram directly —
it talks to a Notifier. Production wiring (real Telegram sender) comes later;
tests use FakeNotifier. We never claim a message was delivered unless the
notifier confirms it."""
from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(self, user_id: int, text: str) -> bool:
        """Send a message. Return True only if delivery was confirmed."""
        ...


class FakeNotifier:
    """In-memory notifier for tests. Can simulate failures."""

    def __init__(self):
        self.inbox: dict[int, list[str]] = {}
        self.fail_next: int = 0  # number of upcoming sends to fail
        self.raise_next: int = 0  # number of upcoming sends to raise

    def send(self, user_id: int, text: str) -> bool:
        if self.raise_next > 0:
            self.raise_next -= 1
            raise ConnectionError("simulated send failure")
        if self.fail_next > 0:
            self.fail_next -= 1
            return False
        self.inbox.setdefault(user_id, []).append(text)
        return True
