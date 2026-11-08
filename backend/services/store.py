"""MongoDB Atlas history: event log and versioned buildings.

Env (never log or return these):
    MONGODB_URI   Atlas SRV string. Unset -> log/save are no-ops, history routes 503.
    MONGODB_DB    database name (default firesight).

log() enqueues inserts on one daemon thread and never raises.
Monkeypatch get_client() in tests with an in-memory stand-in.
"""
import copy
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

_POSE_INTERVAL_S = 2.0
_QUEUE_MAX = 5000
_CONNECT_MS = 3000

_client = None
_client_lock = threading.Lock()
_indexed: set[int] = set()

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker_started = False

_pose_lock = threading.Lock()
_pose_at: dict[str, float] = {}

_save_lock = threading.Lock()
_ts_lock = threading.Lock()
_last_ts: datetime | None = None

router = APIRouter()


def enabled() -> bool:
    """True when MONGODB_URI is set. Does not open a connection."""
    return bool(os.environ.get("MONGODB_URI", "").strip())


def _db_name() -> str:
    name = os.environ.get("MONGODB_DB", "").strip()
    return name or "firesight"


def get_client():
    """Lazy MongoClient, or None when disabled.

    Tests monkeypatch this. The real client uses serverSelectionTimeoutMS=3000
    and does not connect until the first operation.
    """
    global _client
    if not enabled():
        return None
    with _client_lock:
        if _client is None:
            from pymongo import MongoClient

            _client = MongoClient(
                os.environ["MONGODB_URI"].strip(),
                serverSelectionTimeoutMS=_CONNECT_MS,
                tz_aware=True,
            )
        return _client


def get_db():
    client = get_client()
    if client is None:
        return None
    db = client[_db_name()]
    _ensure_indexes(db)
    return db


def _ensure_indexes(db) -> None:
    key = id(db)
    if key in _indexed:
        return
    db["events"].create_index([("kind", 1), ("ts", -1)], name="kind_ts")
    db["buildings"].create_index([("name", 1), ("version", -1)], name="name_version")
    _indexed.add(key)


def _as_utc(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        stamped = _as_utc(value)
        if stamped is None:
            return None
        return stamped.isoformat().replace("+00:00", "Z")
    return value


def _unique_ts() -> datetime:
    """UTC now, strictly increasing so same-millisecond events keep order."""
    global _last_ts
    with _ts_lock:
        now = datetime.now(timezone.utc)
        if _last_ts is not None and now <= _last_ts:
            now = _last_ts + timedelta(microseconds=1)
        _last_ts = now
        return now


def _session_key(doc: dict) -> str:
    for key in ("session", "session_id", "person_id"):
        value = doc.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def forget_session(session) -> None:
    """Drop the loc_pose throttle timestamp for a session that has been purged."""
    if session is None:
        return
    with _pose_lock:
        _pose_at.pop(str(session), None)


def _pose_allowed(doc: dict) -> bool:
    """loc_pose: at most one insert per session per 2 seconds."""
    key = _session_key(doc)
    now = time.monotonic()
    with _pose_lock:
        last = _pose_at.get(key)
        if last is not None and (now - last) < _POSE_INTERVAL_S:
            return False
        _pose_at[key] = now
        return True


def _make_event(kind: str, doc: dict) -> dict:
    event = {k: v for k, v in doc.items() if k != "_id"}
    building = event.get("building")
    if building in (None, "") and event.get("name") not in (None, ""):
        building = event.get("name")
    event["kind"] = kind
    event["ts"] = _unique_ts()
    if building not in (None, ""):
        event["building"] = building
    else:
        event.pop("building", None)
    return event


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, name="firesight-store", daemon=True).start()
        _worker_started = True


def _worker() -> None:
    while True:
        event = _queue.get()
        try:
            _insert_event(event)
        except Exception:
            pass
        finally:
            _queue.task_done()


def _insert_event(event: dict) -> None:
    db = get_db()
    if db is None:
        return
    db["events"].insert_one(dict(event))


def log(kind: str, doc: dict | None = None) -> None:
    """Queue an event insert. No-op when disabled. Never raises.

    Stored as {kind, ts (UTC), building (name from the doc when present), **doc}
    in collection "events". kind "loc_pose" is throttled to 1 per session per 2 s.
    """
    try:
        if not enabled():
            return
        kind = kind if isinstance(kind, str) else str(kind)
        payload = copy.deepcopy(doc) if isinstance(doc, dict) else {}
        if kind == "loc_pose" and not _pose_allowed(payload):
            return
        event = _make_event(kind, payload)
        _ensure_worker()
        _queue.put_nowait(event)
    except Exception:
        return


def flush(timeout: float = 5.0) -> None:
    """Block until queued events have been inserted. For tests."""
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        with _queue.mutex:
            pending = _queue.unfinished_tasks
        if pending == 0:
            return
        time.sleep(0.005)
    raise TimeoutError("event log did not drain")


def save_building(name, doc) -> dict | None:
    """Insert the next version (previous max + 1) into "buildings".

    Returns {name, version, saved_at} or None when disabled or the write fails.
    """
    if not enabled():
        return None
    try:
        db = get_db()
        if db is None:
            return None
        payload = copy.deepcopy(doc) if isinstance(doc, dict) else {}
        label = str(name)
        col = db["buildings"]
        with _save_lock:
            prev = col.find_one({"name": label}, sort=[("version", -1)])
            version = 1
            if prev is not None and prev.get("version") is not None:
                version = int(prev["version"]) + 1
            saved_at = datetime.now(timezone.utc)
            col.insert_one({
                "name": label,
                "version": version,
                "saved_at": saved_at,
                "doc": payload,
            })
        return {"name": label, "version": version, "saved_at": _iso(saved_at)}
    except Exception:
        return None


def list_buildings() -> list:
    """Latest version of each building: [{name, version, saved_at}]."""
    if not enabled():
        return []
    db = get_db()
    if db is None:
        return []
    best: dict = {}
    for row in db["buildings"].find({}):
        label = row.get("name")
        if not label:
            continue
        try:
            version = int(row.get("version"))
        except (TypeError, ValueError):
            continue
        current = best.get(label)
        if current is None or version > current["version"]:
            best[label] = {
                "name": label,
                "version": version,
                "saved_at": _iso(row.get("saved_at")),
            }
    return [best[key] for key in sorted(best)]


def _load_record(name, version=None) -> dict | None:
    if not enabled():
        return None
    db = get_db()
    if db is None:
        return None
    label = str(name)
    col = db["buildings"]
    if version is None:
        row = col.find_one({"name": label}, sort=[("version", -1)])
    else:
        row = col.find_one({"name": label, "version": int(version)})
    if not row:
        return None
    doc = row.get("doc")
    if isinstance(doc, dict):
        doc = copy.deepcopy(doc)
    return {
        "name": row.get("name"),
        "version": row.get("version"),
        "saved_at": _iso(row.get("saved_at")),
        "doc": doc,
    }


def load_building(name, version=None):
    """Return the saved building document, or None. Latest version when version is omitted."""
    rec = _load_record(name, version)
    if rec is None:
        return None
    return rec["doc"]


def _clamp_limit(limit) -> int:
    if limit is None:
        return 100
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 100
    return max(1, min(n, 1000))


def _public_event(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key == "_id":
            out["_id"] = str(value)
        elif isinstance(value, datetime):
            out[key] = _iso(value)
        else:
            out[key] = value
    return out


def recent(kind=None, limit=100) -> list:
    """Events newest first. _id is a string, ts is an ISO timestamp."""
    if not enabled():
        return []
    db = get_db()
    if db is None:
        return []
    query = {}
    if kind:
        query["kind"] = str(kind)
    limit_n = _clamp_limit(limit)
    rows = list(db["events"].find(query).sort([("ts", -1)]).limit(limit_n))
    rows.sort(key=lambda row: _as_utc(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [_public_event(row) for row in rows[:limit_n]]


def _timeline_kind(kind) -> bool:
    return kind == "person_status" or (isinstance(kind, str) and kind.startswith("fire_"))


def _parse_since(since_iso):
    if since_iso in (None, ""):
        return None
    text = str(since_iso).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("since must be an ISO timestamp") from None
    stamped = _as_utc(parsed)
    if stamped is None:
        raise ValueError("since must be an ISO timestamp")
    return stamped


def timeline(since_iso=None) -> list:
    """fire_* and person_status events in time order (oldest first)."""
    if not enabled():
        return []
    since = _parse_since(since_iso)
    db = get_db()
    if db is None:
        return []
    query = {
        "$or": [
            {"kind": {"$regex": "^fire_"}},
            {"kind": "person_status"},
        ]
    }
    if since is not None:
        query["ts"] = {"$gte": since}
    rows = list(db["events"].find(query).sort([("ts", 1)]))
    kept = []
    for row in rows:
        if not _timeline_kind(row.get("kind")):
            continue
        stamped = _as_utc(row.get("ts"))
        if since is not None and (stamped is None or stamped < since):
            continue
        kept.append(row)
    kept.sort(key=lambda row: _as_utc(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return [_public_event(row) for row in kept]


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(503, "History needs MongoDB")


@router.get("/api/history/status")
def history_status():
    return {"enabled": enabled(), "db": _db_name()}


@router.get("/api/history/events")
def history_events(kind: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)):
    _require_enabled()
    try:
        return recent(kind=kind or None, limit=limit)
    except Exception:
        raise HTTPException(503, "History needs MongoDB") from None


@router.get("/api/history/timeline")
def history_timeline(since: str | None = Query(None)):
    _require_enabled()
    try:
        return timeline(since)
    except ValueError:
        raise HTTPException(400, "since must be an ISO timestamp") from None
    except Exception:
        raise HTTPException(503, "History needs MongoDB") from None


@router.get("/api/history/buildings")
def history_buildings():
    _require_enabled()
    try:
        return list_buildings()
    except Exception:
        raise HTTPException(503, "History needs MongoDB") from None


@router.get("/api/history/buildings/{name}")
def history_building(name: str, version: int | None = Query(None)):
    _require_enabled()
    try:
        rec = _load_record(name, version)
    except Exception:
        raise HTTPException(503, "History needs MongoDB") from None
    if rec is None:
        raise HTTPException(404, f"Unknown building {name}")
    return rec
