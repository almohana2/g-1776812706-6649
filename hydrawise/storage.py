"""A local run log built from repeated ``statusschedule`` polls.

The v1 REST API exposes *what is watering now*, not *what watered last
month*: there is no history endpoint. To bill anybody for water or power we
therefore have to keep the history ourselves, by polling the controller and
recording each zone's run from the moment it appears in ``running`` to the
moment it disappears again.

SQLite is the whole storage layer — one file, no server, and it is in the
standard library.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .models import StatusSchedule

__all__ = ["RunRecord", "RunEvent", "RunStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    relay_id         INTEGER NOT NULL,
    zone_number      INTEGER,
    zone_name        TEXT,
    controller_id    INTEGER,
    started_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    ended_at         TEXT,
    seconds          INTEGER NOT NULL DEFAULT 0,
    expected_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS runs_started_at ON runs (started_at);
CREATE INDEX IF NOT EXISTS runs_open ON runs (ended_at);

CREATE TABLE IF NOT EXISTS polls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at     TEXT NOT NULL,
    controller_id INTEGER,
    running       INTEGER NOT NULL DEFAULT 0
);
"""


def _iso(moment: datetime) -> str:
    return _utc(moment).isoformat()


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text)


@dataclass
class RunRecord:
    """One watering run of one zone."""

    id: Optional[int]
    relay_id: int
    zone_number: Optional[int]
    zone_name: Optional[str]
    controller_id: Optional[int]
    started_at: datetime
    last_seen_at: datetime
    ended_at: Optional[datetime]
    seconds: int
    expected_seconds: Optional[int]

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def duration(self) -> timedelta:
        return timedelta(seconds=self.seconds)

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RunRecord":
        return cls(
            id=row["id"],
            relay_id=row["relay_id"],
            zone_number=row["zone_number"],
            zone_name=row["zone_name"],
            controller_id=row["controller_id"],
            started_at=_parse(row["started_at"]),  # type: ignore[arg-type]
            last_seen_at=_parse(row["last_seen_at"]),  # type: ignore[arg-type]
            ended_at=_parse(row["ended_at"]),
            seconds=row["seconds"] or 0,
            expected_seconds=row["expected_seconds"],
        )


@dataclass
class RunEvent:
    """Something the tracker noticed during one poll."""

    kind: str  # "started" | "updated" | "finished"
    run: RunRecord


class RunStore:
    """The SQLite-backed run log.

    Usable as a context manager::

        with RunStore("hydrawise.db") as store:
            store.record_status(status, now=utcnow())
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def record_status(
        self, status: StatusSchedule, *, now: datetime, controller_id: Optional[int] = None
    ) -> List[RunEvent]:
        """Fold one ``statusschedule`` response into the run log.

        Zones that appeared since the last poll open a run, zones still
        running extend theirs, and zones that vanished close theirs.
        """
        now = _utc(now)
        controller_id = controller_id if controller_id is not None else status.controller_id
        events: List[RunEvent] = []

        running = {
            item.relay_id: item for item in status.running if item.relay_id is not None
        }
        names = {
            zone.relay_id: zone
            for zone in status.zones
            if zone.relay_id is not None
        }

        for record in self.open_runs():
            if record.relay_id in running:
                continue
            self._close(record, now=now)
            events.append(RunEvent("finished", self.run(record.id)))  # type: ignore[arg-type]

        open_by_relay = {record.relay_id: record for record in self.open_runs()}
        for relay_id, item in running.items():
            zone = names.get(relay_id)
            zone_number = item.number if item.number is not None else (
                zone.number if zone else None
            )
            zone_name = item.name or (zone.name if zone else None)
            expected = item.run_seconds
            existing = open_by_relay.get(relay_id)
            if existing is None:
                started_at = now
                # time_left plus the total run length tells us how far in we
                # already are, so a poller that starts mid-run does not
                # under-count.
                if expected is not None and item.time_left is not None:
                    elapsed = expected - item.time_left
                    if 0 <= elapsed <= expected:
                        started_at = now - timedelta(seconds=elapsed)
                run_id = self._insert(
                    relay_id=relay_id,
                    zone_number=zone_number,
                    zone_name=zone_name,
                    controller_id=controller_id,
                    started_at=started_at,
                    last_seen_at=now,
                    expected_seconds=expected,
                )
                events.append(RunEvent("started", self.run(run_id)))  # type: ignore[arg-type]
            else:
                self._touch(
                    existing,
                    now=now,
                    zone_name=zone_name,
                    zone_number=zone_number,
                    expected_seconds=expected,
                )
                events.append(RunEvent("updated", self.run(existing.id)))  # type: ignore[arg-type]

        self._conn.execute(
            "INSERT INTO polls (polled_at, controller_id, running) VALUES (?, ?, ?)",
            (_iso(now), controller_id, len(running)),
        )
        self._conn.commit()
        return events

    def close_stale(self, *, now: datetime, max_gap_seconds: float = 900.0) -> List[RunRecord]:
        """Close runs whose zone has not been seen for ``max_gap_seconds``.

        A poller that dies mid-run would otherwise leave that run open
        forever and, when it came back, keep extending it. Closing goes
        through the same estimate as a normal finish, so a run whose
        programmed length is known is credited that length at most, and one
        whose length is unknown is credited only the time actually observed.
        """
        now = _utc(now)
        closed: List[RunRecord] = []
        for record in self.open_runs():
            if (now - record.last_seen_at).total_seconds() <= max_gap_seconds:
                continue
            self._close(record, now=now)
            closed.append(self.run(record.id))  # type: ignore[arg-type]
        self._conn.commit()
        return closed

    def _insert(
        self,
        *,
        relay_id: int,
        zone_number: Optional[int],
        zone_name: Optional[str],
        controller_id: Optional[int],
        started_at: datetime,
        last_seen_at: datetime,
        expected_seconds: Optional[int],
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO runs (relay_id, zone_number, zone_name, controller_id,
                              started_at, last_seen_at, ended_at, seconds,
                              expected_seconds)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                relay_id,
                zone_number,
                zone_name,
                controller_id,
                _iso(started_at),
                _iso(last_seen_at),
                int((last_seen_at - started_at).total_seconds()),
                expected_seconds,
            ),
        )
        return int(cursor.lastrowid)

    def _touch(
        self,
        record: RunRecord,
        *,
        now: datetime,
        zone_name: Optional[str],
        zone_number: Optional[int],
        expected_seconds: Optional[int],
    ) -> None:
        seconds = int((now - record.started_at).total_seconds())
        self._conn.execute(
            """
            UPDATE runs
               SET last_seen_at = ?,
                   seconds = ?,
                   zone_name = COALESCE(?, zone_name),
                   zone_number = COALESCE(?, zone_number),
                   expected_seconds = COALESCE(?, expected_seconds)
             WHERE id = ?
            """,
            (
                _iso(now),
                max(0, seconds),
                zone_name,
                zone_number,
                expected_seconds,
                record.id,
            ),
        )

    def _close(self, record: RunRecord, *, now: datetime) -> None:
        """Close a run, estimating what happened inside the last poll gap.

        We know the zone was still running at ``last_seen_at`` and was gone by
        ``now``. When the controller told us how long the run was programmed
        for, and that much time has passed, the run most likely played out in
        full; otherwise all we can defend is the time we actually observed.
        """
        observed = max(0, int((record.last_seen_at - record.started_at).total_seconds()))
        seconds = observed
        expected = record.expected_seconds
        if expected:
            elapsed = int((_utc(now) - record.started_at).total_seconds())
            seconds = max(observed, min(expected, elapsed))
        ended_at = record.started_at + timedelta(seconds=seconds)
        self._conn.execute(
            "UPDATE runs SET ended_at = ?, seconds = ? WHERE id = ?",
            (_iso(ended_at), seconds, record.id),
        )

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def run(self, run_id: int) -> Optional[RunRecord]:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord.from_row(row) if row else None

    def open_runs(self) -> List[RunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE ended_at IS NULL ORDER BY started_at"
        ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def runs_between(self, start: datetime, end: datetime) -> List[RunRecord]:
        """Runs that *started* inside ``[start, end)``.

        A run is billed to the period it started in; a run that straddles
        midnight on the first of the month is not split.
        """
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
            (_iso(start), _iso(end)),
        ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def all_runs(self) -> List[RunRecord]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def last_poll_at(self) -> Optional[datetime]:
        row = self._conn.execute("SELECT MAX(polled_at) AS ts FROM polls").fetchone()
        return _parse(row["ts"]) if row and row["ts"] else None

    def poll_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM polls").fetchone()
        return int(row["n"]) if row else 0

    def zone_names(self) -> List[Tuple[int, Optional[int], Optional[str]]]:
        """Every ``(relay_id, zone_number, zone_name)`` the log has seen."""
        rows = self._conn.execute(
            """
            SELECT relay_id, MAX(zone_number) AS zone_number, MAX(zone_name) AS zone_name
              FROM runs GROUP BY relay_id ORDER BY zone_number, relay_id
            """
        ).fetchall()
        return [(row["relay_id"], row["zone_number"], row["zone_name"]) for row in rows]

    def add_runs(self, records: Iterable[RunRecord]) -> int:
        """Insert closed runs directly — used by importers and by the tests."""
        count = 0
        for record in records:
            self._conn.execute(
                """
                INSERT INTO runs (relay_id, zone_number, zone_name, controller_id,
                                  started_at, last_seen_at, ended_at, seconds,
                                  expected_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.relay_id,
                    record.zone_number,
                    record.zone_name,
                    record.controller_id,
                    _iso(record.started_at),
                    _iso(record.last_seen_at),
                    _iso(record.ended_at) if record.ended_at else None,
                    record.seconds,
                    record.expected_seconds,
                ),
            )
            count += 1
        self._conn.commit()
        return count
