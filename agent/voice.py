"""Voice notes: Whisper for what comes in, edge-tts and ffmpeg for what goes out.

Both halves are optional and degrade to text on their own:

- listening needs STT_BASE_URL, STT_API_KEY and STT_MODEL in .env
- speaking needs the edge-tts package (no key) and ffmpeg on PATH, because
  Telegram voice notes have to be ogg/opus
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from .config import Settings
from .journal import Journal

#: Opus at 32k mono is what Telegram's own clients send.
FFMPEG_ARGS = ("-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1")


@dataclass
class VoiceClip:
    path: Path
    seconds: int


class Voice:
    """Speech in and out, with every failure landing back on text."""

    def __init__(self, settings: Settings, journal: Journal, workdir: Path) -> None:
        self.journal = journal
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.tts_voice = settings.tts_voice
        self.stt_model = settings.stt_model
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self._counter = 0

        self._stt: OpenAI | None = None
        if settings.stt_base_url and settings.stt_api_key and settings.stt_model:
            self._stt = OpenAI(
                base_url=settings.stt_base_url,
                api_key=settings.stt_api_key,
                timeout=60.0,
                max_retries=1,
            )

        self._tts_import_error: str | None = None
        try:
            import edge_tts  # noqa: F401
        except Exception as exc:  # not installed
            self._tts_import_error = str(exc)

    # --- what is available ------------------------------------------------

    @property
    def can_listen(self) -> bool:
        return self._stt is not None

    @property
    def can_speak(self) -> bool:
        return self._tts_import_error is None and self.ffmpeg is not None

    def problems(self) -> list[str]:
        """Plain-English reasons why voice is off, for the startup banner."""
        notes: list[str] = []
        if not self.can_listen:
            notes.append(
                "Voice messages cannot be transcribed: set STT_BASE_URL, "
                "STT_API_KEY and STT_MODEL in .env. The agent will ask the "
                "manager to write instead."
            )
        if self._tts_import_error is not None:
            notes.append(
                "edge-tts is not installed, so the agent cannot send voice "
                "notes: pip install -r requirements.txt. Text still works."
            )
        elif self.ffmpeg is None:
            notes.append(
                "ffmpeg is not on PATH, so voice notes cannot be encoded as "
                "ogg/opus. Install ffmpeg (winget install Gyan.FFmpeg) and "
                "reopen the terminal. The agent will reply in text."
            )
        return notes

    # --- listening --------------------------------------------------------

    async def transcribe(self, path: Path) -> str | None:
        if self._stt is None:
            return None
        started = time.monotonic()
        try:
            text = await asyncio.to_thread(self._transcribe_blocking, path)
        except Exception as exc:
            self.journal.write(
                "stt_failed", level="warning", error=str(exc)[:300], file=path.name
            )
            return None
        text = (text or "").strip()
        if not text:
            self.journal.write("stt_failed", level="warning", error="empty transcript")
            return None
        self.journal.write(
            "stt_done",
            model=self.stt_model,
            seconds=round(time.monotonic() - started, 1),
            chars=len(text),
        )
        return text

    def _transcribe_blocking(self, path: Path) -> str:
        assert self._stt is not None
        with path.open("rb") as handle:
            result = self._stt.audio.transcriptions.create(
                model=self.stt_model,
                file=(path.name, handle.read()),
                language="en",
            )
        return getattr(result, "text", "") or ""

    # --- speaking ---------------------------------------------------------

    async def speak(self, text: str) -> VoiceClip | None:
        """Returns an ogg/opus clip, or None - in which case send text."""
        if not self.can_speak:
            return None
        import edge_tts

        self._counter += 1
        stem = f"out-{int(time.time())}-{self._counter}"
        mp3 = self.workdir / f"{stem}.mp3"
        ogg = self.workdir / f"{stem}.ogg"
        started = time.monotonic()
        try:
            await edge_tts.Communicate(text, self.tts_voice).save(str(mp3))
            await self._encode(mp3, ogg)
        except Exception as exc:
            self.journal.write(
                "tts_failed", level="warning", error=str(exc)[:300], text=text[:200]
            )
            return None
        if not ogg.exists() or ogg.stat().st_size == 0:
            self.journal.write("tts_failed", level="warning", error="empty ogg")
            return None

        clip = VoiceClip(ogg, await self._duration(ogg))
        self.journal.write(
            "tts_done",
            voice=self.tts_voice,
            seconds=round(time.monotonic() - started, 1),
            duration=clip.seconds,
            bytes=ogg.stat().st_size,
        )
        return clip

    async def _encode(self, mp3: Path, ogg: Path) -> None:
        assert self.ffmpeg is not None
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-y", "-i", str(mp3), *FFMPEG_ARGS, str(ogg),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited {process.returncode}: {stderr.decode(errors='replace')[-300:]}"
            )

    async def _duration(self, path: Path) -> int:
        """Best effort: a voice note with no duration shows as 0:00."""
        if not self.ffprobe:
            return 0
        try:
            process = await asyncio.create_subprocess_exec(
                self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            return max(0, round(float(stdout.decode().strip())))
        except Exception:
            return 0
