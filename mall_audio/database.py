from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .models import AudioItem, Schedule


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        # An unattended installation writes a log row per playback for months.
        # WAL keeps those writes from blocking reads and avoids a full fsync per
        # commit, which matters on the spinning disks these machines often have.
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()
        # Bumped whenever anything the scheduler reads changes, so its
        # one-second tick can reuse cached lists instead of re-querying.
        self.catalog_revision = 0
        self._logs_since_prune = 0

    def _create_schema(self) -> None:
        self.connection.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS audio_items (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('music', 'announcement')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ad_schedules (
                id INTEGER PRIMARY KEY,
                time_of_day TEXT NOT NULL,
                weekdays TEXT NOT NULL,
                repeat_minutes INTEGER,
                active_from TEXT,
                active_until TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS playback_log (
                id INTEGER PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ad_schedule_runs (
                schedule_id INTEGER PRIMARY KEY REFERENCES ad_schedules(id) ON DELETE CASCADE,
                last_fired TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dismissed_audio (
                path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                dismissed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audio_items_kind ON audio_items(kind, name);
        """)
        self._add_playlist_order()
        self._add_gain_column()
        self._add_loudness_column()
        self._adopt_per_recording_rules()
        self.connection.commit()

    def _adopt_per_recording_rules(self) -> None:
        """Carry an older install's per-recording rules over to playlist rules.

        Rules used to name a recording each. Their times, days and intervals
        are kept and made playlist-wide; the recording they named no longer
        matters, since every recording now takes its turn.
        """
        tables = {row["name"] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "schedules" not in tables:
            return
        self.connection.execute(
            "INSERT INTO ad_schedules(time_of_day, weekdays, repeat_minutes, enabled) "
            "SELECT DISTINCT time_of_day, weekdays, repeat_minutes, enabled FROM schedules"
        )
        self.connection.execute("DROP TABLE IF EXISTS schedule_runs")
        self.connection.execute("DROP TABLE schedules")

    def _add_playlist_order(self) -> None:
        """Give existing installs the hand-ordered playlist column.

        Order used to be alphabetical by name.  Existing rows are backfilled in
        that same order so an upgrade does not visibly reshuffle the playlist.
        """
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(audio_items)")}
        if "position" in columns:
            return
        self.connection.execute("ALTER TABLE audio_items ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
        for kind in ("music", "announcement"):
            rows = self.connection.execute(
                "SELECT id FROM audio_items WHERE kind = ? ORDER BY name", (kind,)
            ).fetchall()
            self.connection.executemany(
                "UPDATE audio_items SET position = ? WHERE id = ?",
                [(index, row["id"]) for index, row in enumerate(rows)],
            )

    def _add_gain_column(self) -> None:
        """Give existing installs the per-recording volume trim column."""
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(audio_items)")}
        if "gain_db" in columns:
            return
        self.connection.execute("ALTER TABLE audio_items ADD COLUMN gain_db REAL NOT NULL DEFAULT 0.0")

    def _add_loudness_column(self) -> None:
        """Measured level for the music normaliser; NULL until a file is scanned."""
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(audio_items)")}
        if "loudness_db" in columns:
            return
        self.connection.execute("ALTER TABLE audio_items ADD COLUMN loudness_db REAL")

    def add_audio(self, path: str, kind: str) -> int:
        return self.add_audio_batch([path], kind)

    def add_audio_batch(self, paths: Iterable[str], kind: str, skip_dismissed: bool = False) -> int:
        """Insert many files under a single commit.

        Importing a mall-sized folder one commit at a time costs an fsync per
        file, which dominates both import and start-up; one transaction makes
        the whole scan effectively free.

        `skip_dismissed` is for the automatic folder scan, which must not undo a
        removal the operator made by hand. An import the operator asked for
        passes False, and takes the file off the dismissed list: asking for a
        file explicitly is how you undo having removed it.
        """
        created_at = datetime.now().isoformat(timespec="seconds")
        start = (self.connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM audio_items WHERE kind = ?", (kind,)
        ).fetchone()["next"])
        resolved = [Path(path).expanduser().resolve() for path in paths]
        if skip_dismissed:
            dismissed = {
                row["path"] for row in self.connection.execute(
                    "SELECT path FROM dismissed_audio WHERE kind = ?", (kind,)
                )
            }
            resolved = [path for path in resolved if str(path) not in dismissed]
        elif resolved:
            with self.connection:
                self.connection.executemany(
                    "DELETE FROM dismissed_audio WHERE path = ? AND kind = ?",
                    [(str(path), kind) for path in resolved],
                )
        rows = [
            (path.stem, str(path), kind, created_at, start + offset)
            for offset, path in enumerate(resolved)
        ]
        if not rows:
            return 0
        with self.connection:
            cursor = self.connection.executemany(
                "INSERT OR IGNORE INTO audio_items(name, path, kind, created_at, position) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        if cursor.rowcount:
            self.catalog_revision += 1
        return cursor.rowcount

    def delete_audio(self, item_id: int) -> None:
        # Remembered, so the folder scan does not put it straight back. Without
        # this, removing a track from a watched folder re-added it immediately.
        row = self.connection.execute(
            "SELECT path, kind FROM audio_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is not None:
            self.connection.execute(
                "INSERT OR REPLACE INTO dismissed_audio(path, kind, dismissed_at) VALUES (?, ?, ?)",
                (row["path"], row["kind"], datetime.now().isoformat(timespec="seconds")),
            )
        self.connection.execute("DELETE FROM audio_items WHERE id = ?", (item_id,))
        self.connection.commit()
        # Cached recording lists are stale.
        self.catalog_revision += 1

    def audio_items(self, kind: str) -> list[AudioItem]:
        rows = self.connection.execute(
            "SELECT * FROM audio_items WHERE kind = ? ORDER BY position, name", (kind,)
        ).fetchall()
        return [AudioItem(r["id"], r["name"], Path(r["path"]), r["kind"], r["gain_db"], r["loudness_db"]) for r in rows]

    def set_audio_gain(self, item_id: int, gain_db: float) -> None:
        self.connection.execute("UPDATE audio_items SET gain_db = ? WHERE id = ?", (gain_db, item_id))
        self.connection.commit()
        self.catalog_revision += 1

    def set_audio_loudness(self, item_id: int, loudness_db: float | None) -> None:
        self.connection.execute("UPDATE audio_items SET loudness_db = ? WHERE id = ?", (loudness_db, item_id))
        self.connection.commit()
        self.catalog_revision += 1

    def reorder_audio(self, kind: str, ordered_ids: list[int]) -> None:
        """Persist a hand-dragged playlist order."""
        with self.connection:
            self.connection.executemany(
                "UPDATE audio_items SET position = ? WHERE id = ? AND kind = ?",
                [(index, item_id, kind) for index, item_id in enumerate(ordered_ids)],
            )
        # The scheduler and the players cache item lists off this revision.
        self.catalog_revision += 1

    def add_schedule(self, time_of_day: str, weekdays: list[int], repeat_minutes: int | None,
                     active_until: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO ad_schedules(time_of_day, weekdays, repeat_minutes, active_until) VALUES (?, ?, ?, ?)",
            (time_of_day, ",".join(map(str, weekdays)), repeat_minutes, active_until),
        )
        self.connection.commit()
        self.catalog_revision += 1

    def delete_schedule(self, schedule_id: int) -> None:
        self.connection.execute("DELETE FROM ad_schedules WHERE id = ?", (schedule_id,))
        self.connection.commit()
        self.catalog_revision += 1

    def last_schedule_runs(self) -> dict[int, datetime]:
        rows = self.connection.execute("SELECT schedule_id, last_fired FROM ad_schedule_runs").fetchall()
        return {row["schedule_id"]: datetime.fromisoformat(row["last_fired"]) for row in rows}

    def record_schedule_run(self, schedule_id: int, fired_at: datetime) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO ad_schedule_runs(schedule_id, last_fired) VALUES (?, ?)",
            (schedule_id, fired_at.isoformat(timespec="seconds")),
        )
        self.connection.commit()

    def schedules(self) -> list[Schedule]:
        rows = self.connection.execute("SELECT * FROM ad_schedules ORDER BY time_of_day").fetchall()
        return [
            Schedule(r["id"], r["time_of_day"], r["weekdays"], r["repeat_minutes"],
                     r["active_from"], r["active_until"], bool(r["enabled"]))
            for r in rows
        ]

    def log(self, event_type: str, message: str) -> None:
        self.connection.execute(
            "INSERT INTO playback_log(occurred_at, event_type, message) VALUES (?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), event_type, message),
        )
        self.connection.commit()
        # Pruning only at start-up bounds nothing on a machine that is never
        # restarted; every few hundred rows keeps the table capped in-flight.
        self._logs_since_prune += 1
        if self._logs_since_prune >= 500:
            self._logs_since_prune = 0
            self.prune_logs()

    def recent_logs(self, limit: int = 40) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM playback_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def prune_logs(self, keep: int = 20_000) -> int:
        """Bound the audit trail so a permanently running install cannot grow forever."""
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM playback_log WHERE id <= "
                "(SELECT id FROM playback_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (keep,),
            )
        return cursor.rowcount

    def setting(self, key: str, default: str) -> str:
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.connection.commit()
