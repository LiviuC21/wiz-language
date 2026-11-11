"""config.py - every tunable value lives here."""
from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
MODELS_DIR = APP_DIR / "models"  # faster-whisper models are cached here

# ---------------------------------------------------------------- Speech-to-text
WHISPER_MODEL = "base"          # "tiny" = fastest / smallest; "base" = better accuracy
WHISPER_COMPUTE_TYPE = "int8"   # best CPU speed/size trade-off
WHISPER_CPU_THREADS = 2         # keep low so the game keeps its CPU
WHISPER_BEAM_SIZE = 1           # greedy decoding = lowest latency

# ---------------------------------------------------------------- Audio capture
SAMPLE_RATE = 16_000            # Whisper's native rate (the OS resamples for us)
CHUNK_SECONDS = 0.1             # size of each blocking read from the loopback device
VAD_RMS_THRESHOLD = 0.008       # energy gate; raise it if game music keeps triggering
PRE_ROLL_CHUNKS = 3             # chunks kept before speech starts (avoids clipped words)
MIN_SPEECH_SECONDS = 0.4        # ignore blips shorter than this
SILENCE_END_SECONDS = 0.6       # this much silence ends a phrase
MAX_SEGMENT_SECONDS = 6.0       # force a flush so latency stays bounded
MAX_QUEUE_SEGMENTS = 4          # if Whisper falls behind, oldest audio is dropped

# ---------------------------------------------------------------- Translation
DISPLAY_LANG = "ro"             # incoming speech is translated INTO this language
CHAT_INPUT_LANG = "ro"          # language you type in the overlay
CHAT_OUTPUT_LANG = "en"         # language that is typed into the game
SOURCE_LANGUAGES = {            # meniul de setari (eticheta -> cod Whisper)
    "Auto-detect": None,
    "English": "en",
    "French": "fr",
    "Spanish": "es",
    "Portuguese": "pt",
    "German": "de",
    "Russian": "ru",
}
SETUP_SOURCE_LANGS = ["en", "pt"]   # language packs fetched by `--setup`

# ---------------------------------------------------------------- Typing into the game
CHAT_OPEN_KEY = None            # e.g. "t" or "enter" if your game needs a key to open chat
PRESS_ENTER_AFTER = True        # press Enter after typing to send the message
TYPE_DELAY = 0.004              # seconds between characters (raise if game drops keys)
REFOCUS_DELAY = 0.25            # wait after re-activating the game window

# ---------------------------------------------------------------- Overlay UI
FONT_SIZE_DEFAULT = 11          # pt, dimensiunea initiala a textului
FONT_SIZE_MIN = 8
FONT_SIZE_MAX = 28
HISTORY_MAX_BLOCKS = 500

# ---------------------------------------------------------------- Macro Chat Jocuri
CHAT_OPEN_KEY = 'y'         # Tasta universală pentru deschis chat-ul (pune 't' dacă joci mai mult Minecraft)
PRESS_ENTER_AFTER = True    # Apasă automat Enter pentru a trimite mesajul pe server
REFOCUS_DELAY = 0.15        # Timpul de așteptare după mutarea ferestrei, înainte să apese tasta de chat
TYPE_DELAY = 0.005          # Pauza invizibilă între litere (previne blocarea din cauza anti-spam-ului)
