"""MIDI-Eingang (USB, BLE, alles was ALSA/rtmidi sieht) mit CC-Mapping + MIDI-Learn.

- Neue Geraete werden alle paar Sekunden automatisch erkannt (Hotplug).
- Mapping liegt in midi_map.json: {"<kanal>:<cc>": "<param-id>"}, Kanal 1-16.
- MIDI-Learn: In der Web-UI "L" am Regler druecken, dann Knopf am Controller drehen.
- CC 64 = Sustain-Pedal, CC 123 = All Notes Off sind fest belegt.
- Knob-Baenke, Pads und Mod-Rad: siehe controller.py (Vorrang vor midi_map.json).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .params import RENAMED

log = logging.getLogger("pynth.midi")

IGNORE_PORTS = ("Midi Through", "RtMidi")


class MidiManager:
    def __init__(self, engine, params, map_file: Path, controller=None):
        self.engine = engine
        self.controller = controller
        self.params = params
        self.map_file = Path(map_file)
        self.mapping: dict[str, str] = {}
        self.learn_target: str | None = None
        self.ports: dict[str, object] = {}
        self.last_message = ""
        self._last_shown = 0.0
        self._stop = threading.Event()
        self._load_map()
        if controller is not None:
            controller.on_learned = self._forget_spec

    def _forget_spec(self, spec: str):
        """Eingang wurde als Controller-Knob/Pad gelernt -> alte Direkt-Belegung (L) loeschen,
        sonst steuert ein Knopf je nach Weg mal das eine, mal das andere."""
        kind, _, key = spec.partition(":")
        if kind == "cc" and key in self.mapping:
            log.info("MIDI-Map: %s -> %s entfernt (jetzt Controller-Knob)", key, self.mapping[key])
            del self.mapping[key]
            self._save_map()
            self._announce()

    # ----------------------------- Mapping -----------------------------
    def _load_map(self):
        try:
            data = json.loads(self.map_file.read_text())
            data = {k: RENAMED.get(v, v) for k, v in data.items()}   # alte IDs uebernehmen
            self.mapping = {k: v for k, v in data.items() if v in self.params.params}
            if self.mapping != json.loads(self.map_file.read_text()):
                self._save_map()
        except (OSError, ValueError):
            self.mapping = {}

    def _save_map(self):
        self.map_file.write_text(json.dumps(self.mapping, indent=2, sort_keys=True) + "\n")

    def learn(self, pid: str | None):
        self.learn_target = pid if pid in self.params.params else None
        self._announce()

    def unmap(self, pid: str):
        self.mapping = {k: v for k, v in self.mapping.items() if v != pid}
        self._save_map()
        self._announce()

    def info(self) -> dict:
        return {"ports": list(self.ports), "map": self.mapping,
                "learn": self.learn_target, "last": self.last_message}

    def _announce(self):
        self.params.broadcast("midi", self.info())

    def _show(self, text: str):
        """Letzte MIDI-Nachricht in der Web-UI anzeigen (max. ~10x pro Sekunde)."""
        self.last_message = text
        now = time.monotonic()
        if now - self._last_shown > 0.1:
            self._last_shown = now
            self.params.broadcast("midilast", text)

    # ----------------------------- Eingang -----------------------------
    def _on_message(self, msg):
        try:
            t = msg.type
            ctl = self.controller
            if t in ("note_on", "note_off"):
                ch = msg.channel + 1
                on = t == "note_on" and msg.velocity > 0
                if on:
                    self._show(f"Note {msg.note} (Kanal {ch}) Velocity {msg.velocity}")
                if ctl and on and ctl.handle_note(ch, msg.note, msg.velocity):
                    return
                if ctl and not on and ctl.is_pad_note(ch, msg.note):
                    return
                if on:
                    self.engine.note_on(msg.note, msg.velocity)
                else:
                    self.engine.note_off(msg.note)
            elif t == "program_change":
                ch = msg.channel + 1
                self._show(f"Program Change {msg.program} (Kanal {ch})")
                if ctl:
                    ctl.handle_program(ch, msg.program)
            elif t == "pitchwheel":
                self.engine.pitch_bend(msg.pitch / 8192.0)
            elif t == "control_change":
                key = f"{msg.channel + 1}:{msg.control}"
                self._show(f"CC {msg.control} (Kanal {msg.channel + 1}) = {msg.value}")
                if not self.learn_target and ctl and ctl.handle_cc(msg.channel + 1, msg.control, msg.value):
                    return
                if self.learn_target:
                    self.mapping = {k: v for k, v in self.mapping.items() if v != self.learn_target}
                    self.mapping[key] = self.learn_target
                    if ctl:
                        ctl.forget_input(f"cc:{key}")      # Knopf ist jetzt direkt belegt
                    log.info("MIDI-Learn: %s -> %s", key, self.learn_target)
                    self.learn_target = None
                    self._save_map()
                    self._announce()
                if key in self.mapping:
                    self.params.set_norm(self.mapping[key], msg.value / 127.0, source="midi")
                elif msg.control == 64:
                    self.engine.sustain_pedal(msg.value >= 64)
                elif msg.control == 123:
                    self.engine.panic()
        except Exception:                       # ein kaputtes Paket darf nichts abschiessen
            log.exception("MIDI-Fehler bei %s", msg)

    def _scan(self, mido):
        try:
            names = set(mido.get_input_names())
        except Exception as e:
            log.warning("MIDI-Scan fehlgeschlagen: %s", e)
            return
        wanted = {n for n in names if not any(n.startswith(i) for i in IGNORE_PORTS)}
        changed = False
        for name in list(self.ports):
            if name not in wanted:
                try:
                    self.ports.pop(name).close()
                except Exception:
                    pass
                log.info("MIDI getrennt: %s", name)
                changed = True
        for name in wanted - set(self.ports):
            try:
                self.ports[name] = mido.open_input(name, callback=self._on_message)
                log.info("MIDI verbunden: %s", name)
                changed = True
            except Exception as e:
                log.warning("Kann %s nicht oeffnen: %s", name, e)
        if changed:
            self._announce()

    def start(self, interval: float = 3.0):
        try:
            import mido
        except ImportError:
            log.warning("mido nicht installiert - kein MIDI")
            return

        def loop():
            while not self._stop.is_set():
                self._scan(mido)
                self._stop.wait(interval)

        threading.Thread(target=loop, name="midi-scan", daemon=True).start()

    def stop(self):
        self._stop.set()
        for p in self.ports.values():
            try:
                p.close()
            except Exception:
                pass
