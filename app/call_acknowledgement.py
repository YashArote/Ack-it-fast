from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import requests

from app.call_config import CALL_RING_SECONDS, CALL_STATE_DB_PATH, WORKSPACE_TIMEZONE
from app.service_now_client import ServiceNowClient
from calling.trycall import GraphCallClient, GraphCallError


logger = logging.getLogger("ackitfast.call_acknowledgement")


class CallAcknowledgementService:
    def __init__(
        self,
        service_now_client: ServiceNowClient,
        graph_client: Optional[GraphCallClient] = None,
        database_path: str = CALL_STATE_DB_PATH,
        ring_seconds: int = CALL_RING_SECONDS,
        timezone_name: str = WORKSPACE_TIMEZONE,
    ):
        self.service_now_client = service_now_client
        self.graph_client = graph_client or GraphCallClient()
        self.database_path = database_path
        self.ring_seconds = ring_seconds
        self.timezone = ZoneInfo(timezone_name)
        self._lock = threading.RLock()
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    ticket_sys_id TEXT NOT NULL,
                    ticket_number TEXT NOT NULL,
                    ticket_type TEXT NOT NULL,
                    agent_email TEXT NOT NULL,
                    placed_at TEXT NOT NULL,
                    established_at TEXT,
                    ended_at TEXT,
                    outcome TEXT,
                    result_message TEXT,
                    timeout_requested INTEGER NOT NULL DEFAULT 0,
                    end_requested INTEGER NOT NULL DEFAULT 0,
                    note_claimed INTEGER NOT NULL DEFAULT 0,
                    note_written INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    result_message TEXT
                );
                """
            )
            call_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(calls)").fetchall()
            }
            if "timeout_requested" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN timeout_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "end_requested" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN end_requested INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _format_datetime(self, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")

    @staticmethod
    def _duration(start: str, end: str) -> str:
        seconds = max(
            0,
            int(
                (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
            ),
        )
        minutes, remaining_seconds = divmod(seconds, 60)
        return f"{minutes}m {remaining_seconds}s"

    def _build_work_note(self, call: sqlite3.Row) -> str:
        placed_at = call["placed_at"]
        ended_at = call["ended_at"] or self._now().isoformat()
        lines = [
            f"[AssignmentBot] Acknowledgement call for {call['ticket_number']}.",
            f"Assigned agent: {call['agent_email']}.",
            f"Call placed: {self._format_datetime(placed_at)}.",
        ]
        if call["outcome"] == "acknowledged" and call["established_at"]:
            lines.append(
                "Acknowledged: Yes — call established at "
                f"{self._format_datetime(call['established_at'])} "
                f"({self._duration(placed_at, call['established_at'])} after the call)."
            )
        elif call["outcome"] == "timed_out":
            lines.append(
                f"Acknowledged: No — no answer within {self.ring_seconds} seconds."
            )
        else:
            lines.append(
                "Acknowledged: No — the call ended without an established event."
            )
        lines.append(
            f"Call ended: {self._format_datetime(ended_at)} "
            f"(duration {self._duration(placed_at, ended_at)})."
        )
        return "\n".join(lines)

    def _claim_note(
        self,
        connection: sqlite3.Connection,
        call_id: str,
    ) -> Optional[dict[str, str]]:
        call = connection.execute(
            "SELECT * FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        if (
            not call
            or not call["outcome"]
            or not call["ended_at"]
            or call["note_claimed"]
            or call["note_written"]
        ):
            return None
        connection.execute(
            "UPDATE calls SET note_claimed = 1 WHERE call_id = ?",
            (call_id,),
        )
        return {
            "ticket_sys_id": call["ticket_sys_id"],
            "ticket_type": call["ticket_type"],
            "work_notes": self._build_work_note(call),
        }

    def _apply_event(
        self,
        connection: sqlite3.Connection,
        call_id: str,
        state: str,
        change_type: str,
        received_at: str,
        result_message: str,
    ) -> None:
        call = connection.execute(
            "SELECT * FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        if not call:
            return

        if state == "established" and not call["established_at"] and not call["outcome"]:
            connection.execute(
                "UPDATE calls SET established_at = ? WHERE call_id = ?",
                (received_at, call_id),
            )
            call = connection.execute(
                "SELECT * FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()

        if (
            state == "terminating"
            and not call["established_at"]
            and not call["timeout_requested"]
            and not call["outcome"]
        ):
            connection.execute(
                "UPDATE calls SET outcome = 'not_answered' WHERE call_id = ?",
                (call_id,),
            )
            call = connection.execute(
                "SELECT * FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()

        if state == "terminated" or change_type == "deleted":
            outcome = call["outcome"]
            if not outcome:
                if call["established_at"]:
                    outcome = "acknowledged"
                elif call["timeout_requested"]:
                    outcome = "timed_out"
                else:
                    outcome = "not_answered"
            connection.execute(
                """
                UPDATE calls
                SET ended_at = COALESCE(ended_at, ?),
                    outcome = COALESCE(outcome, ?),
                    result_message = COALESCE(result_message, ?)
                WHERE call_id = ?
                """,
                (received_at, outcome, result_message or None, call_id),
            )

    def _dispatch_note(self, note: dict[str, str], call_id: str) -> None:
        try:
            if note["ticket_type"] == "ritm":
                self.service_now_client.add_ritm_work_notes(
                    note["ticket_sys_id"],
                    note["work_notes"],
                )
            else:
                self.service_now_client.add_work_notes(
                    note["ticket_sys_id"],
                    note["work_notes"],
                )
        except requests.RequestException:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "UPDATE calls SET note_claimed = 0 WHERE call_id = ?",
                    (call_id,),
                )
            logger.exception(
                "failed to append call acknowledgment work note",
                extra={"call_id": call_id, "ticket_sys_id": note["ticket_sys_id"]},
            )
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE calls SET note_written = 1 WHERE call_id = ?",
                (call_id,),
            )
        logger.info(
            "call acknowledgment work note appended",
            extra={"call_id": call_id, "ticket_sys_id": note["ticket_sys_id"]},
        )

    def _write_failure_note(
        self,
        ticket_sys_id: str,
        ticket_number: str,
        ticket_type: str,
    ) -> None:
        work_notes = (
            f"[AssignmentBot] An acknowledgement call could not be started for "
            f"{ticket_number}; no call or acknowledgement timing is available."
        )
        try:
            if ticket_type == "ritm":
                self.service_now_client.add_ritm_work_notes(ticket_sys_id, work_notes)
            else:
                self.service_now_client.add_work_notes(ticket_sys_id, work_notes)
        except requests.RequestException:
            logger.exception(
                "failed to append call-start failure work note",
                extra={"ticket_sys_id": ticket_sys_id, "ticket_number": ticket_number},
            )

    def register_call(
        self,
        call_id: str,
        ticket_sys_id: str,
        ticket_number: str,
        ticket_type: str,
        agent_email: str,
        placed_at: datetime,
    ) -> list[str]:
        notes = []
        calls_to_end = []
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO calls (
                    call_id, ticket_sys_id, ticket_number, ticket_type,
                    agent_email, placed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    ticket_sys_id,
                    ticket_number,
                    ticket_type,
                    agent_email,
                    placed_at.isoformat(),
                ),
            )
            events = connection.execute(
                "SELECT * FROM call_events WHERE call_id = ? ORDER BY id",
                (call_id,),
            ).fetchall()
            for event in events:
                self._apply_event(
                    connection,
                    call_id,
                    event["state"],
                    event["change_type"],
                    event["received_at"],
                    event["result_message"] or "",
                )
            connection.execute("DELETE FROM call_events WHERE call_id = ?", (call_id,))
            call = connection.execute(
                "SELECT established_at, outcome, end_requested FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if (
                call
                and call["established_at"]
                and not call["outcome"]
                and not call["end_requested"]
            ):
                connection.execute(
                    "UPDATE calls SET end_requested = 1 WHERE call_id = ?",
                    (call_id,),
                )
                calls_to_end.append(call_id)
            note = self._claim_note(connection, call_id)
            if note:
                notes.append(note)
        for note in notes:
            self._dispatch_note(note, call_id)
        return calls_to_end

    def handle_notifications(
        self,
        payload: dict[str, Any],
    ) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
        notifications = payload.get("value")
        if not isinstance(notifications, list):
            raise ValueError("Notification payload must contain a value array")

        notes: list[tuple[str, dict[str, str]]] = []
        calls_to_end: list[str] = []
        with self._lock, self._connect() as connection:
            for notification in notifications:
                if not isinstance(notification, dict):
                    continue
                resource_data = notification.get("resourceData") or {}
                if not isinstance(resource_data, dict):
                    continue
                resource = notification.get("resource") or notification.get("resourceUrl") or ""
                call_id = str(resource).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                state = str(resource_data.get("state") or "").casefold()
                change_type = str(notification.get("changeType") or "").casefold()
                if not call_id or not state:
                    continue
                received_at = self._now().isoformat()
                result_info = resource_data.get("resultInfo") or {}
                result_message = (
                    str(result_info.get("message") or "")
                    if isinstance(result_info, dict)
                    else ""
                )
                registered = connection.execute(
                    "SELECT 1 FROM calls WHERE call_id = ?",
                    (call_id,),
                ).fetchone()
                if not registered:
                    connection.execute(
                        """
                        INSERT INTO call_events (
                            call_id, state, change_type, received_at, result_message
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (call_id, state, change_type, received_at, result_message),
                    )
                self._apply_event(
                    connection,
                    call_id,
                    state,
                    change_type,
                    received_at,
                    result_message,
                )
                if state == "established":
                    call = connection.execute(
                        "SELECT end_requested FROM calls WHERE call_id = ?",
                        (call_id,),
                    ).fetchone()
                    if call and not call["end_requested"]:
                        connection.execute(
                            "UPDATE calls SET end_requested = 1 WHERE call_id = ?",
                            (call_id,),
                        )
                        calls_to_end.append(call_id)
                note = self._claim_note(connection, call_id)
                if note:
                    notes.append((call_id, note))

            cutoff = (self._now() - timedelta(days=1)).isoformat()
            connection.execute(
                """
                DELETE FROM call_events
                WHERE received_at < ?
                  AND call_id NOT IN (SELECT call_id FROM calls)
                """,
                (cutoff,),
            )

        return notes, calls_to_end

    def write_notification_notes(self, notes: list[tuple[str, dict[str, str]]]) -> None:
        for call_id, note in notes:
            self._dispatch_note(note, call_id)

    def end_acknowledged_call(self, call_id: str) -> None:
            with self._lock, self._connect() as connection:
                call = connection.execute(
                    "SELECT established_at, outcome FROM calls WHERE call_id = ?",
                    (call_id,),
                ).fetchone()
                if not call or not call["established_at"] or call["outcome"]:
                    return

            try:
                self.graph_client.end_call(call_id)
            except (GraphCallError, requests.RequestException):
                logger.exception(
                    "failed to end acknowledged call",
                    extra={"call_id": call_id},
                )
                return

    def timeout_call(self, call_id: str) -> None:
        with self._lock, self._connect() as connection:
            call = connection.execute(
                "SELECT established_at, outcome FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if not call or call["established_at"] or call["outcome"]:
                return
            connection.execute(
                "UPDATE calls SET timeout_requested = 1 WHERE call_id = ?",
                (call_id,),
            )

        try:
            self.graph_client.end_call(call_id)
        except (GraphCallError, requests.RequestException):
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    UPDATE calls SET timeout_requested = 0
                    WHERE call_id = ? AND outcome IS NULL
                    """,
                    (call_id,),
                )
            logger.exception("failed to end unanswered call", extra={"call_id": call_id})
            return


    def start_call(
        self,
        email: str,
        ticket_sys_id: str,
        ticket_number: str,
        ticket_type: str,
    ) -> None:
        if not email or not email.strip():
            logger.error(
                "cannot initiate acknowledgment call without assigned agent email",
                extra={"ticket_sys_id": ticket_sys_id, "ticket_number": ticket_number},
            )
            self._write_failure_note(ticket_sys_id, ticket_number, ticket_type)
            return

        placed_at = self._now()
        try:
            call_id = self.graph_client.create_call(email)
        except (GraphCallError, requests.RequestException, ValueError):
            logger.exception(
                "failed to initiate acknowledgment call",
                extra={"ticket_sys_id": ticket_sys_id, "ticket_number": ticket_number},
            )
            self._write_failure_note(ticket_sys_id, ticket_number, ticket_type)
            return

        calls_to_end = self.register_call(
            call_id,
            ticket_sys_id,
            ticket_number,
            ticket_type,
            email,
            placed_at,
        )
        for acknowledged_call_id in calls_to_end:
            self.end_acknowledged_call(acknowledged_call_id)
        logger.info(
            "acknowledgment call initiated",
            extra={
                "call_id": call_id,
                "ticket_number": ticket_number,
                "ticket_type": ticket_type,
            },
        )
        timer = threading.Timer(
            self.ring_seconds,
            self.timeout_call,
            args=(call_id,),
        )
        timer.daemon = True
        timer.start()
