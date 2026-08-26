from dataclasses import dataclass, field
from pathlib import Path

from .file_ops import (
    audit_log_is_trusted,
    bytes_to_human,
    get_deletion_log_path,
    record_deletion_audit,
)
from .text import sanitize_for_display

REMOVED_STATUSES = {"deleted", "removed"}
TRASHED_PREFIXES = ("trashed",)
FAILED_STATUSES = {"failed"}
# Anything Topo refused to delete is a skip. Matched by prefix so that a new
# rejection reason (the ancestor checks in analyze._sudo_remove each added one)
# is counted the day it is introduced instead of silently falling out of every
# tally — which is what happened to rejected-toctou and the ancestor statuses.
SKIPPED_PREFIXES = ("rejected-",)
SKIPPED_STATUSES = {
    "dry-run",
    "missing",
    "trash-failed",
}


@dataclass
class DeletionEvent:
    timestamp: str
    mode: str
    size_bytes: int | None
    status: str
    path: str


# The three ways a session line can close it. "ended" is the normal finish;
# "interrupted" is Ctrl-C, written from the same `finally` that prints the
# partial report, so the log says "stopped here on purpose". A session with no
# closing line at all is neither -- it renders as "incomplete", which now means
# what it says: the process died without getting the chance to write anything.
SESSION_STATUSES = frozenset({"started", "ended", "interrupted"})
_CLOSING_STATUSES = frozenset({"ended", "interrupted"})


@dataclass
class HistorySession:
    command: str
    started_at: str
    ended_at: str = ""
    interrupted: bool = False
    events: list[DeletionEvent] = field(default_factory=list)

    @property
    def removed(self) -> int:
        return sum(1 for event in self.events if event.status in REMOVED_STATUSES)

    @property
    def trashed(self) -> int:
        return sum(1 for event in self.events if event.status.startswith(TRASHED_PREFIXES))

    @property
    def failed(self) -> int:
        return sum(1 for event in self.events if event.status in FAILED_STATUSES)

    @property
    def skipped(self) -> int:
        return sum(
            1
            for event in self.events
            if event.status in SKIPPED_STATUSES or event.status.startswith(SKIPPED_PREFIXES)
        )

    @property
    def total_size(self) -> int:
        return sum(
            event.size_bytes or 0
            for event in self.events
            if event.status in REMOVED_STATUSES or event.status.startswith(TRASHED_PREFIXES)
        )


def record_history_session(command: str, status: str) -> None:
    """Record a session boundary in the deletion audit log.

    *status* is one of SESSION_STATUSES; anything else is dropped rather than
    written as an event the parser would not recognise.
    """
    if status not in SESSION_STATUSES:
        return
    record_deletion_audit(command, "session", status, 0)


def parse_deletion_history(log_path: Path | None = None) -> list[HistorySession]:
    path = log_path or get_deletion_log_path()
    if not path.exists() or not audit_log_is_trusted(path):
        return []

    sessions: list[HistorySession] = []
    active: HistorySession | None = None
    ungrouped: HistorySession | None = None

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        event = _parse_event(raw_line)
        if event is None:
            continue

        if event.mode == "session":
            if event.status == "started":
                if active is not None:
                    sessions.append(active)
                active = HistorySession(command=event.path, started_at=event.timestamp)
            elif event.status in _CLOSING_STATUSES:
                if active is not None:
                    active.ended_at = event.timestamp
                    active.interrupted = event.status == "interrupted"
                    sessions.append(active)
                    active = None
            continue

        if active is not None:
            active.events.append(event)
        else:
            if ungrouped is None:
                ungrouped = HistorySession(command="legacy", started_at=event.timestamp)
            ungrouped.events.append(event)
            ungrouped.ended_at = event.timestamp

    if active is not None:
        sessions.append(active)
    if ungrouped is not None:
        sessions.insert(0, ungrouped)
    return sessions


def render_history(sessions: list[HistorySession], limit: int = 10) -> str:
    if not sessions:
        return "No deletion history found."

    lines = ["Deletion History", ""]
    for session in sessions[-limit:][::-1]:
        ended = session.ended_at or "incomplete"
        # "interrupted" and "incomplete" are different answers to "why does this
        # session stop here", so they read differently: the first was Ctrl-C and
        # the counts below it are the real total, the second means nothing closed
        # the session and the counts may be short.
        outcome = "  (interrupted)" if session.interrupted else ""
        lines.append(f"{session.started_at} -> {ended}  {session.command}{outcome}")
        lines.append(
            "  "
            f"removed={session.removed}  "
            f"trashed={session.trashed}  "
            f"skipped={session.skipped}  "
            f"failed={session.failed}  "
            f"size={bytes_to_human(session.total_size)}"
        )
        for event in session.events[-3:]:
            lines.append(f"    {event.status:<20} {sanitize_for_display(event.path)}")
    return "\n".join(lines)


def show_history(limit: int = 10) -> None:
    print(render_history(parse_deletion_history(), limit=limit))


def _parse_event(line: str) -> DeletionEvent | None:
    parts = line.split("\t", 4)
    if len(parts) != 5:
        return None
    timestamp, mode, raw_size, status, path = parts
    try:
        size_bytes = int(raw_size)
    except ValueError:
        size_bytes = None
    return DeletionEvent(
        timestamp=timestamp,
        mode=mode,
        size_bytes=size_bytes,
        status=status,
        path=path,
    )
