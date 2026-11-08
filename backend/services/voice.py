"""Fish Audio TTS for occupant and firefighter lines.

Env (never log these):
    FISH_API_KEY       required to synthesize. Missing key or API failure -> 503.
    FISH_MODEL         model header (default s2.1-pro)
    FISH_VOICE_ID      the one voice for every role. Omit reference_id when unset.

Clips are cached at data/tts_cache/<sha1(style|voice|text)>.mp3.
Same text, style, and voice id are served from disk with no API call.
Text longer than 300 characters is rejected. The cache keeps at most 500 files.
"""
import hashlib
import json
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

_TTS_URL = "https://api.fish.audio/v1/tts"
_DEFAULT_MODEL = "s2.1-pro"
_TIMEOUT_S = 15.0
_SPEED = {"occupant": 0.9, "firefighter": 1.15}
_MAX_TEXT_CHARS = 300
_MAX_CACHE_FILES = 500

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(ROOT, "data", "tts_cache")

COMMON_PHRASES = (
    "Turn left.",
    "Turn right.",
    "Go straight.",
    "Take the stairs down.",
    "Take the stairs up.",
    "Smoke ahead. Stay low.",
    "Fire ahead. Turn around.",
    "Route changed.",
    "You are out. Go to the assembly point.",
    "Exit the room, turn left.",
    "Exit the room, turn right.",
    "East stairs blocked. Use the west stairs.",
    "Head to the west stairs.",
    "The elevator is not for evacuation.",
    "Point the camera at a door number.",
)

router = APIRouter()

_WALK_HALL = re.compile(
    r"Walk\s+(\d+(?:\.\d+)?)\s+m\s+through\s+the\s+hallway\s+\(([^)]+)\)",
    re.IGNORECASE,
)
_WALK_ANY = re.compile(
    r"Walk\s+(\d+(?:\.\d+)?)\s+m\s+through\s+(.+)",
    re.IGNORECASE,
)
_STAIR = re.compile(
    r"Take\s+(.+?)\s+(up|down)\s+to\s+floor\s+(\S+)",
    re.IGNORECASE,
)
_EXIT = re.compile(
    r"Exit\s+the\s+building\s+at\s+(.+)",
    re.IGNORECASE,
)
_METERS = re.compile(r"\b(\d+(?:\.\d+)?)\s+m\b", re.IGNORECASE)


def style_text(text, style):
    """Shape a line for speech.

    Occupant lines stay short and calm: "Exit the room, turn left."
    Firefighter lines get compressed: "Walk 12 m through the hallway (317)"
    becomes "Hallway 317, 12 meters."
    """
    if style not in _SPEED:
        raise ValueError("style must be occupant or firefighter")
    raw = " ".join((text or "").split())
    if not raw:
        return ""
    if style == "occupant":
        return _calm(raw)
    parts = [_firefighter_sentence(part) for part in _sentences(raw)]
    return " ".join(part for part in parts if part)


def _sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]


def _calm(text):
    text = text.replace("!", ".")
    text = re.sub(r"\.{2,}", ".", text)
    letters = [ch for ch in text if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        text = text.capitalize()
    if text[-1] not in ".?":
        text += "."
    return text


def _meters(num):
    value = float(num)
    shown = str(int(value)) if value.is_integer() else f"{value:g}"
    unit = "meter" if abs(value - 1.0) < 1e-9 else "meters"
    return f"{shown} {unit}"


def _firefighter_sentence(sentence):
    core = sentence.strip().rstrip(".!?").strip()
    match = _WALK_HALL.fullmatch(core)
    if match:
        return f"Hallway {match.group(2).strip()}, {_meters(match.group(1))}."
    match = _WALK_ANY.fullmatch(core)
    if match:
        place = match.group(2).strip()
        if place:
            place = place[0].upper() + place[1:]
        return f"{place}, {_meters(match.group(1))}."
    match = _STAIR.fullmatch(core)
    if match:
        return f"{match.group(1).strip()} {match.group(2).lower()}, floor {match.group(3)}."
    match = _EXIT.fullmatch(core)
    if match:
        return f"Exit at {match.group(1).strip()}."
    core = _METERS.sub(lambda m: _meters(m.group(1)), core)
    if core and core[-1] not in ".?":
        core += "."
    return core


def _voice_id(style):
    occupant = os.environ.get("FISH_VOICE_ID", "").strip()
    return occupant


def _cache_path(style, voice, text):
    digest = hashlib.sha1(f"{style}|{voice}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.mp3")


def _api_key():
    return os.environ.get("FISH_API_KEY", "").strip()


def _redact(message):
    key = _api_key()
    if key and key in message:
        message = message.replace(key, "[redacted]")
    return message


def _detail_from_body(raw):
    message = "Fish Audio TTS failed"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return message
    if isinstance(data, dict):
        found = data.get("message")
        if isinstance(found, str) and found.strip():
            message = found.strip()
    return _redact(message)


def _read_cache(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        data = handle.read()
    return data or None


def _write_cache(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _fetch_mp3(text, style, voice):
    key = _api_key()
    if not key:
        raise HTTPException(503, "FISH_API_KEY is not set")
    payload = {
        "text": text,
        "format": "mp3",
        "latency": "balanced",
        "prosody": {"speed": _SPEED[style], "volume": 0},
    }
    if voice:
        payload["reference_id"] = voice
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "model": os.environ.get("FISH_MODEL", "").strip() or _DEFAULT_MODEL,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT_S) as client:
            with client.stream("POST", _TTS_URL, headers=headers, json=payload) as resp:
                raw = resp.read()
                status = resp.status_code
    except httpx.TimeoutException:
        raise HTTPException(503, "Fish Audio TTS timed out")
    except httpx.HTTPError:
        raise HTTPException(503, "Fish Audio TTS request failed")
    if status != 200:
        raise HTTPException(503, _detail_from_body(raw))
    if not raw:
        raise HTTPException(503, "Fish Audio TTS returned no audio")
    return raw


def _evict_cache(keep=None):
    """Keep at most _MAX_CACHE_FILES mp3s. Delete the oldest by mtime."""
    try:
        entries = []
        with os.scandir(CACHE_DIR) as scan:
            for ent in scan:
                if not ent.name.endswith(".mp3"):
                    continue
                try:
                    entries.append((ent.stat().st_mtime, ent.path))
                except OSError:
                    continue
        overflow = len(entries) - _MAX_CACHE_FILES
        if overflow <= 0:
            return
        entries.sort()
        keep_abs = os.path.abspath(keep) if keep else None
        for _mtime, path in entries:
            if overflow <= 0:
                break
            if keep_abs and os.path.abspath(path) == keep_abs:
                continue
            try:
                os.remove(path)
                overflow -= 1
            except OSError:
                pass
    except Exception:
        return


def _cached_audio(text, style):
    if style not in _SPEED:
        raise HTTPException(400, "style must be occupant or firefighter")
    raw = text if isinstance(text, str) else str(text or "")
    if len(raw) > _MAX_TEXT_CHARS:
        raise HTTPException(422, "text is too long")
    spoken = style_text(raw, style)
    if not spoken:
        raise HTTPException(400, "text is empty")
    voice = _voice_id(style)
    path = _cache_path(style, voice, raw)
    cached = _read_cache(path)
    if cached is not None:
        _evict_cache(path)
        return cached
    data = _fetch_mp3(spoken, style, voice)
    _write_cache(path, data)
    _evict_cache(path)
    return data


@router.get("/api/voice/tts")
def tts(text: str = Query(...), style: str = Query(...)):
    """Return an MP3 for text. style is occupant (calm) or firefighter (terse)."""
    return Response(content=_cached_audio(text, style), media_type="audio/mpeg")


@router.post("/api/voice/prewarm")
def prewarm():
    """Synthesize COMMON_PHRASES for both styles into the disk cache."""
    cached = 0
    for style in ("occupant", "firefighter"):
        for phrase in COMMON_PHRASES:
            _cached_audio(phrase, style)
            cached += 1
    return {"cached": cached}
