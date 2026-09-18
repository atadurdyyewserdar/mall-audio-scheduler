from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from mutagen import File as MutagenFile

from .models import Schedule


@lru_cache(maxsize=4096)
def audio_duration_seconds(path: str) -> float:
    """Read an audio duration without decoding the file during UI refreshes."""
    try:
        audio = MutagenFile(path)
        # `if audio` is False for a file that carries no tags, whatever its
        # format - which is most WAV announcements and any untagged MP3 - so the
        # duration has to be guarded on the parse succeeding, not on tags.
        if audio is None or audio.info is None:
            return 0.0
        return float(audio.info.length)
    except Exception:
        return 0.0


def next_schedule_time(schedule: Schedule, now: datetime) -> datetime | None:
    """Find the next permitted occurrence for a one-off or recurring schedule."""
    if not schedule.enabled:
        return None
    active_days = {int(day) for day in schedule.weekdays.split(",") if day}
    for day_offset in range(8):
        date = now + timedelta(days=day_offset)
        if date.weekday() not in active_days:
            continue
        candidate = date.replace(hour=int(schedule.time_of_day[:2]), minute=int(schedule.time_of_day[3:]), second=0, microsecond=0)
        if schedule.repeat_minutes:
            interval = timedelta(minutes=schedule.repeat_minutes)
            while candidate < now:
                candidate += interval
        if candidate < now:
            continue
        if schedule.active_from and candidate.strftime("%H:%M") < schedule.active_from:
            candidate = candidate.replace(hour=int(schedule.active_from[:2]), minute=int(schedule.active_from[3:]))
        if schedule.active_until and candidate.strftime("%H:%M") > schedule.active_until:
            continue
        return candidate
    return None


def upcoming_occurrences(schedules: list[Schedule], now: datetime, count: int) -> list[datetime]:
    """The next `count` moments any rule fires, soonest first, across all rules."""
    moments: list[datetime] = []
    cursor = now
    while len(moments) < count:
        candidates = [moment for rule in schedules for moment in (next_schedule_time(rule, cursor),) if moment]
        if not candidates:
            break
        soonest = min(candidates)
        moments.append(soonest)
        cursor = soonest + timedelta(minutes=1)
    return moments


def voice_ad_start_times(schedules: list[Schedule], now: datetime, ordered_ids: list[int]) -> dict[int, datetime]:
    """When each recording next plays. They all play at every scheduled moment,
    one after another, so every one of them is next at the soonest moment."""
    moments = upcoming_occurrences(schedules, now, 1)
    return {item_id: moments[0] for item_id in ordered_ids} if moments else {}
