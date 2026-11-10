#!/usr/bin/env python3
"""main.py - wires UI, audio capture, Whisper, Argos and keyboard output together.

First run (needs internet ONCE):   python main.py --setup
Normal run (fully offline):        python main.py
Pick an output device by name:     python main.py --device "Headphones"
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import queue
import argostranslate.translate
import sys

import config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Offline live translation overlay")
    p.add_argument("--setup", action="store_true", help="download Whisper + Argos models (internet needed once)")
    p.add_argument("--langs", nargs="*", help="source languages for --setup (default from config.py)")
    p.add_argument("--list-devices", action="store_true", help="print loopback devices and exit")
    p.add_argument("--device", help="substring of the loopback device name to capture")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def run_setup(langs) -> None:
    from faster_whisper import WhisperModel
    from translation import install_language_packs

    config.MODELS_DIR.mkdir(exist_ok=True)
    print(f"Downloading Whisper '{config.WHISPER_MODEL}' to {config.MODELS_DIR} ...")
    WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type=config.WHISPER_COMPUTE_TYPE,
                 download_root=str(config.MODELS_DIR))
    langs = langs or config.SETUP_SOURCE_LANGS
    pairs = [(s, "en") for s in langs if s != "en"]
    pairs += [("en", config.DISPLAY_LANG), (config.CHAT_INPUT_LANG, config.CHAT_OUTPUT_LANG)]
    install_language_packs(pairs)
    print("Setup complete. You can now run offline.")


def list_devices() -> None:
    from audio import list_loopback_devices, pick_loopback_device

    devices = list_loopback_devices()
    default = pick_loopback_device()
    for d in devices:
        print(("* " if default is not None and d.id == default.id else "  ") + d.name)
    if not devices:
        print("No loopback devices found.")


def run_app(args: argparse.Namespace) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never touch the network at runtime

    # pynput + always-on-top overlays need X11. Under Wayland, run Qt through XWayland.
    if platform.system() == "Linux" and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
        logging.warning("Wayland session detected: keystroke injection into other apps will "
                        "NOT work. Choose an 'Xorg/X11' session at the login screen.")

    from PyQt5 import QtCore, QtWidgets

    from audio import AudioCaptureThread
    from output import FocusTracker, OutgoingWorker
    from overlay import OverlayWindow
    from stt import TranscriptionThread
    from translation import Translator

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv)

    translator = Translator()
    segments: "queue.Queue" = queue.Queue(maxsize=config.MAX_QUEUE_SEGMENTS)

    capture = AudioCaptureThread(segments, args.device)
    stt = TranscriptionThread(segments, translator)
    focus = FocusTracker()
    outgoing = OutgoingWorker(translator, focus)
    win = OverlayWindow()

    # --- signal wiring (all cross-thread traffic goes through Qt signals) ---
    capture.status.connect(win.set_status)
    capture.devices_found.connect(win.set_devices)
    stt.status.connect(win.set_status)
    stt.result.connect(win.add_incoming)
    outgoing.status.connect(win.set_status)
    outgoing.translated.connect(win.add_outgoing)
    win.text_submitted.connect(outgoing.submit)
    win.device_chosen.connect(capture.set_device)
    win.source_language_changed.connect(lambda code: setattr(stt, "source_language", code))

    win.show()
    focus.own_ids.add(int(win.winId()))
    focus.start()
    for t in (stt, outgoing, capture):
        t.start()

    def shutdown() -> None:
        focus.stop()
        for t in (capture, stt, outgoing):
            t.stop()
        for t in (capture, stt, outgoing):
            t.wait(3000)

    app.aboutToQuit.connect(shutdown)
    return app.exec_()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.setup:
        run_setup(args.langs)
        return 0
    if args.list_devices:
        list_devices()
        return 0
    return run_app(args)


if __name__ == "__main__":
    sys.exit(main())
