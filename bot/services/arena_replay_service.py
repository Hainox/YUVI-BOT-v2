"""Детерминированный MP4-renderer для Arena replay.

Источник истины — ``ArenaMatchEvent.payload``. Redis/client state сюда не
попадает. Кадры рисуются стандартной библиотекой в raw RGB и кодируются
системным ffmpeg; Pillow/OpenCV не нужны. Рендер запускается в thread pool,
поэтому ежедневный пост и aiogram event loop не блокируются.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


WIDTH = 720
HEIGHT = 405
FPS = 12
MAX_OUTPUT_BYTES = 49_000_000
FRAMES_PER_EVENT = 8


class ReplayRenderError(RuntimeError):
    """Видео нельзя собрать в текущем окружении."""


def _number(payload: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return default
    return default


def _action(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value).replace("_", " ").upper()[:16]
    return "WAIT"


def _rect(frame: bytearray, x: int, y: int, width: int, height: int, color: tuple[int, int, int]) -> None:
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(WIDTH, x + width), min(HEIGHT, y + height)
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(color) * (x1 - x0)
    stride = WIDTH * 3
    for row_y in range(y0, y1):
        start = row_y * stride + x0 * 3
        frame[start : start + len(row)] = row


def _circle(frame: bytearray, cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
    for y in range(max(0, cy - radius), min(HEIGHT, cy + radius + 1)):
        dy = y - cy
        half = int((radius * radius - dy * dy) ** 0.5) if abs(dy) <= radius else 0
        _rect(frame, cx - half, y, half * 2 + 1, 1, color)


def _bar(frame: bytearray, x: int, y: int, width: int, value: int, maximum: int, color: tuple[int, int, int]) -> None:
    _rect(frame, x, y, width, 14, (31, 37, 63))
    fill = int(width * min(1.0, max(0, value) / max(1, maximum)))
    _rect(frame, x, y, fill, 14, color)


def _frame(event: dict[str, Any], match: Any, frame_index: int) -> bytes:
    payload = event.get("payload") or {}
    frame = bytearray(bytes((9, 12, 29)) * (WIDTH * HEIGHT))
    # Neon grid / stage.
    _rect(frame, 0, 250, WIDTH, 155, (14, 20, 43))
    for x in range(0, WIDTH, 48):
        _rect(frame, x, 250, 1, 155, (25, 33, 60))
    for y in range(270, HEIGHT, 34):
        _rect(frame, 0, y, WIDTH, 1, (25, 33, 60))
    _rect(frame, 28, 30, 664, 2, (82, 92, 164))
    _rect(frame, 28, 34, 170, 3, (43, 218, 211))
    _rect(frame, 522, 34, 170, 3, (255, 84, 151))

    player_hp = _number(payload, "player_hp", "player_hp_after", "player_hp_remaining", default=100)
    opponent_hp = _number(payload, "opponent_hp", "opponent_hp_after", "opponent_hp_remaining", default=100)
    player_max = _number(payload, "player_max_hp", default=max(player_hp, 100))
    opponent_max = _number(payload, "opponent_max_hp", default=max(opponent_hp, 100))
    player_damage = _number(payload, "player_damage")
    opponent_damage = _number(payload, "opponent_damage")
    phase = _number(event, "phase_index", default=_number(payload, "phase_index"))

    _bar(frame, 36, 58, 285, player_hp, player_max, (43, 218, 211))
    _bar(frame, 399, 58, 285, opponent_hp, opponent_max, (255, 84, 151))
    # Fighter silhouettes: head, torso, energy core and a motion trail on hit.
    pulse = 4 if frame_index % 8 < 4 else 0
    _circle(frame, 185, 164, 34 + pulse, (43, 218, 211))
    _rect(frame, 133, 198, 104, 112, (29, 115, 142))
    _rect(frame, 154, 218, 62, 62, (43, 218, 211))
    _circle(frame, 535, 164, 34 + pulse, (255, 84, 151))
    _rect(frame, 483, 198, 104, 112, (138, 39, 101))
    _rect(frame, 504, 218, 62, 62, (255, 84, 151))
    if player_damage or opponent_damage:
        _rect(frame, 320, 206, 80, 8, (255, 220, 91))
        _rect(frame, 300, 222, 120, 4, (255, 122, 76))
    if payload.get("player_special") or payload.get("opponent_special"):
        _circle(frame, 360, 208, 48, (255, 220, 91))
        _circle(frame, 360, 208, 33, (23, 26, 53))

    # A small event timeline is intentionally visual-only; all values above
    # came from the signed/unique event ledger.
    _rect(frame, 36, 350, min(648, max(10, phase * 18)), 5, (255, 220, 91))
    return bytes(frame)


def _render_sync(events: list[dict[str, Any]], match: Any, output_path: Path) -> None:
    if not events:
        raise ReplayRenderError("У матча нет событий для replay")
    events = sorted(events, key=lambda event: (int(event.get("sequence", 0)), int(event.get("phase_index", 0))))[:90]
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
        "-r", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
        "-b:v", "800k", "-movflags", "+faststart", str(output_path),
    ]
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        for event in events:
            for frame_index in range(FRAMES_PER_EVENT):
                process.stdin.write(_frame(event, match, frame_index))
        process.stdin.close()
        process.wait(timeout=120)
        error_output = process.stderr.read() if process.stderr is not None else b""
    except (FileNotFoundError, BrokenPipeError, subprocess.TimeoutExpired) as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise ReplayRenderError("ffmpeg недоступен или превысил лимит времени") from exc
    if process.returncode != 0:
        error = error_output.decode("utf-8", "replace")[-400:]
        raise ReplayRenderError(f"ffmpeg завершился с ошибкой: {error}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ReplayRenderError("ffmpeg не создал MP4")
    if output_path.stat().st_size > MAX_OUTPUT_BYTES:
        raise ReplayRenderError("MP4 превышает безопасный лимит Telegram")


@asynccontextmanager
async def render_match_replay(events: list[dict[str, Any]], match: Any):
    """Собирает временный MP4 и удаляет его после отправки в Telegram."""
    with tempfile.TemporaryDirectory(prefix="arena-replay-") as directory:
        output_path = Path(directory) / f"arena-{getattr(match, 'id', 'match')}.mp4"
        await asyncio.to_thread(_render_sync, events, match, output_path)
        try:
            yield output_path
        finally:
            if output_path.exists():
                output_path.unlink(missing_ok=True)


def event_payload(event: Any) -> dict[str, Any]:
    """Normalize ORM event for renderer/tests without leaking ORM objects."""
    return {
        "sequence": int(getattr(event, "sequence", 0)),
        "phase_index": int(getattr(event, "phase_index", 0)),
        "event_type": str(getattr(event, "event_type", "phase_resolved")),
        "payload": dict(getattr(event, "payload", None) or {}),
    }


def render_events_to_mp4(events: list[Any], match: Any):
    """Public convenience API returning the same cleanup context manager."""
    return render_match_replay([event_payload(event) for event in events], match)
