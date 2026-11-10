"""stt.py - background Whisper transcription + Argos translation thread."""
from __future__ import annotations

import logging
import queue
import re
from typing import Optional

from PyQt5 import QtCore

import config
from translation import Translator

log = logging.getLogger(__name__)

# Whisper-tiny often "hears" these on near-silence; drop them.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thanks for watching!",
    "bye", "obrigado", "obrigada", "subtitles by the amara.org community",
}


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s!]", "", text.lower()).strip()


class TranscriptionThread(QtCore.QThread):
    status = QtCore.pyqtSignal(str)
    result = QtCore.pyqtSignal(str, str, str)  # (detected_lang, original, translated)

    def __init__(self, in_queue: "queue.Queue", translator: Translator) -> None:
        super().__init__()
        self._in = in_queue
        self._translator = translator
        self.source_language: Optional[str] = None  # None = auto-detect
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self.status.emit("Loading Whisper...")
        model = self._load_model()
        if model is None:
            return
        self._translator.warmup([("en", config.DISPLAY_LANG)])
        self.status.emit("Ready")

        while not self._stop_flag:
            try:
                audio = self._in.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._process(model, audio)
            except Exception as exc:  # noqa: BLE001
                log.exception("Transcription failed")
                self.status.emit(f"STT error: {exc}")

    def _load_model(self):
        try:
            from faster_whisper import WhisperModel

            return WhisperModel(
                config.WHISPER_MODEL,
                device="cpu",
                compute_type=config.WHISPER_COMPUTE_TYPE,
                cpu_threads=config.WHISPER_CPU_THREADS,
                download_root=str(config.MODELS_DIR),
                local_files_only=True,   # strictly offline
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not load Whisper model")
            self.status.emit("Whisper model missing - run: python main.py --setup")
            log.error("Model load error: %s", exc)
            return None

    def _process(self, model, audio) -> None:
        segments, info = model.transcribe(
            audio,
            language=self.source_language,
            task="transcribe",
            beam_size=config.WHISPER_BEAM_SIZE,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if len(text) < 2 or _norm(text) in _HALLUCINATIONS:
            return

        lang = info.language
        if lang == config.DISPLAY_LANG:
            translated = text
        else:
            translated = self._translator.translate(text, lang, config.DISPLAY_LANG)
            if translated is None:
                translated = f"(no {lang}->{config.DISPLAY_LANG} language pack installed)"
        self.result.emit(lang, text, translated)
