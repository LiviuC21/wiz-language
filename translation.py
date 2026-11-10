"""translation.py - thin, thread-safe wrapper around argos-translate."""
from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, Optional, Tuple

log = logging.getLogger(__name__)


class Translator:
    """Offline translator. Safe to share between threads (calls are serialised)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], object] = {}

    def _get_translation(self, src: str, dst: str):
        key = (src, dst)
        if key in self._cache:
            return self._cache[key]
        import argostranslate.translate as at

        langs = at.get_installed_languages()
        from_l = next((l for l in langs if l.code == src), None)
        to_l = next((l for l in langs if l.code == dst), None)
        tr = from_l.get_translation(to_l) if (from_l and to_l) else None
        if tr is not None:          # never cache misses: a pack may be installed later
            self._cache[key] = tr
        return tr

    def is_available(self, src: str, dst: str) -> bool:
        with self._lock:
            return src == dst or self._get_translation(src, dst) is not None

    def translate(self, text: str, src: str, dst: str) -> Optional[str]:
        """Return the translation, or None if no language pack covers src->dst.
        Argos pivots through English automatically (e.g. pt->en->ro)."""
        if src == dst:
            return text
        with self._lock:
            tr = self._get_translation(src, dst)
            if tr is None:
                return None
            return tr.translate(text)

    def warmup(self, pairs: Iterable[Tuple[str, str]]) -> None:
        """Pre-load models so the first real sentence isn't slow."""
        for src, dst in pairs:
            try:
                self.translate("ok", src, dst)
            except Exception:  # noqa: BLE001
                log.exception("warmup failed for %s->%s", src, dst)


# ------------------------------------------------------------------ one-time setup
def _direct_pack_installed(src: str, dst: str) -> bool:
    import argostranslate.translate as at

    try:
        for lang in at.get_installed_languages():
            if lang.code == src:
                return any(t.to_lang.code == dst for t in lang.translations_from)
    except Exception:  # noqa: BLE001
        pass
    return False


def install_language_packs(pairs: Iterable[Tuple[str, str]]) -> None:
    """Download + install argos language packs (needs internet, run once)."""
    import argostranslate.package as pkg

    print("Updating Argos package index...")
    pkg.update_package_index()
    available = pkg.get_available_packages()
    for src, dst in pairs:
        if _direct_pack_installed(src, dst):
            print(f"  {src}->{dst}: already installed")
            continue
        match = next((p for p in available if p.from_code == src and p.to_code == dst), None)
        if match is None:
            print(f"  {src}->{dst}: no such package in the index (will pivot via English if possible)")
            continue
        print(f"  {src}->{dst}: downloading...")
        pkg.install_from_path(match.download())
        print(f"  {src}->{dst}: installed")
