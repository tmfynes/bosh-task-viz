"""bosh-task-viz -- Flask app + background poller/streams.

SSHes to Ops Manager (bound user-provided service `ops-manager-ssh`), runs
read-only `bosh` commands there, and serves a live D3 Gantt of BOSH task
chores plus a filtered debug log. Single process, in-memory state -- run
with `threaded=True` so SSE + REST share the same TaskManager safely.

DEMO=1 (or missing VCAP creds) replays fixtures/task-84098.* so the UI has
something to show without a real Ops Manager.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, send_from_directory

import om_ssh
from parse_bosh import ChoreBuilder, DebugFilter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
FIXTURES_DIR = os.path.join(BASE_DIR, "fixtures")

RECENT_TASKS_LIMIT = 40
DEBUG_LINES_CAP = 500
MAX_CONCURRENT_DEBUG_STREAMS = 8
POLL_INTERVAL_S = 1.0
BOSH_STATUS_REFRESH_S = 15.0
RESULT_TRUNCATE_CHARS = 300

app = Flask(__name__, static_folder="static", static_url_path="/static")


def _truncate(value, n=RESULT_TRUNCATE_CHARS):
    if value is None:
        return None
    s = value if isinstance(value, str) else json.dumps(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def _to_epoch(value):
    """Normalize bosh timestamps (unix seconds or 'Fri Oct  9 14:16:12 UTC 2026')."""
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in (
        "%a %b %d %H:%M:%S UTC %Y",
        "%a %b %d %H:%M:%S %Z %Y",
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _parse_tasks_json(output):
    """Parse `bosh tasks --recent=N --json` output into a list of task dicts.

    bosh CLI --json emits {"Tables":[{"Rows":[{...}], ...}], ...}. Each row
    uses the column names from the spec: id, state, started_at, finished_at,
    user, deployment, description, result.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    rows = []
    if isinstance(data, dict) and isinstance(data.get("Tables"), list):
        for table in data["Tables"]:
            rows.extend(table.get("Rows", []) or [])
    elif isinstance(data, list):
        rows = data
    tasks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            task_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        tasks.append({
            "id": task_id,
            "state": row.get("state"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "user": row.get("user"),
            "deployment": row.get("deployment"),
            "description": row.get("description"),
            "result": row.get("result"),
        })
    return tasks


def _parse_bosh_env(output):
    """Best-effort parse of `bosh env --json` (or plain text) for a name."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("Tables"), list):
        for table in data["Tables"]:
            rows = table.get("Rows") or []
            if rows and isinstance(rows[0], dict):
                name = rows[0].get("name") or rows[0].get("Name")
                if name:
                    return name
    for line in output.splitlines():
        line = line.strip()
        if line.lower().startswith("name"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


class TaskStream:
    """Live state for one tracked task: chore builder + debug filter + threads."""

    def __init__(self, task_id):
        self.task_id = task_id
        self.lock = threading.RLock()
        self.chore_builder = ChoreBuilder()
        self.debug_filter = DebugFilter()
        self.event_thread = None
        self.debug_thread = None
        self.event_done = False
        self.debug_done = False
        self.error = None

    def chores_snapshot(self):
        with self.lock:
            return self.chore_builder.snapshot(now=time.time())

    def debug_snapshot(self):
        with self.lock:
            snap = self.debug_filter.snapshot()
        snap["lines"] = snap["lines"][-DEBUG_LINES_CAP:]
        snap["warnings"] = snap["warnings"][-DEBUG_LINES_CAP:]
        snap["errors"] = snap["errors"][-DEBUG_LINES_CAP:]
        return snap


class TaskManager:
    def __init__(self, demo=False):
        self.demo = demo
        self.lock = threading.RLock()
        self.ssh = None
        self.ssh_status = {"connected": False, "host": None, "user": None, "error": None}
        self.bosh_status = {"ok": False, "name": None, "error": None}
        self.tasks = {}  # id -> task meta dict
        self.manual_tracked = set()
        self.streams = {}  # id -> TaskStream
        self.debug_semaphore = threading.Semaphore(MAX_CONCURRENT_DEBUG_STREAMS)
        self._stop = threading.Event()
        self._last_bosh_check = 0.0
        self._started_at = time.time()

        if not demo:
            creds = om_ssh.load_creds_from_vcap()
            if creds:
                self.ssh = om_ssh.OmSSH(creds)
                self.ssh_status["host"] = self.ssh.host
                self.ssh_status["user"] = self.ssh.user
            else:
                self.ssh_status["error"] = (
                    "no 'ops-manager-ssh' user-provided service bound "
                    "(expected credentials: host, ip, port, user, private_key)"
                )

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self.demo:
            self._load_demo()
            return
        threading.Thread(target=self._poll_loop, daemon=True, name="bosh-poller").start()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - poller must never die
                with self.lock:
                    self.ssh_status["error"] = str(exc)
            self._stop.wait(POLL_INTERVAL_S)

    def _tick(self):
        if self.ssh is None:
            return
        try:
            self.ssh.ensure_connected()
            with self.lock:
                self.ssh_status = {
                    "connected": True,
                    "host": self.ssh.host,
                    "user": self.ssh.user,
                    "error": None,
                }
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.ssh_status = {
                    "connected": False,
                    "host": self.ssh.host,
                    "user": self.ssh.user,
                    "error": str(exc),
                }
            return

        now = time.time()
        if now - self._last_bosh_check >= BOSH_STATUS_REFRESH_S:
            self._last_bosh_check = now
            self._refresh_bosh_status()

        self._refresh_tasks()
        self._reconcile_streams()

    def _refresh_bosh_status(self):
        try:
            status, out, err = self.ssh.exec_bosh("env --json", timeout=30)
            if status == 0:
                name = _parse_bosh_env(out)
                with self.lock:
                    self.bosh_status = {"ok": True, "name": name, "error": None}
            else:
                with self.lock:
                    self.bosh_status = {
                        "ok": False,
                        "name": None,
                        "error": (err or out).strip().splitlines()[0][:200] if (err or out).strip() else "bosh env failed",
                    }
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.bosh_status = {"ok": False, "name": None, "error": str(exc)}

    def _refresh_tasks(self):
        status, out, err = self.ssh.exec_bosh(f"tasks --recent={RECENT_TASKS_LIMIT} --json", timeout=30)
        if status != 0:
            return
        tasks = _parse_tasks_json(out)
        with self.lock:
            self.tasks = {t["id"]: t for t in tasks}

    def _reconcile_streams(self):
        with self.lock:
            tasks = dict(self.tasks)
            wanted = {
                tid for tid, t in tasks.items()
                if t.get("state") == "processing" or tid in self.manual_tracked
            }
            existing = set(self.streams.keys())

        for tid in wanted - existing:
            self._start_stream(tid)

        for tid in existing:
            stream = self.streams.get(tid)
            if not stream:
                continue
            task = tasks.get(tid)
            still_processing = bool(task and task.get("state") == "processing")
            if stream.event_thread and not stream.event_thread.is_alive() and still_processing:
                self._start_event_thread(stream)
            if stream.debug_thread and not stream.debug_thread.is_alive() and still_processing and not stream.debug_done:
                self._start_debug_thread(stream)

    def _start_stream(self, task_id):
        stream = TaskStream(task_id)
        with self.lock:
            self.streams[task_id] = stream
        self._start_event_thread(stream)
        self._start_debug_thread(stream)

    def _start_event_thread(self, stream):
        def run():
            try:
                for line in self.ssh.stream_bosh_lines(f"task {stream.task_id} --event"):
                    with stream.lock:
                        stream.chore_builder.feed_line(line)
            except Exception as exc:  # noqa: BLE001
                stream.error = str(exc)
            finally:
                stream.event_done = True

        t = threading.Thread(target=run, daemon=True, name=f"events-{stream.task_id}")
        stream.event_thread = t
        stream.event_done = False
        t.start()

    def _start_debug_thread(self, stream):
        if not self.debug_semaphore.acquire(blocking=False):
            return

        def run():
            try:
                for line in self.ssh.stream_bosh_lines(f"task {stream.task_id} --debug"):
                    with stream.lock:
                        stream.debug_filter.feed_line(line)
            except Exception as exc:  # noqa: BLE001
                stream.error = str(exc)
            finally:
                stream.debug_done = True
                self.debug_semaphore.release()

        t = threading.Thread(target=run, daemon=True, name=f"debug-{stream.task_id}")
        stream.debug_thread = t
        stream.debug_done = False
        t.start()

    # -- demo mode -----------------------------------------------------------

    def _load_demo(self):
        events_path = os.path.join(FIXTURES_DIR, "task-84098.events.jsonl")
        debug_path = os.path.join(FIXTURES_DIR, "task-84098.debug.redacted.log")

        builder = ChoreBuilder()
        started_at = None
        finished_at = None
        if os.path.exists(events_path):
            with open(events_path) as f:
                for line in f:
                    builder.feed_line(line)
                    try:
                        ev = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    t = ev.get("time")
                    if t is None:
                        continue
                    started_at = t if started_at is None else min(started_at, t)
                    finished_at = t if finished_at is None else max(finished_at, t)

        debug_filter = DebugFilter()
        if os.path.exists(debug_path):
            with open(debug_path) as f:
                for line in f:
                    debug_filter.feed_line(line)

        stream = TaskStream(84098)
        stream.chore_builder = builder
        stream.debug_filter = debug_filter
        stream.event_done = True
        stream.debug_done = True

        with self.lock:
            self.streams[84098] = stream
            self.manual_tracked.add(84098)
            self.tasks = {
                84098: {
                    "id": 84098,
                    "state": "done",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "user": "postgres-99cb62541fc22651f819",
                    "deployment": "service-instance_a8d11c73-e850-4ce1-b4ea-5bb7c7ce9074",
                    "description": "create deployment",
                    "result": "demo fixture replay (not a live task)",
                }
            }
            self.ssh_status = {
                "connected": False,
                "host": None,
                "user": None,
                "error": "DEMO mode -- showing fixtures/task-84098.* (no ops-manager-ssh bound)",
            }
            self.bosh_status = {"ok": False, "name": None, "error": "demo mode"}

    # -- read helpers used by routes -----------------------------------------

    def health(self):
        with self.lock:
            return {
                "ok": True,
                "demo": self.demo,
                "ssh": dict(self.ssh_status),
                "bosh": dict(self.bosh_status),
            }

    def task_summaries(self):
        with self.lock:
            tasks = list(self.tasks.values())
            manual = set(self.manual_tracked)
            streams = dict(self.streams)
        now = time.time()
        summaries = []
        for t in sorted(tasks, key=lambda x: (x.get("started_at") or 0), reverse=True):
            tid = t["id"]
            stream = streams.get(tid)
            chore_count = 0
            current_chore = None
            if stream is not None:
                snap = stream.chores_snapshot()
                chore_count = len(snap["chores"])
                open_chores = [c for c in snap["chores"] if c["end"] is None]
                last = open_chores[-1] if open_chores else (snap["chores"][-1] if snap["chores"] else None)
                if last is not None:
                    label = f"{last['stage']}: {last['task']}"
                    if last["sub_chores"]:
                        label += f" ({last['sub_chores'][-1]['name']})"
                    current_chore = label
            started_at = t.get("started_at")
            finished_at = t.get("finished_at")
            start_epoch = _to_epoch(started_at)
            finish_epoch = _to_epoch(finished_at)
            if t.get("state") == "processing" and start_epoch:
                elapsed_s = now - start_epoch
            elif start_epoch and finish_epoch:
                elapsed_s = finish_epoch - start_epoch
            else:
                elapsed_s = None
            summaries.append({
                "id": tid,
                "state": t.get("state"),
                "deployment": t.get("deployment"),
                "description": t.get("description"),
                "started_at": started_at,
                "finished_at": finished_at,
                "result": _truncate(t.get("result")),
                "tracked": tid in manual or t.get("state") == "processing",
                "chore_count": chore_count,
                "elapsed_s": elapsed_s,
                "current_chore": current_chore,
            })
        return summaries

    def task_detail(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            stream = self.streams.get(task_id)
            tracked = task_id in self.manual_tracked
        if task is None and stream is None:
            return None
        meta = dict(task) if task else {"id": task_id}
        meta["result"] = _truncate(meta.get("result"))
        meta["tracked"] = tracked
        chores = stream.chores_snapshot() if stream else {"chores": [], "unattached_warnings": [], "unattached_errors": []}
        debug = stream.debug_snapshot() if stream else {"lines": [], "warnings": [], "errors": []}
        return {
            "meta": meta,
            "chores": chores["chores"],
            "warnings": chores["unattached_warnings"] + debug["warnings"],
            "errors": chores["unattached_errors"] + debug["errors"],
            "debug_lines": debug["lines"],
        }

    def track(self, task_id):
        with self.lock:
            self.manual_tracked.add(task_id)
        if not self.demo:
            self._reconcile_streams()

    def untrack(self, task_id):
        with self.lock:
            self.manual_tracked.discard(task_id)

    def stream_payload(self):
        with self.lock:
            tracked_ids = set(self.manual_tracked) | {
                tid for tid, t in self.tasks.items() if t.get("state") == "processing"
            }
            streams = dict(self.streams)
        chores = {}
        for tid in tracked_ids:
            stream = streams.get(tid)
            if stream is not None:
                chores[str(tid)] = stream.chores_snapshot()["chores"]
        health = self.health()
        return {
            "ts": time.time(),
            "demo": health.get("demo", False),
            "ssh": health["ssh"],
            "bosh": health["bosh"],
            "tasks": self.task_summaries(),
            "chores": chores,
        }


DEMO = os.environ.get("DEMO") == "1" or not os.environ.get("VCAP_SERVICES")
manager = TaskManager(demo=DEMO)
manager.start()


# -- routes -------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/health")
def api_health():
    return jsonify(manager.health())


@app.route("/api/tasks")
def api_tasks():
    return jsonify({"tasks": manager.task_summaries()})


@app.route("/api/tasks/<int:task_id>")
def api_task_detail(task_id):
    detail = manager.task_detail(task_id)
    if detail is None:
        return jsonify({"error": "task not found"}), 404
    return jsonify(detail)


@app.route("/api/tasks/<int:task_id>/track", methods=["POST"])
def api_task_track(task_id):
    manager.track(task_id)
    return jsonify({"ok": True, "tracked": True})


@app.route("/api/tasks/<int:task_id>/untrack", methods=["POST"])
def api_task_untrack(task_id):
    manager.untrack(task_id)
    return jsonify({"ok": True, "tracked": False})


@app.route("/api/stream")
def api_stream():
    def generate():
        last_heartbeat = time.time()
        while True:
            payload = manager.stream_payload()
            yield f"event: snapshot\ndata: {json.dumps(payload)}\n\n"
            now = time.time()
            if now - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            time.sleep(1.0)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
