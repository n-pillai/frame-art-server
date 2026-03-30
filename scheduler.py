"""
scheduler.py — Time-based art rotation with mood/query scheduling.

Supports two modes:
  1. Interval mode: change art every N hours
  2. Time-of-day mode: different art styles at different times
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional

logger = logging.getLogger("frame_art.scheduler")


class TimeSlot:
    """A time window with associated art preferences."""

    def __init__(self, name: str, start: str, end: str, mood: str, queries: list[str]):
        self.name = name
        self.start = self._parse_time(start)
        self.end = self._parse_time(end)
        self.mood = mood
        self.queries = queries

    @staticmethod
    def _parse_time(t: str) -> dtime:
        parts = t.split(":")
        return dtime(int(parts[0]), int(parts[1]))

    def contains(self, now: dtime) -> bool:
        """Check if a time falls within this slot (handles midnight wraparound)."""
        if self.start <= self.end:
            return self.start <= now <= self.end
        else:
            # Wraps past midnight (e.g., 23:00 → 05:59)
            return now >= self.start or now <= self.end

    def __repr__(self):
        return f"TimeSlot({self.name}: {self.start}-{self.end}, mood={self.mood})"


class ArtScheduler:
    """Manages art rotation based on time-of-day or fixed intervals."""

    def __init__(self, config: dict):
        self.mode = config.get("mode", "interval")
        self.interval_hours = config.get("interval_hours", 4)
        self.time_slots = []
        self._last_slot_name = None
        self._last_change = None

        # Build time slots from config
        slots_config = config.get("time_slots", {})
        for name, slot_data in slots_config.items():
            self.time_slots.append(
                TimeSlot(
                    name=name,
                    start=slot_data["start"],
                    end=slot_data["end"],
                    mood=slot_data.get("mood", ""),
                    queries=slot_data.get("queries", []),
                )
            )

        logger.info(
            f"Scheduler initialized: mode={self.mode}, "
            f"slots={len(self.time_slots)}"
        )

    def get_current_slot(self) -> Optional[TimeSlot]:
        """Get the time slot for the current time."""
        now = datetime.now().time()
        for slot in self.time_slots:
            if slot.contains(now):
                return slot
        return None

    def should_change_art(self) -> bool:
        """Determine if it's time to change the art."""
        now = datetime.now()

        if self.mode == "interval":
            if self._last_change is None:
                return True
            elapsed = (now - self._last_change).total_seconds() / 3600
            return elapsed >= self.interval_hours

        elif self.mode == "time_of_day":
            current_slot = self.get_current_slot()
            if current_slot is None:
                return False

            # Change when we enter a new time slot
            if current_slot.name != self._last_slot_name:
                return True

            # Also change at the interval within a slot
            if self._last_change is not None:
                elapsed = (now - self._last_change).total_seconds() / 3600
                return elapsed >= self.interval_hours

            return True

        return False

    def get_current_queries(self, default_queries: list[str]) -> list[str]:
        """Get the art search queries appropriate for the current time."""
        if self.mode == "time_of_day":
            slot = self.get_current_slot()
            if slot and slot.queries:
                logger.info(f"Using {slot.name} queries (mood: {slot.mood})")
                return slot.queries

        return default_queries

    def mark_changed(self):
        """Record that we just changed the art."""
        self._last_change = datetime.now()
        slot = self.get_current_slot()
        if slot:
            self._last_slot_name = slot.name
        logger.debug(f"Art changed at {self._last_change}")

    def get_status(self) -> dict:
        """Return current scheduler status for logging/debugging."""
        slot = self.get_current_slot()
        return {
            "mode": self.mode,
            "current_slot": slot.name if slot else None,
            "current_mood": slot.mood if slot else None,
            "last_change": str(self._last_change) if self._last_change else None,
            "should_change": self.should_change_art(),
        }
