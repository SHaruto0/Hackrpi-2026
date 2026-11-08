"""Commander live feed.

publish(kind, data) fans out to WebSocket clients (no-op when nobody is
listening or the loop is down) and always notifies subscribe(fn) callbacks.
"""
import asyncio
import json
import threading
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

_MAX_CLIENTS = 200
_PING_S = 20.0

_guard = threading.Lock()
_clients: set[WebSocket] = set()
_subscribers: list = []
_loop: asyncio.AbstractEventLoop | None = None
_ping_task: asyncio.Task | None = None


def _bind_loop() -> asyncio.AbstractEventLoop:
    global _loop
    loop = asyncio.get_running_loop()
    _loop = loop
    return loop


def _ensure_ping() -> None:
    global _ping_task
    if _ping_task is not None and not _ping_task.done():
        return
    _ping_task = asyncio.get_running_loop().create_task(_ping_loop())


def subscribe(fn) -> None:
    """Call fn(kind, data) on the publisher's thread for every publish()."""
    if not callable(fn):
        return
    with _guard:
        if fn not in _subscribers:
            _subscribers.append(fn)


def publish(kind: str, data: dict) -> None:
    """Queue one event from any thread. WebSocket send is a no-op when the loop is down or nobody is listening."""
    if not isinstance(data, dict):
        data = {}
    kind = str(kind)
    with _guard:
        subs = tuple(_subscribers)
    for fn in subs:
        try:
            fn(kind, data)
        except Exception:
            pass
    payload = {"kind": kind, "data": data, "ts": time.time()}
    try:
        text = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    loop = _loop
    if loop is None or not loop.is_running():
        return
    with _guard:
        if not _clients:
            return
    loop.call_soon_threadsafe(_enqueue, text)


def _enqueue(text: str) -> None:
    with _guard:
        if not _clients:
            return
    try:
        asyncio.get_running_loop().create_task(_broadcast(text))
    except RuntimeError:
        return


async def _broadcast(text: str) -> None:
    with _guard:
        targets = list(_clients)
    dead: list[WebSocket] = []
    for ws in targets:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    if not dead:
        return
    with _guard:
        for ws in dead:
            _clients.discard(ws)


async def _ping_loop() -> None:
    while True:
        await asyncio.sleep(_PING_S)
        _enqueue(json.dumps({"kind": "ping", "data": {}, "ts": time.time()}, separators=(",", ":")))


@router.on_event("startup")
async def _startup() -> None:
    _bind_loop()
    _ensure_ping()


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    _bind_loop()
    _ensure_ping()
    await websocket.accept()
    with _guard:
        if len(_clients) >= _MAX_CLIENTS:
            overflow = True
        else:
            _clients.add(websocket)
            overflow = False
    if overflow:
        await websocket.close(code=1013)
        return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        with _guard:
            _clients.discard(websocket)
