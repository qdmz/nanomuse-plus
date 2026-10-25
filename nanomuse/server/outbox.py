"""Durable hand-off of scheduled work to the in-memory thread queues."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Delivery:
    id: str
    source_key: str
    kind: str
    source_id: str
    occurrence: str
    thread: str
    text: str
    label: str


class DeliveryOutbox:
    """A claimed occurrence stays pending until its agent run completes.

    Completed rows are retained as the idempotency key. ``finalized`` only means the
    source store's fired/check-in counters were advanced; recovery can finish that step.
    """

    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_deliveries (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                occurrence TEXT NOT NULL,
                thread TEXT NOT NULL,
                text TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                finalized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    def claim(
        self,
        source_key: str,
        *,
        kind: str,
        source_id: str,
        occurrence: str,
        thread: str,
        text: str,
        label: str,
    ) -> Delivery | None:
        delivery_id = "d_" + uuid.uuid4().hex
        try:
            self._conn.execute(
                "INSERT INTO scheduled_deliveries "
                "(id,source_key,kind,source_id,occurrence,thread,text,label,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    source_key,
                    kind,
                    source_id,
                    occurrence,
                    thread,
                    text,
                    label,
                    _now(),
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            return None
        return self.get(delivery_id)

    def get(self, delivery_id: str) -> Delivery | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        return self._delivery(row) if row else None

    def pending(self) -> list[Delivery]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_deliveries WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        return [self._delivery(row) for row in rows]

    def complete(self, delivery_id: str) -> Delivery | None:
        self._conn.execute(
            "UPDATE scheduled_deliveries SET status='done', completed_at=? "
            "WHERE id=? AND status='pending'",
            (_now(), delivery_id),
        )
        self._conn.commit()
        return self.get(delivery_id)

    def unfinalized(self) -> list[Delivery]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_deliveries WHERE status='done' AND finalized=0 "
            "ORDER BY completed_at"
        ).fetchall()
        return [self._delivery(row) for row in rows]

    def finalized(self, delivery_id: str) -> None:
        self._conn.execute(
            "UPDATE scheduled_deliveries SET finalized=1 WHERE id=? AND status='done'",
            (delivery_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            id=row["id"],
            source_key=row["source_key"],
            kind=row["kind"],
            source_id=row["source_id"],
            occurrence=row["occurrence"],
            thread=row["thread"],
            text=row["text"],
            label=row["label"],
        )


__all__ = ["Delivery", "DeliveryOutbox"]
