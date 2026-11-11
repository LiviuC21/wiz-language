# ---------------------------------------------------------------------------------------
# REQUIREMENTS
#   all platforms : pip install numpy PyQt5 "soundcard>=0.4.4"
#   Linux         : pip install pulsectl
#                   + the `parec` binary:  Debian/Ubuntu: sudo apt install pulseaudio-utils
#                                          CachyOS/Arch : sudo pacman -S libpulse
#   Windows       : pip install proc-tap pycaw psutil        (Windows 10 2004 / build 19041+)
# ---------------------------------------------------------------------------------------
"""audio.py - per-application audio capture with automatic fallback to system loopback.

How it works
  * `device_hint` (constructor / set_device) is now an APPLICATION NAME (case-insensitive,
    substring match). `None` means "system loopback" (the old behaviour).
    `devices_found` emits the names of applications that are currently playing sound.
  * Linux  : pulsectl lists the active sink-inputs. The chosen stream is recorded with
             `parec --monitor-stream=<sink-input>`, which taps ONE stream without changing
             your audio routing (works on PulseAudio and PipeWire's pulse layer).
  * Windows: pycaw lists the active WASAPI sessions (name + PID). Isolation uses WASAPI
             *process loopback* via `proc-tap` (Windows 10 2004+). soundcard/pycaw alone
             cannot isolate one app, so they are only used for listing and for the fallback.
  * Fallback: if the app isn't playing, a library/binary is missing, the OS is too old, or
             the capture fails, we record the default system loopback instead and keep
             watching for the app to (re)appear. After a failure we wait APP_RETRY_COOLDOWN
             seconds before trying app capture again.

Caveats: if one application owns several streams, the first one is captured. This module
was written without access to live audio hardware - verify on your machines.
"""
from __future__ import annotations

import logging
import os
import queue
import select
import shutil
import subprocess
import sys
import time
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np
from PyQt5 import QtCore

import config

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore", message=".*data discontinuity.*")

APP_SCAN_INTERVAL = 3.0      # seconds between refreshes of the "apps playing sound" list
APP_RETRY_COOLDOWN = 30.0    # seconds to wait before retrying app capture after a failure


# =======================================================================================
# Small helpers
# =======================================================================================
def _short(exc: BaseException, limit: int = 70) -> str:
    text = str(exc).strip().replace("\n", " ") or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AppStreamEnded(Exception):
    """The captured application closed its audio stream / exited (not a real error)."""


@dataclass(frozen=True)
class AppStream:
    """One application currently playing audio."""
    name: str
    pid: Optional[int] = None
    sink_input: Optional[int] = None       # Linux: PulseAudio sink-input index
    monitor_source: Optional[str] = None   # Linux: monitor source of the sink it plays on


def pick_app(apps: List[AppStream], hint: Optional[str]) -> Optional[AppStream]:
    """Exact (case-insensitive) name match first, then substring match."""
    if not hint:
        return None
    h = hint.lower()
    for a in apps:
        if a.name.lower() == h:
            return a
    for a in apps:
        if h in a.name.lower():
            return a
    return None


# =======================================================================================
# System loopback (fallback path) - unchanged behaviour
# =======================================================================================
def _sc():
    import soundcard as sc  # lazy import: on Windows keep COM usage inside the capture thread
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


# =======================================================================================
# Speech segmentation / chunking utilities
# =======================================================================================
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


class ChunkAssembler:
    """Re-slices arbitrarily sized mono audio into fixed-size chunks (e.g. 100 ms)."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._buf = np.zeros(0, dtype=np.float32)

    def push(self, samples: np.ndarray) -> List[np.ndarray]:
        if samples.size == 0:
            return []
        self._buf = np.concatenate([self._buf, samples]) if self._buf.size else samples
        out: List[np.ndarray] = []
        while self._buf.size >= self._size:
            out.append(self._buf[: self._size])
            self._buf = self._buf[self._size:]
        return out


class StreamResampler:
    """Mono streaming resampler to config.SAMPLE_RATE.
    Integer ratios (48k->16k) use a boxcar average (cheap low-pass + decimate) with the
    remainder carried between calls; other ratios fall back to per-chunk linear interpolation."""

    def __init__(self, src_rate: int, dst_rate: int = config.SAMPLE_RATE) -> None:
        self.src, self.dst = int(src_rate), int(dst_rate)
        self._factor = self.src // self.dst if self.src % self.dst == 0 else None
        self._carry = np.zeros(0, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.src == self.dst or x.size == 0:
            return x
        if self._factor:
            f = self._factor
            x = np.concatenate([self._carry, x])
            n = (x.size // f) * f
            self._carry = x[n:]
            if n == 0:
                return np.zeros(0, dtype=np.float32)
            return x[:n].reshape(-1, f).mean(axis=1).astype(np.float32)
        n_out = int(x.size * self.dst / self.src)
        if n_out == 0:
            return np.zeros(0, dtype=np.float32)
        pos = np.linspace(0, x.size - 1, n_out)
        return np.interp(pos, np.arange(x.size), x).astype(np.float32)


# =======================================================================================
# Per-application backends
#   interface:  list_apps() -> List[AppStream]
#               capture(app, emit, keep_going)   (blocks; raises on failure,
#                                                 AppStreamEnded if the app went away)
#               close()
#   `emit(mono_float32_at_SAMPLE_RATE)` receives audio, `keep_going()` -> bool.
# =======================================================================================
class PulseAppBackend:
    """Linux: pulsectl for discovery, `parec --monitor-stream` for single-stream capture."""

    def __init__(self) -> None:
        import pulsectl  # ImportError is handled by create_app_backend()
        self._pulsectl = pulsectl
        self._pulse = None

    def _conn(self):
        if self._pulse is None:
            self._pulse = self._pulsectl.Pulse("live-translator-scan")
        return self._pulse

    def list_apps(self) -> List[AppStream]:
        try:
            pulse = self._conn()
            sinks = {s.index: s for s in pulse.sink_list()}
            apps: List[AppStream] = []
            for si in pulse.sink_input_list():
                if getattr(si, "corked", 0):          # paused streams aren't "playing"
                    continue
                sink = sinks.get(si.sink)
                if sink is None:
                    continue
                props = si.proplist or {}
                pid = _to_int(props.get("application.process.id"))
                if pid == os.getpid():
                    continue
                name = (props.get("application.name")
                        or props.get("application.process.binary")
                        or si.name or f"stream {si.index}")
                apps.append(AppStream(name=str(name), pid=pid, sink_input=si.index,
                                      monitor_source=sink.monitor_source_name))
            return apps
        except Exception:
            self.close()      # drop the connection so the next scan reconnects
            raise

    def capture(self, app: AppStream, emit: Callable[[np.ndarray], None],
                keep_going: Callable[[], bool]) -> None:
        if not shutil.which("parec"):
            raise RuntimeError("`parec` not found (install pulseaudio-utils / libpulse)")
        cmd = ["parec", "--raw",
               f"--monitor-stream={app.sink_input}",
               f"--device={app.monitor_source}",
               f"--rate={config.SAMPLE_RATE}", "--channels=1", "--format=float32le",
               "--latency-msec=100", "--client-name=live-translator",
               "--stream-name=app-capture"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            time.sleep(0.4)                       # unsupported setups die immediately
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(err or f"parec exited with code {proc.returncode}")

            fd = proc.stdout.fileno()
            pending = bytearray()
            while keep_going():
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:                     # silence: parec sends nothing while idle
                    if proc.poll() is not None:
                        raise AppStreamEnded("parec exited")
                    continue
                data = os.read(fd, 65536)
                if not data:
                    raise AppStreamEnded("stream closed")
                pending.extend(data)
                usable = len(pending) - (len(pending) % 4)
                if usable:
                    emit(np.frombuffer(bytes(pending[:usable]), dtype="<f4"))
                    del pending[:usable]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    def close(self) -> None:
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:  # noqa: BLE001
                pass
            self._pulse = None


class WasapiAppBackend:
    """Windows: pycaw lists audio sessions, proc-tap does WASAPI process-loopback capture."""

    MIN_BUILD = 19041  # Windows 10 2004

    def __init__(self) -> None:
        sys.coinit_flags = 0          # COM in MTA mode, consistent with soundcard
        import comtypes
        import psutil
        from pycaw.pycaw import AudioUtilities
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                      # already initialised on this thread
        self._utils = AudioUtilities
        self._psutil = psutil

    def list_apps(self) -> List[AppStream]:
        apps: List[AppStream] = []
        for s in self._utils.GetAllSessions():
            proc = getattr(s, "Process", None)
            if proc is None:                      # "System sounds" session
                continue
            try:
                if getattr(s, "State", 1) != 1:   # 1 = AudioSessionStateActive
                    continue
                name = proc.name()
                pid = int(s.ProcessId)
            except Exception:  # noqa: BLE001
                continue
            if pid == os.getpid():
                continue
            if name.lower().endswith(".exe"):
                name = name[:-4]
            apps.append(AppStream(name=name, pid=pid))
        return apps

    def capture(self, app: AppStream, emit: Callable[[np.ndarray], None],
                keep_going: Callable[[], bool]) -> None:
        if sys.getwindowsversion().build < self.MIN_BUILD:
            raise RuntimeError("per-app capture needs Windows 10 2004 or newer")
        try:
            from proctap import ProcTap
        except ImportError as exc:
            raise RuntimeError("proc-tap not installed (pip install proc-tap)") from exc

        tap = ProcTap(pid=app.pid)
        try:
            tap.start()
            if not getattr(tap, "is_running", True):
                raise RuntimeError("process loopback failed to start")
            rate, channels, dtype = self._stream_format(tap)
            resampler = StreamResampler(rate)
            itemsize = np.dtype(dtype).itemsize
            last_alive_check = time.monotonic()

            while keep_going():
                now = time.monotonic()
                if now - last_alive_check > 2.0:
                    last_alive_check = now
                    if not self._psutil.pid_exists(app.pid):
                        raise AppStreamEnded("process exited")
                raw = tap.read(timeout=0.5)
                if not raw:
                    continue                      # timeout / silence
                usable = len(raw) - (len(raw) % (itemsize * channels))
                if usable <= 0:
                    continue
                samples = np.frombuffer(raw[:usable], dtype=dtype)
                if dtype == np.int16:
                    samples = samples.astype(np.float32) / 32768.0
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)
                emit(resampler.process(samples.astype(np.float32, copy=False)))
        finally:
            try:
                tap.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _stream_format(tap) -> Tuple[int, int, type]:
        """Documented Windows format: 48 kHz, stereo, float32 (int16 if the API says so)."""
        fmt = {}
        try:
            fmt = tap.get_format() or {}
        except Exception:  # noqa: BLE001
            pass
        rate = _to_int(fmt.get("sample_rate")) or 48000
        channels = _to_int(fmt.get("channels")) or 2
        bits = _to_int(fmt.get("bits_per_sample")) or 32
        return rate, channels, (np.int16 if bits == 16 else np.float32)

    def close(self) -> None:
        pass


def create_app_backend() -> Tuple[Optional[object], Optional[str]]:
    """Return (backend, None) or (None, human-readable reason app capture is unavailable)."""
    try:
        if sys.platform.startswith("linux"):
            return PulseAppBackend(), None
        if sys.platform == "win32":
            return WasapiAppBackend(), None
        return None, "per-app capture unsupported on this OS"
    except ImportError as exc:
        return None, f"app capture unavailable: {_short(exc)}"
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not create app backend")
        return None, f"app capture unavailable: {_short(exc)}"


def list_active_apps() -> List[str]:
    """Convenience for CLI use: names of applications currently playing sound."""
    backend, _ = create_app_backend()
    if backend is None:
        return []
    try:
        return sorted({a.name for a in backend.list_apps()}, key=str.lower)
    finally:
        backend.close()


# =======================================================================================
# Capture thread
# =======================================================================================
class AudioCaptureThread(QtCore.QThread):
    """Records one application (or the system loopback) and pushes speech segments to a queue."""

    status = QtCore.pyqtSignal(str)
    devices_found = QtCore.pyqtSignal(list)   # list[str]: applications currently playing sound
    device_in_use = QtCore.pyqtSignal(str)    # name of the app / loopback device being recorded

    def __init__(self, out_queue: "queue.Queue", device_hint: Optional[str] = None) -> None:
        super().__init__()
        self._out = out_queue
        self._hint = device_hint              # application name, or None = system loopback
        self._stop_flag = False
        self._reopen = False

        self._backend = None                  # created inside run() (thread-affine COM/Pulse)
        self._apps: List[AppStream] = []
        self._last_scan = 0.0
        self._last_names: Optional[List[str]] = None
        self._retry_at = 0.0

    # -- control (called from the GUI thread) --
    def set_device(self, hint: Optional[str]) -> None:
        self._hint = hint
        self._retry_at = 0.0
        self._reopen = True

    def stop(self) -> None:
        self._stop_flag = True

    # -- thread body --
    def run(self) -> None:
        self._backend, backend_err = create_app_backend()
        if backend_err:
            log.warning(backend_err)
        try:
            while not self._stop_flag:
                self._reopen = False
                apps = self._scan(force=True)
                target = self._select_target(apps)
                note = ""

                if target is not None:
                    try:
                        self._capture_app(target)
                        continue
                    except AppStreamEnded as exc:
                        log.info("%s: %s", target.name, exc)
                        self.status.emit(f"{target.name}: audio stream ended")
                        self._sleep(0.5)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        log.exception("App capture failed")
                        self._retry_at = time.monotonic() + APP_RETRY_COOLDOWN
                        note = f"app capture failed: {_short(exc)}"
                elif self._hint:
                    note = backend_err or f"'{self._hint}' not playing - system audio"

                try:
                    self._capture_default(note)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Capture failed")
                    self.status.emit(f"Audio error: {_short(exc)} - retrying")
                    self._sleep(2.0)
        finally:
            if self._backend is not None:
                self._backend.close()

    # -- app discovery --
    def _scan(self, force: bool = False) -> List[AppStream]:
        """Refresh the list of apps playing sound (rate-limited) and publish it to the UI."""
        now = time.monotonic()
        if not force and now - self._last_scan < APP_SCAN_INTERVAL:
            return self._apps
        self._last_scan = now
        apps: List[AppStream] = []
        if self._backend is not None:
            try:
                apps = self._backend.list_apps()
            except Exception as exc:  # noqa: BLE001
                log.warning("App scan failed: %s", exc)
        self._apps = apps
        names = sorted({a.name for a in apps}, key=str.lower)
        if names != self._last_names:
            self._last_names = names
            self.devices_found.emit(names)
        return apps

    def _select_target(self, apps: List[AppStream]) -> Optional[AppStream]:
        if not self._hint or time.monotonic() < self._retry_at:
            return None
        return pick_app(apps, self._hint)

    def _keep_going(self, watch_target: bool = False) -> bool:
        """Loop condition shared by all capture paths; also keeps the app list fresh."""
        if self._stop_flag or self._reopen:
            return False
        apps = self._scan()
        if watch_target and self._select_target(apps) is not None:
            return False          # the wanted app just started playing: switch to it
        return True

    # -- capture paths --
    def _capture_app(self, app: AppStream) -> None:
        frames = int(config.SAMPLE_RATE * config.CHUNK_SECONDS)
        assembler = ChunkAssembler(frames)
        segmenter = SpeechSegmenter()

        def emit(mono: np.ndarray) -> None:
            for chunk in assembler.push(np.ascontiguousarray(mono, dtype=np.float32)):
                segment = segmenter.push(chunk)
                if segment is not None:
                    put_drop_oldest(self._out, segment)

        self.device_in_use.emit(app.name)
        self.status.emit(f"Listening: {app.name}")
        self._backend.capture(app, emit, self._keep_going)

    def _capture_default(self, note: str = "") -> None:
        """Fallback: default system loopback (all audio)."""
        device = pick_loopback_device(None)
        if device is None:
            self.status.emit("No loopback device found - right-click to rescan")
            self._sleep(2.0)
            return

        frames = int(config.SAMPLE_RATE * config.CHUNK_SECONDS)
        segmenter = SpeechSegmenter()
        watch = bool(self._hint)      # keep looking for the requested app while on fallback
        self.device_in_use.emit(device.name)
        self.status.emit(f"Listening: {device.name}" + (f" ({note})" if note else ""))

        # channels=None -> all device channels; we downmix to mono ourselves.
        with device.recorder(samplerate=config.SAMPLE_RATE) as rec:
            while self._keep_going(watch):
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
