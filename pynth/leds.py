"""Onboard-LED des Pi als Anzeige "pynth startet / spielbereit / Fehler".

  Start:        schnelles Blinken (setzt schon der Dienst vor dem Start, siehe pynth.service)
  Spielbereit:  Herzschlag (zweimal kurz, Pause), sonst dauerhaft an
  Fehler:       langsames Blinken (z.B. keine Soundkarte)
  Beenden:      zurueck auf den Standard (SD-Karten-Zugriff); nach dem Herunterfahren aus

Die Dateien unter /sys/class/leds gehoeren root. Der Dienst gibt sie vor dem Start der
Gruppe audio frei (ExecStartPre in deploy/pynth.service). Fehlen LED oder Rechte (z.B.
am Laptop), passiert nichts ausser einem Log-Hinweis: pynth laeuft normal weiter.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("pynth.leds")

BASE = Path("/sys/class/leds")
CANDIDATES = ("ACT", "led0")            # gruene Aktivitaets-LED, je nach Modell/Kernel
DEFAULT_TRIGGERS = ("mmc0", "actpwr", "default-on")


class StatusLed:
    def __init__(self, name: str | None = None):
        """name: LED-Name unter /sys/class/leds, None = automatisch, "off" = keine LED."""
        self.dir = None
        self.triggers: list[str] = []
        env = os.environ.get("PYNTH_LED")
        name = name or env
        if name == "off":
            return
        for cand in ([name] if name else CANDIDATES):
            d = BASE / cand
            if (d / "trigger").exists():
                self.dir = d
                break
        if self.dir is None:
            log.debug("Keine Status-LED gefunden")
            return
        try:
            self.triggers = (self.dir / "trigger").read_text().replace("[", "").replace("]", "").split()
        except OSError:
            self.dir = None

    def _write(self, attr: str, value) -> bool:
        try:
            (self.dir / attr).write_text(f"{value}\n")
            return True
        except OSError as e:
            log.info("Status-LED %s: %s nicht schreibbar (%s). Dienst-Datei neu kopieren, siehe README 'Status-LED'",
                     self.dir.name, attr, e.strerror or e)
            self.dir = None                      # nicht bei jedem Zustand wieder meckern
            return False

    def _trigger(self, name: str) -> bool:
        return self.dir is not None and name in self.triggers and self._write("trigger", name)

    def _blink(self, on_ms: int, off_ms: int):
        if self._trigger("timer"):
            # delay_* legt der Kernel beim Umschalten neu an (gehoeren dann root). Klappt das
            # Schreiben nicht, blinkt die LED mit dem Standard 500/500 ms weiter.
            try:
                (self.dir / "delay_on").write_text(f"{on_ms}\n")
                (self.dir / "delay_off").write_text(f"{off_ms}\n")
            except OSError:
                pass

    def starting(self):
        self._blink(100, 100)

    def ready(self):
        if self.dir is None:
            return
        if not self._trigger("heartbeat") and self._trigger("none"):
            self._write("brightness", 1)
        if self.dir is not None:
            log.info("Status-LED %s: spielbereit", self.dir.name)

    def error(self):
        self._blink(1000, 1000)

    def restore(self):
        for t in DEFAULT_TRIGGERS:
            if self._trigger(t):
                return
