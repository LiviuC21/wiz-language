"""audio.py - loopback (system output) capture + simple speech segmentation.

IMPORTANT (Windows): `soundcard` is imported lazily and only used inside the
capture thread. It initialises COM, and doing that on the Qt main thread can
clash with Qt's own COM setup.
"""
from __future__ import annotations

import logging
import queue
import warnings
from collections import deque
from typing import List, Optional

import numpy as np
from PyQt5 import QtCore

import config

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore", message=".*data discontinuity.*")


def _sc():
    import soundcard as sc  # lazy import, see module docstring
    return sc


def _is_loopback(mic) -> bool:
    """True for Windows WASAPI loopback devices and PulseAudio/PipeWire '.monitor' sources."""
    if getattr(mic, "isloopback", False):
        return True
    return "monitor" in str(mic.id).lower() or mic.name.lower().startswith("monitor of")


def list_loopback_devices() -> list:
    sc = _sc()
    return [m for m in sc.all_microphones(include_loopback=True) if _is_loopback(m)]


def pick_loopback_device(hint: Optional[str] = None):
    """Choose a loopback device: explicit name hint > monitor of default output > first one."""
    sc = _sc()
    devices = list_loopback_devices()
    if not devices:
        return None
    if hint:
        for d in devices:
            if hint.lower() in d.name.lower() or hint == str(d.id):
                return d
        log.warning("Device hint %r not found, falling back to default", hint)
    try:
        spk = sc.default_speaker()
        for d in devices:
            if str(d.id) in (str(spk.id), f"{spk.id}.monitor") or spk.name in d.name:
                return d
    except Exception:  # noqa: BLE001
        log.exception("Could not resolve default speaker")
    return devices[0]


class SpeechSegmenter:
    """Energy-based phrase detector. Whisper's own VAD then cleans up each segment."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buf: List[np.ndarray] = []
        self._pre = deque(maxlen=config.PRE_ROLL_CHUNKS)
        self._active = False
        self._silence = 0.0
        self._speech = 0.0
        self._total = 0.0

    def push(self, chunk: np.ndarray) -> Optional[np.ndarray]:
        if chunk.size == 0:
            return None
        dur = chunk.size / config.SAMPLE_RATE
        loud = float(np.sqrt(np.mean(np.square(chunk)))) > config.VAD_RMS_THRESHOLD

        if not self._active:
            if not loud:
                self._pre.append(chunk)
                return None
            self._active = True
            self._buf = list(self._pre) + [chunk]
            self._total = sum(c.size for c in self._buf) / config.SAMPLE_RATE
            self._speech, self._silence = dur, 0.0
        else:
            self._buf.append(chunk)
            self._total += dur
            if loud:
                self._speech += dur
                self._silence = 0.0
            else:
                self._silence += dur

        if self._silence >= config.SILENCE_END_SECONDS or self._total >= config.MAX_SEGMENT_SECONDS:
            return self._flush()
        return None

    def _flush(self) -> Optional[np.ndarray]:
        audio = np.concatenate(self._buf)
        enough = self._speech >= config.MIN_SPEECH_SECONDS
        self.reset()
        return audio if enough else None


def put_drop_oldest(q: "queue.Queue", item) -> None:
    """Keep latency low: if the consumer is behind, discard the oldest item."""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


class AudioCaptureThread(QtCore.QThread):
    """Continuously records system output and pushes speech segments to a queue."""

    status = QtCore.pyqtSignal(str)
    devices_found = QtCore.pyqtSignal(list)   # list[str] of loopback device names
    device_in_use = QtCore.pyqtSignal(str)

    def __init__(self, out_queue: "queue.Queue", device_hint: Optional[str] = None) -> None:
        super().__init__()
        self._out = out_queue
        self._hint = device_hint
        self._stop_flag = False
        self._reopen = False

    # -- control (called from the GUI thread) --
    def set_device(self, hint: Optional[str]) -> None:
        self._hint = hint
        self._reopen = True

    def stop(self) -> None:
        self._stop_flag = True

    # -- thread body --
    def run(self) -> None:
        while not self._stop_flag:
            self._reopen = False
            try:
                devices = list_loopback_devices()
                self.devices_found.emit([d.name for d in devices])
                device = pick_loopback_device(self._hint)
            except Exception as exc:  # noqa: BLE001
                log.exception("Device enumeration failed")
                self.status.emit(f"Audio init failed: {exc}")
                self._sleep(2.0)
                continue

            if device is None:
                self.status.emit("No loopback device - right-click to rescan")
                self._sleep(2.0)
                continue

            self.device_in_use.emit(device.name)
            try:
                self._capture(device)
            except Exception as exc:  # noqa: BLE001
                log.exception("Capture failed")
                self.status.emit(f"Audio error: {exc} - retrying")
                self._sleep(2.0)

    def _capture(self, device) -> None:
        frames = int(config.SAMPLE_RATE * config.CHUNK_SECONDS)
        segmenter = SpeechSegmenter()
        self.status.emit(f"Listening: {device.name}")
        # channels=None -> all device channels; we downmix to mono ourselves.
        with device.recorder(samplerate=config.SAMPLE_RATE) as rec:
            while not self._stop_flag and not self._reopen:
                data = rec.record(numframes=frames)
                mono = data.mean(axis=1) if data.ndim == 2 else data
                mono = np.ascontiguousarray(mono, dtype=np.float32)
                segment = segmenter.push(mono)
                if segment is not None:
                    put_drop_oldest(self._out, segment)

    def _sleep(self, seconds: float) -> None:
        for _ in range(int(seconds * 10)):
            if self._stop_flag or self._reopen:
                return
            self.msleep(100)
