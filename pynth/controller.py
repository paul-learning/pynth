"""Knob-Baenke fuer einen Controller mit 8 Knobs + 4 Pads (z.B. Samson Graphite M25).

- Die 4 Pads schalten die Bank um, jede Bank belegt die 8 Knobs neu -> 32 Regler.
- Knobs sind absolute Potis (0-127). Standard "jump": Knob setzt sofort seinen Wert.
  Optional "pickup" (Soft-Takeover): nach Bank-/Preset-Wechsel greift ein Knob erst,
  wenn er den aktuellen Wert "abholt" (darueber dreht oder ihn trifft). So springt nichts.
- Mod-Rad (CC 1) steuert standardmaessig die LFO-Tiefe, direkt ohne Abholen.

Welche CCs/Noten der Controller sendet, wird per Learn in der Web-UI eingerichtet
("Controller einrichten": Knob 1..8 drehen, Pad 1..4 druecken). Alles landet in
controller.json (Nutzerdaten, nicht im Repo). Fehlt die Datei, gelten die Defaults unten.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("pynth.controller")

N_KNOBS = 8
N_PADS = 4
PICKUP_TOLERANCE = 3 / 127      # so nah am Wert gilt ein Knob als "abgeholt"
LEARN_MIN_MOVE = 4              # Knob-Learn: erst nach so vielen Schritten Bewegung (Potis zittern um 1-2)
TAKEOVER_MODES = ("jump", "pickup")
RELEASE_WINDOW = 0.8            # Weiter-Taste: Loslassen so kurz nach dem Druecken = kein neuer Druck
PC_CC_GAP = 0.3                 # CC 0 direkt nach Program Change ist Bank-Select des Presets, kein Pad

DEFAULT_CONFIG = {
    # Eingaenge: "cc:<kanal>:<nummer>", "note:<kanal>:<nummer>" oder "pc:<kanal>:<nummer>"
    # (Program Change), Kanal 1-16, None = nicht eingerichtet
    "knobs": [None] * N_KNOBS,
    "pads": [None] * N_PADS,
    "modwheel": {"input": "cc:*:1", "param": "lfo.depth"},     # * = jeder Kanal
    # Weiter-Taste: jeder Druck schaltet zur naechsten Bank (1 -> 2 -> 3 -> 4 -> 1).
    # Fuer Controller, deren Pads alle dasselbe senden (Graphite M25: Note 0 bzw. CC 0).
    "next_bank": ["note:*:0", "cc:*:0"],
    # "jump": Knob setzt sofort seinen Wert (voller Weg, kann nach Bankwechsel springen)
    # "pickup": Soft-Takeover, Knob greift erst, wenn er den aktuellen Wert ueberstreicht
    "takeover": "jump",
    # Jede Bank startet leer und wird selbst belegt: Pad waehlen, "L" am Regler, Knob drehen.
    "banks": [{"name": "", "params": [None] * N_KNOBS} for _ in range(N_PADS)],
}

# Fruehere Vorbelegung (bis PR 2). Steht sie unveraendert in controller.json, wird sie geleert.
OLD_DEFAULT_BANKS = [
    ["osc1.wave", "osc1.octave", "osc2.wave", "osc2.octave", "osc2.detune", "mix.osc1", "mix.osc2", "mix.noise"],
    ["filter.cutoff", "filter.resonance", None, None, "env.attack", "env.decay", "env.sustain", "env.release"],
    ["lfo.rate", "lfo.depth", "lfo.wave", "lfo.target", "noise.type", "mix.noise", None, None],
    ["fx.delay_mix", "fx.delay_time", "fx.delay_feedback", None, "fx.reverb_mix", "fx.reverb_size", None, "master.volume"],
]
OLD_DEFAULT_NAMES = ["Osz + Mix", "Filter + Hüllkurve", "LFO + Noise", "Effekte + Master"]


def _matches(spec: str | None, kind: str, channel: int, number: int) -> bool:
    if not spec:
        return False
    try:
        k, ch, num = spec.split(":")
    except ValueError:
        return False
    return k == kind and (ch == "*" or int(ch) == channel) and int(num) == number


class Controller:
    def __init__(self, params, config_file: Path):
        self.params = params
        self.file = Path(config_file)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.bank = 0
        self.learn_slot: tuple[str, int] | None = None     # ("knob", 3) / ("pad", 0)
        self.wizard = False                                  # nach jedem Learn automatisch weiter
        self._picked = [False] * N_KNOBS
        self._last_x: list[float | None] = [None] * N_KNOBS   # letzte Knob-Stellung 0..1
        self._last_set: list[object] = [None] * N_KNOBS       # Wert, den der Knob zuletzt gesetzt hat
        self._learn_seen: dict[str, int] = {}                # Learn: erster gesehener CC-Wert je Eingang
        self._wizard_done: set[str] = set()                  # in diesem Einrichten-Durchlauf schon gelernt
        self._nb_state: dict[str, dict] = {}                 # Weiter-Taste: letzter Zustand je Eingang
        self._last_pc = 0.0
        self.on_learned = None                               # Callback(spec): z.B. alte Direkt-Belegung loeschen
        self._load()

    # ----------------------------- Datei -----------------------------
    def _load(self):
        try:
            data = json.loads(self.file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("%s nicht lesbar (%s), nutze Defaults", self.file, e)
            return
        cfg = self.config
        for key, n in (("knobs", N_KNOBS), ("pads", N_PADS)):
            vals = list(data.get(key) or [])[:n]
            cfg[key] = vals + [None] * (n - len(vals))
        nb = data.get("next_bank")
        if isinstance(nb, list):
            cfg["next_bank"] = [x for x in nb if isinstance(x, str)]
        if data.get("takeover") in TAKEOVER_MODES:
            cfg["takeover"] = data["takeover"]
        if isinstance(data.get("modwheel"), dict):
            cfg["modwheel"].update(data["modwheel"])
        banks = data.get("banks")
        migrated = False
        if isinstance(banks, list):
            for i, b in enumerate(banks[:N_PADS]):
                if not isinstance(b, dict):
                    continue
                ps = list(b.get("params") or [])[:N_KNOBS]
                ps = [p if p in self.params.params else None for p in ps]
                ps += [None] * (N_KNOBS - len(ps))
                name = str(b.get("name") or "")
                if ps == OLD_DEFAULT_BANKS[i]:          # alte Vorbelegung -> leere Bank
                    ps, migrated = [None] * N_KNOBS, True
                if name == OLD_DEFAULT_NAMES[i]:
                    name, migrated = "", True
                cfg["banks"][i] = {"name": name, "params": ps}
        if migrated:
            log.info("Alte Bank-Vorbelegung entfernt, Baenke sind jetzt leer")
            self._save()

    def _save(self):
        try:
            self.file.write_text(json.dumps(self.config, indent=2, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("Kann %s nicht schreiben: %s", self.file, e)

    # ----------------------------- Status -----------------------------
    def info(self) -> dict:
        return {"bank": self.bank, "banks": self.config["banks"],
                "knobs": self.config["knobs"], "pads": self.config["pads"],
                "modwheel": self.config["modwheel"], "picked": list(self._picked),
                "next_bank": self.config["next_bank"],
                "takeover": self.config["takeover"],
                "learn": list(self.learn_slot) if self.learn_slot else None,
                "wizard": self.wizard}

    def _announce(self):
        self.params.broadcast("controller", self.info())

    # ----------------------------- Aktionen (Web) -----------------------------
    def set_bank(self, i: int):
        if 0 <= i < N_PADS and i != self.bank:
            self.bank = i
            self._picked = [False] * N_KNOBS
            self._announce()

    def learn(self, slot: str | None, index: int = 0, wizard: bool = False):
        if slot == "next":
            self.learn_slot, self.wizard = ("next", 0), False
        elif slot in ("knob", "pad") and 0 <= index < (N_KNOBS if slot == "knob" else N_PADS):
            self.learn_slot = (slot, index)
            self.wizard = wizard
        else:
            self.learn_slot, self.wizard = None, False
        self._learn_seen = {}
        self._wizard_done = set()
        self._announce()

    def set_takeover(self, mode: str):
        if mode in TAKEOVER_MODES:
            self.config["takeover"] = mode
            self._picked = [False] * N_KNOBS
            self._save()
            self._announce()

    def uses_input(self, spec: str) -> bool:
        return spec in self.config["knobs"] or spec in self.config["pads"]

    def forget_input(self, spec: str):
        """Eingang wurde anderswo (MIDI-Learn am Regler) belegt -> hier freigeben."""
        changed = False
        for key in ("knobs", "pads"):
            lst = self.config[key]
            for j, s in enumerate(lst):
                if s == spec:
                    lst[j], changed = None, True
        if changed:
            self._save()
            self._announce()

    def clear(self, slot: str, index: int):
        if slot == "next":
            self.config["next_bank"] = []
            self._save()
            self._announce()
            return
        key = "knobs" if slot == "knob" else "pads"
        if 0 <= index < len(self.config[key]):
            self.config[key][index] = None
            self._save()
            self._announce()

    def assign(self, bank: int, knob: int, pid: str | None):
        if 0 <= bank < N_PADS and 0 <= knob < N_KNOBS and (pid is None or pid in self.params.params):
            self.config["banks"][bank]["params"][knob] = pid
            if bank == self.bank:
                self._picked[knob] = False
            self._save()
            self._announce()

    def knob_index(self, spec: str) -> int | None:
        try:
            return self.config["knobs"].index(spec)
        except ValueError:
            return None

    def unassign_param(self, pid: str):
        """Regler aus der aktiven Bank nehmen (Rechtsklick auf "L")."""
        ps = self.config["banks"][self.bank]["params"]
        if pid in ps:
            for k, x in enumerate(ps):
                if x == pid:
                    ps[k] = None
            self._save()
            self._announce()

    def set_modwheel(self, pid: str | None):
        if pid is None or pid in self.params.params:
            self.config["modwheel"]["param"] = pid
            self._save()
            self._announce()

    # ----------------------------- MIDI -----------------------------
    def _learn_input(self, spec: str):
        slot, i = self.learn_slot
        if self.wizard and spec in self._wizard_done:
            return      # z.B. Pad loest doppelt aus oder Knob 1 wird weitergedreht: nicht ins naechste Feld
        self._wizard_done.add(spec)
        for key in ("knobs", "pads"):        # gleicher Eingang darf nur einmal belegt sein
            lst = self.config[key]
            for j, s in enumerate(lst):
                if s == spec:
                    lst[j] = None
        if slot == "next":
            self.config["next_bank"] = [spec]
        else:
            self.config["next_bank"] = [x for x in self.config["next_bank"] if x != spec]
            self.config["knobs" if slot == "knob" else "pads"][i] = spec
        self._learn_seen = {}
        if self.on_learned:
            self.on_learned(spec)
        log.info("Controller-Learn: %s %d <- %s", slot, i + 1, spec)
        if self.wizard:                      # Knob 1..8, dann Pad 1..4
            if slot == "knob" and i + 1 < N_KNOBS:
                self.learn_slot = ("knob", i + 1)
            elif slot == "knob":
                self.learn_slot = ("pad", 0)
            elif i + 1 < N_PADS:
                self.learn_slot = ("pad", i + 1)
            else:
                self.learn_slot, self.wizard = None, False
        else:
            self.learn_slot = None
        self._save()
        self._announce()

    # ----------------------------- Weiter-Taste -----------------------------
    def next_bank(self):
        self.set_bank((self.bank + 1) % N_PADS)

    def _next_input(self, kind: str, channel: int, number: int, on: bool) -> bool:
        """Weiter-Taste. True = Eingang gehoert dazu (verbraucht, nicht spielen).

        Jeder physische Druck zaehlt genau einmal, egal ob das Pad "momentary" (an beim
        Druecken, aus beim Loslassen) oder "toggle" (abwechselnd an/aus pro Druck) sendet:
        - "an" zaehlt immer.
        - "aus" kurz nach "an" ist nur das Loslassen.
        - Kommen zweimal hintereinander "an" oder "aus", sendet das Pad im Toggle-Modus
          (mehrere Pads mit derselben Note) -> ab dann zaehlt jede Nachricht.
        """
        spec = next((s for s in self.config["next_bank"] if _matches(s, kind, channel, number)), None)
        if spec is None:
            return False
        now = time.monotonic()
        if kind == "cc" and now - self._last_pc < PC_CC_GAP:
            return True                          # Bank-Select nach Preset-Wechsel am Controller
        st = self._nb_state.setdefault(spec, {"on": None, "t": 0.0, "toggle": False})
        prev_on, prev_t = st["on"], st["t"]
        st["on"], st["t"] = on, now
        if prev_on is not None and prev_on == on:
            st["toggle"] = True
        if on or st["toggle"] or not (prev_on is True and now - prev_t < RELEASE_WINDOW):
            self.next_bank()
        return True

    def handle_note_off(self, channel: int, note: int) -> bool:
        """Note-Off. True = gehoert zum Controller, nicht an die Engine."""
        if self.learn_slot and self.learn_slot[0] in ("pad", "next"):
            return self.is_pad_note(channel, note)
        if any(_matches(s, "note", channel, note) for s in self.config["pads"]):
            return True
        return self._next_input("note", channel, note, False)

    def _press_pad(self, spec: str) -> bool:
        for i, s in enumerate(self.config["pads"]):
            if s == spec:
                self.set_bank(i)
                return True
        return False

    def handle_program(self, channel: int, program: int) -> bool:
        """Program Change (manche Controller-Pads senden das). True = verarbeitet."""
        spec = f"pc:{channel}:{program}"
        self._last_pc = time.monotonic()
        if self.learn_slot and self.learn_slot[0] in ("pad", "next"):
            self._learn_input(spec)
            return True
        return self._press_pad(spec)

    def handle_note(self, channel: int, note: int, velocity: int) -> bool:
        """Note-On. True = gehoert zum Controller (Pad), nicht spielen."""
        spec = f"note:{channel}:{note}"
        if self.learn_slot and self.learn_slot[0] in ("pad", "next"):
            self._learn_input(spec)
            return True
        for i, s in enumerate(self.config["pads"]):     # fest angelernte Pads haben Vorrang
            if _matches(s, "note", channel, note):
                if i != self.bank:
                    self.set_bank(i)
                return True
        return self._next_input("note", channel, note, True)

    def is_pad_note(self, channel: int, note: int) -> bool:
        return any(_matches(s, "note", channel, note)
                   for s in self.config["pads"] + self.config["next_bank"])

    def handle_cc(self, channel: int, cc: int, value: int) -> bool:
        """True = CC wurde hier verarbeitet."""
        x = value / 127.0
        spec = f"cc:{channel}:{cc}"
        if self.learn_slot:
            self._learn_cc(spec, value)
            return True
        for i, s in enumerate(self.config["pads"]):     # fest angelernte Pads/Knobs haben Vorrang
            if _matches(s, "cc", channel, cc):
                if value > 0:
                    self.set_bank(i)
                return True
        for i, s in enumerate(self.config["knobs"]):
            if _matches(s, "cc", channel, cc):
                self._knob(i, x)
                return True
        if self._next_input("cc", channel, cc, value >= 64):
            return True
        mw = self.config["modwheel"]
        if mw.get("param") and _matches(mw.get("input"), "cc", channel, cc):
            self.params.set_norm(mw["param"], x, source="midi")
            return True
        return False

    def _learn_cc(self, spec: str, value: int):
        """Nur echte Bewegungen lernen: ein zitterndes Poti darf kein Pad werden."""
        first = self._learn_seen.setdefault(spec, value)
        slot, i = self.learn_slot
        if slot == "knob":
            if any(_matches(n, "cc", *map(int, spec.split(":")[1:])) for n in self.config["next_bank"]):
                return                       # Weiter-Taste (z.B. Pads im CC-Modus) ist kein Knob
            if abs(value - first) >= LEARN_MIN_MOVE:
                self._learn_input(spec)
            return
        # Pad als CC: Druecken = Sprung auf >= 64 (typisch 0 -> 127). Eingaenge, die
        # schon als Knob belegt sind, oder das Mod-Rad zaehlen nicht.
        if spec in self.config["knobs"] or _matches(self.config["modwheel"].get("input"),
                                                    "cc", *map(int, spec.split(":")[1:])):
            return
        if value >= 64 and (value - first >= 64 or (value == first == 127)):
            self._learn_input(spec)
        elif value < 64:
            self._learn_seen[spec] = value

    def _knob(self, i: int, x: float):
        pid = self.config["banks"][self.bank]["params"][i]
        last_x, self._last_x[i] = self._last_x[i], x
        if not pid:
            return
        p = self.params.params[pid]
        cur = self.params.values[pid]
        if self._picked[i] and cur != self._last_set[i]:
            self._picked[i] = False          # Wert wurde woanders geaendert (Web, Preset)
        if self.config["takeover"] == "jump":
            self._picked[i] = True
        elif not self._picked[i]:
            c = p.to_norm(cur)
            crossed = last_x is not None and (last_x - c) * (x - c) <= 0
            if p.from_norm(x) == cur or abs(x - c) <= PICKUP_TOLERANCE or crossed:
                self._picked[i] = True
                self._announce()
            else:
                return
        self.params.set_norm(pid, x, source="midi")
        self._last_set[i] = self.params.values[pid]
