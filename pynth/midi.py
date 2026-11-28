"""MIDI-Eingang (USB, BLE, alles was ALSA/rtmidi sieht) mit CC-Mapping + MIDI-Learn.

- Neue Geraete werden alle paar Sekunden automatisch erkannt (Hotplug).
- Mapping liegt in midi_map.json: {"<kanal>:<cc>": "<param-id>"}, Kanal 1-16.
- MIDI-Learn: In der Web-UI "L" am Regler druecken, dann Knopf am Controller drehen.
- CC 64 = Sustain-Pedal, CC 123 = All Notes Off sind fest belegt.
- Knob-Baenke, Pads und Mod-Rad: siehe controller.py (Vorrang vor midi_map.json).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

from .params import RENAMED_CONTROLS as RENAMED
from .storage import load_json, save_json

log = logging.getLogger("pynth.midi")

IGNORE_PORTS = ("Midi Through", "RtMidi")
SKIP_TYPES = ("clock", "active_sensing")
CONTINUOUS = ("control_change", "pitchwheel", "aftertouch", "polytouch")   # im Monitor gedrosselt
# MIDI Machine Control (Sysex F0 7F <id> 06 <cmd> F7), senden viele Transport-Tasten ab Werk.
# Vor (Fast Forward) / Zurueck (Rewind) = Looper-Takt dazu / weg.
MMC = {1: "stop", 2: "play", 3: "play", 4: "bar_add", 5: "bar_remove", 6: "record", 9: "stop"}
MMC_TEXT = {"bar_add": "Takt +", "bar_remove": "Takt −"}


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
        self._hold_until = 0.0
        self.log: deque[str] = deque(maxlen=40)      # MIDI-Monitor (rohe Nachrichten)
        self._last_raw: dict[str, float] = {}
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
        data = load_json(self.map_file, {})
        if not isinstance(data, dict):
            data = {}
        raw = dict(data)
        data = {k: RENAMED.get(v, v) for k, v in data.items()}   # alte IDs uebernehmen
        self.mapping = {k: v for k, v in data.items() if v in self.params.params}
        if raw and self.mapping != raw:
            self._save_map()

    def _save_map(self):
        try:
            save_json(self.map_file, self.mapping, sort_keys=True, history=True)
        except OSError as e:
            log.warning("Kann %s nicht schreiben: %s", self.map_file, e)

    def learn(self, pid: str | None):
        self.learn_target = pid if pid in self.params.params else None
        self._announce()

    def unmap(self, pid: str):
        log.info("Zuordnung von %s entfernt", pid)
        self.mapping = {k: v for k, v in self.mapping.items() if v != pid}
        self._save_map()
        if self.controller:
            self.controller.unassign_param(pid)
        self._announce()

    def info(self) -> dict:
        return {"ports": list(self.ports), "map": self.mapping,
                "learn": self.learn_target, "last": self.last_message, "log": list(self.log)}

    def _raw(self, msg, port: str):
        """Jede Nachricht roh in den MIDI-Monitor (Web-UI), Dauer-Nachrichten gedrosselt."""
        now = time.monotonic()
        if msg.type in CONTINUOUS:
            if now - self._last_raw.get(msg.type, 0.0) < 0.05:
                return
            self._last_raw[msg.type] = now
        d = msg.dict()
        d.pop("time", None)
        typ, ch = d.pop("type"), d.pop("channel", None)
        rest = " ".join(f"{k}={v}" for k, v in d.items())
        line = (f"{time.strftime('%H:%M:%S')}  {port[:20]}  "
                + (f"Kanal {ch + 1:2d}  " if ch is not None else "") + f"{typ} {rest}")
        self.log.append(line)
        self.params.broadcast("midiraw", line)

    def _announce(self):
        self.params.broadcast("midi", self.info())

    def _show(self, text: str, hold: float = 0.0):
        """Letzte MIDI-Nachricht in der Web-UI anzeigen (max. ~10x pro Sekunde).
        hold > 0: wichtiger Hinweis, sofort zeigen und so viele Sekunden stehen lassen."""
        now = time.monotonic()
        if hold:
            self._hold_until = now + hold
        elif now < self._hold_until:
            return
        self.last_message = text
        if hold or now - self._last_shown > 0.1:
            self._last_shown = now
            self.params.broadcast("midilast", text)

    # ----------------------------- Eingang -----------------------------
    def _on_message(self, msg, port: str = ""):
        try:
            t = msg.type
            if t in SKIP_TYPES:
                return
            self._raw(msg, port)
            ctl = self.controller
            if t in ("polytouch", "aftertouch"):   # Pad-Druck-Nachdruck: nur im Monitor zeigen
                return
            if t in ("note_on", "note_off"):
                ch = msg.channel + 1
                on = t == "note_on" and msg.velocity > 0
                if on:
                    self._show(f"Note {msg.note} (Kanal {ch}) Velocity {msg.velocity}")
                if ctl and on and ctl.handle_note(ch, msg.note, msg.velocity):
                    return
                if ctl and not on and ctl.handle_note_off(ch, msg.note):
                    return
                if on:
                    self.engine.note_on(msg.note, msg.velocity)
                else:
                    self.engine.note_off(msg.note)
            elif t == "sysex":
                d = msg.data
                if ctl and len(d) >= 4 and d[0] == 0x7F and d[2] == 0x06 and d[3] in MMC:
                    self._show(f"MMC {MMC_TEXT.get(MMC[d[3]], MMC[d[3]].capitalize())} -> Looper")
                    ctl.transport(MMC[d[3]])
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
                    pid = self.learn_target
                    knob = ctl.knob_index(f"cc:{key}") if ctl else None
                    self.mapping = {k: v for k, v in self.mapping.items() if v != pid}
                    if knob is not None:
                        # Bank-Knob gedreht -> Regler in die aktive Bank legen (nicht global)
                        self.mapping.pop(key, None)
                        ctl.assign(ctl.bank, knob, pid)
                        log.info("MIDI-Learn: Bank %d Knob %d -> %s", ctl.bank + 1, knob + 1, pid)
                        self._show(f"Gelernt: {self.params.params[pid].label} -> Knob {knob + 1} "
                                   f"in Bank {ctl.bank + 1}", hold=3)
                    else:
                        self.mapping[key] = pid
                        if ctl:
                            ctl.forget_input(f"cc:{key}")      # Knopf ist jetzt direkt belegt
                        log.info("MIDI-Learn: %s -> %s", key, pid)
                        if ctl:                         # deutlich sagen, dass das KEIN Bank-Knob ist
                            self._show(f"CC {msg.control} ist kein Bank-Knob -> "
                                       f"{self.params.params[pid].label} gilt in ALLEN Bänken. "
                                       "Falls das ein Bank-Knob sein soll: Knobs unter "
                                       "'Controller einrichten' neu anlernen.", hold=6)
                    self.learn_target = None
                    self._save_map()
                    self._announce()
                    if knob is not None:
                        return
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
                self.ports[name] = mido.open_input(
                    name, callback=lambda m, n=name: self._on_message(m, n))
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
