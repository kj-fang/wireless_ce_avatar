"""
Voice input: speech to text on this PC.

The page records a short clip from the microphone and posts it to
/assistant/transcribe; the text comes back into the input box for the user to
read and send. Recognition runs here, with faster-whisper on the CPU:

    * the audio never leaves this PC and is never written to disk - it is
      decoded from memory and dropped
    * nothing is sent on the user's behalf: transcription only fills the box

OPTIONAL. faster-whisper is not a requirement of the app. Without it (or
without its model) status() says what is missing and the page shows how to
enable it; everything else works unchanged.

    pip install faster-whisper          (once, in the app's Python)
    the model (about 480 MB) is downloaded once, on request, into
    <avatarfiles_dir>/assistant/models

The model stays loaded after first use (loading takes several seconds).
"""

from __future__ import annotations

import importlib.util
import io
import re
import threading
import time
from pathlib import Path
from typing import Optional

MODEL_NAME = "small"
MODEL_REPO = "Systran/faster-whisper-small"
MODEL_SIZE_MB = 480
MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_SECONDS = 120
MIN_SECONDS = 0.3              # shorter than this holds no word
SAMPLE_RATE = 16000
# Whisper writes Chinese in whichever script its prompt leans to; this leans
# it to Traditional and to the terms the user is likely to say.
ZH_PROMPT = "以下是繁體中文的句子，可能夾雜英文技術名詞，例如 Wi-Fi、Bluetooth、IPS case、driver、Echo。"

_lock = threading.Lock()            # one recognition at a time (CPU-bound)
_model = None
_download: dict = {"state": "idle", "error": "", "started": 0.0}
_download_lock = threading.RLock()  # re-entrant: start_download answers with status()


class VoiceUnavailable(RuntimeError):
    """Voice input cannot run; `code` is package_missing | model_missing |
    downloading | bad_audio."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---- seams (tests replace these) ---------------------------------------------

def _package_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def models_dir() -> Path:
    from configs.global_configs import app_config
    base = getattr(app_config, "avatarfiles_dir", None)
    if not base:
        raise VoiceUnavailable("model_missing", "the app's data folder is not set up yet")
    return Path(base) / "assistant" / "models"


def _load_model():
    import os
    from faster_whisper import WhisperModel
    # Eight threads measured about a quarter faster than the library's default
    # of four on a 16-thread laptop; beyond eight it no longer helps.
    return WhisperModel(MODEL_NAME, device="cpu", compute_type="int8",
                        download_root=str(models_dir()), local_files_only=True,
                        cpu_threads=max(1, min(8, os.cpu_count() or 4)))


DOWNLOAD_PROXY = "http://proxy-dmz.intel.com:912"     # the app's proxy for outside downloads


def _fetch_model() -> None:
    """Download the model files into models_dir() (the one network access of
    this module, and only when the user asked for it).

    First as the process is. Inside the app the proxy variables are often
    unset (other services clear them around their own calls), so a failed
    attempt is repeated in a child Python that has the proxy in ITS
    environment - this process's own environment is not touched, because the
    intranet calls of other threads must keep going direct."""
    target = str(models_dir())
    try:
        from faster_whisper import download_model
        download_model(MODEL_NAME, cache_dir=target)
        return
    except Exception as first:
        import os
        import subprocess
        import sys
        if getattr(sys, "frozen", False):
            raise                       # the packaged app has no Python to start
        env = dict(os.environ, HTTPS_PROXY=DOWNLOAD_PROXY, HTTP_PROXY=DOWNLOAD_PROXY,
                   https_proxy=DOWNLOAD_PROXY, http_proxy=DOWNLOAD_PROXY)
        code = ("import sys; from faster_whisper import download_model; "
                "download_model(sys.argv[1], cache_dir=sys.argv[2])")
        done = subprocess.run([sys.executable, "-c", code, MODEL_NAME, target], env=env,
                              capture_output=True, text=True, timeout=3600)
        if done.returncode != 0:
            tail = (done.stderr or "").strip().splitlines()[-1:] or [""]
            raise RuntimeError(f"direct: {type(first).__name__}: {first}; "
                               f"through the proxy: {tail[0][:200]}") from None


# ---- state -----------------------------------------------------------------------

def model_ready() -> bool:
    """Whether the model's files are on this PC (a finished download)."""
    try:
        root = models_dir()
    except VoiceUnavailable:
        return False
    return any(root.glob("**/model.bin"))


def status() -> dict:
    """What the page needs to decide how to show the microphone."""
    installed = _package_installed()
    ready = installed and model_ready()
    with _download_lock:
        download = dict(_download)
    if not installed:
        state, hint = "package_missing", (
            "Voice input needs the optional package faster-whisper. In the app's Python: "
            "pip install faster-whisper - then restart Avatar.")
    elif download["state"] == "running":
        state, hint = "downloading", f"Downloading the speech model (about {MODEL_SIZE_MB} MB) ..."
    elif not ready:
        state, hint = "model_missing", (
            f"The speech model is not on this PC yet (about {MODEL_SIZE_MB} MB, downloaded once "
            f"from Hugging Face: {MODEL_REPO}).")
    else:
        state, hint = "ready", "Speech is recognised on this PC; audio is not stored or sent anywhere."
    return {"state": state, "available": state == "ready", "hint": hint, "model": MODEL_NAME,
            "model_size_mb": MODEL_SIZE_MB, "download_error": download["error"],
            "loaded": _model is not None, "max_seconds": MAX_SECONDS}


def start_download() -> dict:
    """Fetch the model in the background (the user pressed Download)."""
    if not _package_installed():
        return status()
    with _download_lock:
        if _download["state"] == "running" or model_ready():
            return status()
        _download.update(state="running", error="", started=time.time())

    def work() -> None:
        error = ""
        try:
            models_dir().mkdir(parents=True, exist_ok=True)
            _fetch_model()
            if not model_ready():
                error = "the download finished but the model files are not there"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:400]
        with _download_lock:
            _download.update(state="failed" if error else "done", error=error)

    threading.Thread(target=work, daemon=True, name="assistant-voice-model-download").start()
    return status()


# ---- recognition -------------------------------------------------------------------

def _get_model():
    global _model
    if _model is None:
        _model = _load_model()
    return _model


def _decode(audio: bytes):
    """A recorded clip (WebM/Opus from the browser, or WAV, OGG, MP3 ...) as
    mono 16 kHz float32 samples - what the recogniser takes directly.

    Decoded here, from memory, with PyAV, instead of handing the container to
    faster-whisper: its own decoder passes an option that current PyAV
    releases no longer accept (found with faster-whisper 1.2.1 + av 19), and
    this way the clip's length is known before the model is run."""
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks: list = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("no audio in the recording")
        try:
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
        except Exception:
            # A clip cut off mid-frame (the recorder was stopped) still holds
            # everything said before the cut; only an unreadable start fails.
            if not chunks:
                raise
        for out in resampler.resample(None):            # what the resampler held back
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _join(segments) -> str:
    text = "".join(str(getattr(s, "text", "") or "") for s in segments)
    return re.sub(r"\s+", " ", text).strip()


def transcribe(audio: bytes, language: Optional[str] = None) -> dict:
    """Recognise one recorded clip. {"text", "language", "seconds", "took_s"}.

    language: None lets the model detect it (Chinese is then re-run with a
    Traditional-Chinese prompt), or an ISO code ("zh", "en") to force it.
    Raises VoiceUnavailable when voice input is not set up or the clip cannot
    be read."""
    if not _package_installed():
        raise VoiceUnavailable("package_missing", status()["hint"])
    if not audio:
        raise VoiceUnavailable("bad_audio", "the recording is empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceUnavailable("bad_audio", "the recording is too large (keep it under two minutes)")
    if _model is None and not model_ready():
        code = "downloading" if _download["state"] == "running" else "model_missing"
        raise VoiceUnavailable(code, status()["hint"])
    language = (str(language).strip().lower() or None) if language else None
    if language and not re.fullmatch(r"[a-z]{2,3}", language):
        language = None
    started = time.monotonic()
    with _lock:
        try:
            samples = _decode(audio)
        except Exception as e:
            raise VoiceUnavailable(
                "bad_audio", f"the recording could not be read ({type(e).__name__}: {e})"[:300]) from e
        seconds = len(samples) / SAMPLE_RATE
        if seconds > MAX_SECONDS + 5:
            raise VoiceUnavailable(
                "bad_audio", f"the recording is longer than {MAX_SECONDS} seconds")
        if seconds < MIN_SECONDS:
            return {"text": "", "language": language or "", "seconds": round(seconds, 1),
                    "took_s": round(time.monotonic() - started, 1)}
        model = _get_model()
        try:
            segments, info = model.transcribe(
                samples, language=language, vad_filter=True, beam_size=5,
                condition_on_previous_text=False,
                initial_prompt=ZH_PROMPT if language == "zh" else None)
            detected = str(getattr(info, "language", "") or language or "")
            if language is None and detected == "zh":
                # Detected Chinese: run it with the prompt that keeps the
                # output in Traditional characters. The first pass has only
                # detected the language so far (segments are produced lazily).
                segments, info = model.transcribe(
                    samples, language="zh", vad_filter=True, beam_size=5,
                    condition_on_previous_text=False, initial_prompt=ZH_PROMPT)
            text = _join(segments)
        except Exception as e:
            raise VoiceUnavailable(
                "bad_audio", f"recognition failed ({type(e).__name__}: {e})"[:300]) from e
    return {"text": text, "language": detected, "seconds": round(seconds, 1),
            "took_s": round(time.monotonic() - started, 1)}


def unload() -> None:
    """Drop the loaded model (tests; frees about 500 MB)."""
    global _model
    with _lock:
        _model = None
