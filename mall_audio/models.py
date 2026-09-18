from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class AudioItem:
    id: int
    name: str
    path: Path
    kind: str


@dataclass(frozen=True)
class Schedule:
    """One playback rule for the whole voice-ad playlist.

    A rule says *when*; *what* plays is the next recording in list order, so
    the recordings take turns rather than each needing its own rule.
    """

    id: int
    time_of_day: str
    weekdays: str
    repeat_minutes: int | None
    active_from: str | None
    active_until: str | None
    enabled: bool

    def is_due(self, now: datetime, last_fired: datetime | None) -> bool:
        if not self.enabled or str(now.weekday()) not in self.weekdays.split(","):
            return False
        scheduled = now.replace(
            hour=int(self.time_of_day[:2]), minute=int(self.time_of_day[3:]), second=0, microsecond=0
        )
        if now < scheduled:
            return False
        if self.active_from and now.strftime("%H:%M") < self.active_from:
            return False
        if self.active_until and now.strftime("%H:%M") > self.active_until:
            return False
        interval_seconds = (self.repeat_minutes or 24 * 60) * 60
        elapsed = (now - scheduled).total_seconds()
        occurrence = scheduled + timedelta(seconds=(elapsed // interval_seconds) * interval_seconds)
        # The one-minute window makes the check resilient to the scheduler's one-second tick,
        # while the persisted last_fired timestamp prevents duplicate plays after a restart.
        return (now - occurrence).total_seconds() < 60 and (last_fired is None or last_fired < occurrence)
