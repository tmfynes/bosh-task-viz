"""Pure parsing helpers for bosh-task-viz.

Two jobs:
  - parse_events / ChoreBuilder: turn `bosh task <id> --event` NDJSON into a
    tree of "chores" (parent bars + nested sub-chore bars) for the Gantt.
  - filter_debug / DebugFilter: turn `bosh task <id> --debug` ruby-logger
    output into a noise-filtered, secret-redacted line list.

No I/O, no Flask, no paramiko here -- keeps this testable with stdlib unittest.
"""

import json
import re
import time


# ---------------------------------------------------------------------------
# Event -> chore model
# ---------------------------------------------------------------------------

def classify_stage_family(stage):
    """Bucket a top-level stage name into a color family for the UI."""
    s = (stage or "").lower()
    if "compil" in s:
        return "compile"
    if "prepar" in s:
        return "prepare"
    if "creating missing vm" in s:
        return "create_vm"
    if "updat" in s and "instance" in s:
        return "update_instance"
    if "error" in s:
        return "error"
    return "other"


def classify_sub_family(status):
    """Bucket a sub-chore status string (data.status) into a color family."""
    s = (status or "").lower()
    if "drain" in s or "stop" in s:
        return "drain_stop"
    if "install" in s and "package" in s:
        return "packages"
    if "start" in s:
        return "start"
    return "other"


class ChoreBuilder:
    """Incrementally builds the chore tree, one NDJSON event at a time.

    Key = (stage, task, index) identifies the parent bar. `started` opens it,
    `finished`/`error` closes it. `in_progress` + data.status opens/closes
    nested sub-chores under the parent. Warnings/errors attach to whichever
    chore is currently open (most recently started, not yet closed).
    """

    def __init__(self):
        self.chores = []
        self._open_index = {}  # key -> chore list index, only while open
        self._open_stack = []  # indices of currently-open chores, opening order
        self.unattached_warnings = []
        self.unattached_errors = []
        self._next_id = 0

    def feed_text(self, text):
        for line in text.splitlines():
            self.feed_line(line)

    def feed_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return
        self.feed_event(event)

    def feed_event(self, event):
        etype = event.get("type")
        if etype in ("warning", "error"):
            entry = {"time": event.get("time"), "message": event.get("message", "")}
            target = self._current_open_chore()
            if target is not None:
                bucket = "warnings" if etype == "warning" else "errors"
                target[bucket].append(entry)
            else:
                bucket = self.unattached_warnings if etype == "warning" else self.unattached_errors
                bucket.append(entry)
            return

        stage = event.get("stage")
        task = event.get("task")
        index = event.get("index")
        state = event.get("state")
        key = (stage, task, index)

        if state == "started":
            self._open_chore(key, stage, task, index, event)
            return

        idx = self._open_index.get(key)
        if idx is None:
            # Saw in_progress/finished without a started event (stream started
            # mid-chore, e.g. we attached partway through a live task) -- open
            # one implicitly so nothing is silently dropped.
            idx = self._open_chore(key, stage, task, index, event)
        chore = self.chores[idx]

        if state == "in_progress":
            chore["progress"] = event.get("progress", chore["progress"])
            chore["state"] = "in_progress"
            data = event.get("data") or {}
            status = data.get("status")
            if status:
                self._close_last_sub(chore, event.get("time"))
                chore["sub_chores"].append({
                    "name": status,
                    "family": classify_sub_family(status),
                    "start": event.get("time"),
                    "end": None,
                    "duration_ms": None,
                    "progress": event.get("progress"),
                })
            return

        if state in ("finished", "error"):
            end = event.get("time")
            chore["end"] = end
            chore["duration_ms"] = self._duration_ms(chore["start"], end)
            chore["state"] = state
            chore["progress"] = event.get("progress", chore["progress"])
            self._close_last_sub(chore, end)
            if self._open_index.get(key) == idx:
                del self._open_index[key]
            if idx in self._open_stack:
                self._open_stack.remove(idx)
            return

    def _open_chore(self, key, stage, task, index, event):
        chore = {
            "id": self._next_id,
            "stage": stage,
            "task": task,
            "index": index,
            "tags": event.get("tags", []),
            "family": classify_stage_family(stage),
            "start": event.get("time"),
            "end": None,
            "duration_ms": None,
            "state": "started",
            "progress": event.get("progress", 0),
            "sub_chores": [],
            "warnings": [],
            "errors": [],
        }
        self._next_id += 1
        self.chores.append(chore)
        idx = len(self.chores) - 1
        self._open_index[key] = idx
        self._open_stack.append(idx)
        return idx

    @staticmethod
    def _close_last_sub(chore, end_time):
        subs = chore["sub_chores"]
        if subs and subs[-1]["end"] is None:
            prev = subs[-1]
            prev["end"] = end_time
            prev["duration_ms"] = ChoreBuilder._duration_ms(prev["start"], end_time)

    @staticmethod
    def _duration_ms(start, end):
        if start is None or end is None:
            return None
        return int((end - start) * 1000)

    def _current_open_chore(self):
        if self._open_stack:
            return self.chores[self._open_stack[-1]]
        return None

    def snapshot(self, now=None):
        """Return a plain-dict snapshot, filling in live durations for any
        chore/sub-chore still open (end is None) using `now`."""
        now = time.time() if now is None else now
        chores = []
        for chore in self.chores:
            c = dict(chore)
            if c["end"] is None and c["start"] is not None:
                c["duration_ms"] = self._duration_ms(c["start"], now)
            c["sub_chores"] = []
            for sub in chore["sub_chores"]:
                s = dict(sub)
                if s["end"] is None and s["start"] is not None:
                    s["duration_ms"] = self._duration_ms(s["start"], now)
                c["sub_chores"].append(s)
            c["warnings"] = list(chore["warnings"])
            c["errors"] = list(chore["errors"])
            chores.append(c)
        return {
            "chores": chores,
            "unattached_warnings": list(self.unattached_warnings),
            "unattached_errors": list(self.unattached_errors),
        }


def parse_events(lines, now=None):
    """One-shot parse of an iterable of NDJSON event lines -> chore snapshot."""
    builder = ChoreBuilder()
    for line in lines:
        builder.feed_line(line)
    return builder.snapshot(now=now)


# ---------------------------------------------------------------------------
# Debug stream noise filter
# ---------------------------------------------------------------------------

# I, [2026-10-09T14:16:12.794823 #29] [0xaa4a80]  INFO -- TaskHelper: Director Version: 283.1.0
RUBY_LOG_RE = re.compile(
    r'^(?P<sev>[IWEFD]),\s*'
    r'\[(?P<ts>[^\s\]]+)\s+#(?P<pid>\d+)\]\s*'
    r'(?:\[(?P<ctx>[^\]]*)\]\s*)?'
    r'(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\s*--\s*'
    r'(?:(?P<logger>[^:]+):\s+)?'
    r'(?P<message>.*)$'
)

_SQL_RE = re.compile(r'\b(SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT)\b', re.IGNORECASE)
_PEM_BEGIN_RE = re.compile(r'-----BEGIN [^-]+-----')
_PEM_END_RE = re.compile(r'-----END [^-]+-----')
_SECRET_KV_RE = re.compile(
    r'(?i)\b(password|secret|token|private_key|client_secret)(["\']?\s*(?:[:=]|=>)\s*)'
    r'("[^"]*"|\'[^\']*\'|\S+)'
)

_KEEP_LEVELS = ("INFO", "WARN", "ERROR", "FATAL")


def parse_ruby_log_line(line):
    """Extract ts/level/logger/message from a ruby Logger-formatted line.

    Returns None if the line doesn't match the expected format.
    """
    m = RUBY_LOG_RE.match(line)
    if not m:
        return None
    return {
        "ts": m.group("ts"),
        "level": m.group("level"),
        "logger": (m.group("logger") or "").strip() or None,
        "message": m.group("message").strip(),
    }


def is_level_line(line):
    parsed = parse_ruby_log_line(line)
    return parsed is not None and parsed["level"] in _KEEP_LEVELS


def _redact_secrets(line):
    return _SECRET_KV_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}[REDACTED]', line)


class DebugFilter:
    """Stateful line-by-line filter for `bosh task <id> --debug` output.

    Stateful because PEM blocks and manifest dumps span multiple lines, so a
    live stream needs to carry in_pem/in_manifest across feed_line calls.
    """

    def __init__(self):
        self.lines = []
        self.warnings = []
        self.errors = []
        self._in_pem = False
        self._in_manifest = False

    def feed_text(self, text):
        for raw in text.splitlines():
            self.feed_line(raw)

    def feed_line(self, raw):
        line = raw.rstrip("\r\n")
        if not line:
            return None

        if self._in_pem:
            if _PEM_END_RE.search(line):
                self._in_pem = False
                return self._emit_marker("[REDACTED PEM]")
            return None

        if _PEM_BEGIN_RE.search(line):
            self._in_pem = not bool(_PEM_END_RE.search(line))
            return self._emit_marker("[REDACTED PEM]")

        if self._in_manifest:
            if is_level_line(line):
                self._in_manifest = False
            else:
                return None

        if "Manifest:" in line:
            self._in_manifest = True
            return None

        if "DEBUG --" in line and _SQL_RE.search(line):
            return None

        line = _redact_secrets(line)

        parsed = parse_ruby_log_line(line)
        if parsed and parsed["level"] in _KEEP_LEVELS:
            entry = {"raw": line, **parsed}
        elif line.startswith("Task "):
            entry = {"raw": line, "ts": None, "level": "INFO", "logger": None, "message": line}
        else:
            return None

        self.lines.append(entry)
        if entry["level"] == "WARN":
            self.warnings.append(entry)
        elif entry["level"] in ("ERROR", "FATAL"):
            self.errors.append(entry)
        return entry

    def _emit_marker(self, text):
        entry = {"raw": text, "ts": None, "level": "INFO", "logger": None, "message": text}
        self.lines.append(entry)
        return entry

    def snapshot(self):
        return {
            "lines": list(self.lines),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def filter_debug(lines):
    """One-shot filter of an iterable of raw debug-log lines."""
    f = DebugFilter()
    for line in lines:
        f.feed_line(line)
    return f.snapshot()
