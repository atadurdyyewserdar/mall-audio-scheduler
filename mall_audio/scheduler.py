from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from .database import Database
from .models import Schedule


class Scheduler(QObject):
    announcement_due = Signal(int, str)

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database
        self.last_fired = database.last_schedule_runs()
        self._schedules: list[Schedule] = []
        self._schedules_seen = -1
        self._announcements = []
        self._announcements_seen = -1
        self.timer = QTimer(self)
        self.timer.setInterval(1_000)
        self.timer.timeout.connect(self.check)

    def start(self) -> None:
        self.timer.start()

    def _current_schedules(self) -> list[Schedule]:
        """Re-read schedules only when something actually changed them.

        The tick runs 86,400 times a day; re-running the join each time is pure
        waste when the schedule set changes a few times a month.
        """
        if self._schedules_seen != self.database.catalog_revision:
            self._schedules = self.database.schedules()
            self._schedules_seen = self.database.catalog_revision
        return self._schedules

    def _current_announcements(self):
        if self._announcements_seen != self.database.catalog_revision:
            self._announcements = self.database.audio_items("announcement")
            self._announcements_seen = self.database.catalog_revision
        return self._announcements

    def check(self) -> None:
        now = datetime.now()
        for schedule in self._current_schedules():
            if not schedule.is_due(now, self.last_fired.get(schedule.id)):
                continue
            self.last_fired[schedule.id] = now
            self.database.record_schedule_run(schedule.id, now)
            # The whole list goes out, back to back, in list order. The player
            # starts the first and queues the rest; a recording that has gone
            # missing is simply left out of that run.
            for item in self._current_announcements():
                if item.path.exists():
                    self.announcement_due.emit(item.id, item.name)
