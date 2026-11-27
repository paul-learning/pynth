"""Zentrales Parametermodell.

Jeder Regler existiert genau einmal hier. Web-UI, MIDI-CC-Mapping, Presets und
Engine benutzen alle dieselbe ID (z.B. "filter.cutoff"). MIDI und Web-Slider
arbeiten mit normalisierten Werten 0..1; die Umrechnung in echte Werte
(Hz, Sekunden, ...) passiert nur hier.
"""
from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass, field, asdict, replace

WAVES = ("sine", "saw", "square", "triangle")


@dataclass(frozen=True)
class Param:
    id: str
    label: str
    group: str
    min: float = 0.0
    max: float = 1.0
    default: float | str = 0.0
    curve: str = "lin"          # "lin" | "exp" (fuer Hz und Zeiten)
    step: float = 0.0           # >0 rastet ein (Oktaven, Halbtoene, ...)
    unit: str = ""
    choices: tuple = field(default_factory=tuple)
    preset: bool = True         # False = nicht Teil des Klangs (Preset laden laesst den Wert stehen)
    limits: tuple = ()          # (lo, hi): Bereich min..max ist in der Web-UI einstellbar, in diesen Grenzen

    @property
    def is_choice(self) -> bool:
        return bool(self.choices)

    def clamp(self, value):
        if self.is_choice:
            if isinstance(value, (int, float)):
                return self.choices[max(0, min(int(value), len(self.choices) - 1))]
            return value if value in self.choices else self.default
        v = max(self.min, min(self.max, float(value)))
        if self.step:
            v = round(v / self.step) * self.step
        return v

    def from_norm(self, x: float):
        x = max(0.0, min(1.0, float(x)))
        if self.is_choice:
            return self.choices[min(int(x * len(self.choices)), len(self.choices) - 1)]
        if self.curve == "exp":
            v = self.min * (self.max / self.min) ** x
        else:
            v = self.min + x * (self.max - self.min)
        return self.clamp(v)

    def to_norm(self, value) -> float:
        if self.is_choice:
            i = self.choices.index(value) if value in self.choices else 0
            return (i + 0.5) / len(self.choices)
        v = float(value)
        if self.curve == "exp":
            return math.log(v / self.min) / math.log(self.max / self.min)
        return (v - self.min) / (self.max - self.min)


def _osc(n, wave, octave):
    g = f"osc{n}"
    return [
        Param(f"{g}.wave", "Wellenform", g, default=wave, choices=WAVES),
        Param(f"{g}.octave", "Oktave", g, -2, 2, octave, step=1),
    ]


# Reihenfolge hier = Reihenfolge der Bereiche in der Web-UI
PARAMS: list[Param] = [
    *_osc(1, "saw", 0),
    *_osc(2, "square", 0),
    Param("osc2.detune", "Verstimmung", "osc2", -50, 50, 7, unit="ct"),
    Param("bend.range", "Pitchbend", "osc2", 0, 12, 2, step=1, unit="HT"),
    Param("noise.type", "Farbe", "noise", default="white", choices=("white", "pink")),

    Param("mix.osc1", "Osc 1", "mix", 0, 1, 0.6),
    Param("mix.osc2", "Osc 2", "mix", 0, 1, 0.3),
    Param("mix.noise", "Noise", "mix", 0, 1, 0.0),
    Param("master.volume", "Master", "mix", 0, 1, 0.7),

    Param("env.attack", "Attack", "env", 0.002, 8, 0.01, curve="exp", unit="s"),
    Param("env.decay", "Decay", "env", 0.01, 8, 0.4, curve="exp", unit="s"),
    Param("env.sustain", "Sustain", "env", 0, 1, 0.7),
    Param("env.release", "Release", "env", 0.01, 10, 0.5, curve="exp", unit="s"),

    Param("filter.cutoff", "Cutoff", "filter", 40, 18000, 2500, curve="exp", unit="Hz"),
    Param("filter.resonance", "Resonanz", "filter", 0, 1, 0.2),
    Param("filter.drive", "Drive", "filter", 0, 1, 0.0),

    Param("lfo.wave", "Wellenform", "lfo", default="sine", choices=("sine", "triangle", "square", "saw")),
    Param("lfo.target", "Ziel", "lfo", default="filter", choices=("off", "filter", "pitch", "volume")),
    Param("lfo.rate", "Rate", "lfo", 0.05, 20, 2.0, curve="exp", unit="Hz"),
    Param("lfo.depth", "Tiefe", "lfo", 0, 1, 0.0),

    Param("fx.delay_mix", "Delay Mix", "fx", 0, 1, 0.0),
    Param("fx.delay_time", "Delay Zeit", "fx", 0.05, 1.5, 0.35, curve="exp", unit="s"),
    Param("fx.delay_feedback", "Delay Feedback", "fx", 0, 0.9, 0.35),
    Param("fx.reverb_mix", "Hall Mix", "fx", 0, 1, 0.15),
    Param("fx.reverb_size", "Hall Größe", "fx", 0, 1, 0.6),

    # Tempo der Looper-Wiedergabe (Noten kommen schneller/langsamer, Tonhoehe bleibt)
    Param("looper.speed", "Tempo", "looper", 0.25, 5.0, 1.0, curve="exp", unit="×", preset=False,
          limits=(0.1, 10.0)),
]

# Umbenannte Parameter (alte ID -> neue ID). Gilt fuer Presets, state.json und midi_map.json.
RENAMED = {"osc1.level": "mix.osc1", "osc2.level": "mix.osc2", "noise.level": "mix.noise"}

GROUP_LABELS = {
    "osc1": "Oszillator 1", "osc2": "Oszillator 2", "noise": "Noise", "mix": "Mix",
    "env": "Hüllkurve (ADSR)", "filter": "Ladder-Filter", "lfo": "LFO", "fx": "Effekte",
    "looper": "Looper",
}


class ParamStore:
    """Haelt die aktuellen Werte und benachrichtigt Abonnenten (Web-UI)."""

    def __init__(self):
        self.params = {p.id: p for p in PARAMS}
        self.values = {p.id: p.default for p in PARAMS}
        self.preset_name = "Init"
        self.dirty = False
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    # --- Werte ---------------------------------------------------------
    def set(self, pid: str, value, source: str = "api") -> bool:
        p = self.params.get(pid)
        if p is None:
            return False
        v = p.clamp(value)
        if self.values[pid] == v:
            return True
        self.values[pid] = v      # einzelne dict-Zuweisung ist atomar (GIL)
        self.dirty = True
        self.broadcast("param", {"id": pid, "value": v, "source": source})
        return True

    def set_norm(self, pid: str, x: float, source: str = "midi") -> bool:
        p = self.params.get(pid)
        return p is not None and self.set(pid, p.from_norm(x), source)

    def load_flat(self, flat: dict, name: str | None = None):
        for pid, p in self.params.items():
            if not p.preset and pid not in flat:
                continue                    # z.B. Looper-Tempo: Preset-Wechsel aendert es nicht
            self.values[pid] = p.clamp(flat.get(pid, p.default))
        if name:
            self.preset_name = name
        self.dirty = True
        self.broadcast("values", {"values": dict(self.values), "preset": self.preset_name})

    def schema(self) -> list[dict]:
        return [asdict(self.params[p.id]) for p in PARAMS]

    # --- einstellbarer Bereich (z.B. Looper-Tempo) ----------------------
    def set_range(self, pid: str, lo: float, hi: float) -> bool:
        p = self.params.get(pid)
        if p is None or not p.limits:
            return False
        a, b = p.limits
        lo, hi = max(a, min(b, float(lo))), max(a, min(b, float(hi)))
        if hi <= lo * 1.01:                 # min muss deutlich unter max liegen
            return False
        np_ = replace(p, min=round(lo, 3), max=round(hi, 3))
        self.params[pid] = np_
        self.dirty = True
        self.broadcast("schema", asdict(np_))
        self.set(pid, self.values[pid], source="range")    # aktuellen Wert in den Bereich holen
        return True

    def schema_of(self, pid: str) -> dict | None:
        p = self.params.get(pid)
        return asdict(p) if p else None

    def ranges(self) -> dict:
        """Geaenderte Bereiche fuer state.json: {pid: [min, max]}"""
        base = {p.id: p for p in PARAMS}
        return {pid: [p.min, p.max] for pid, p in self.params.items()
                if p.limits and (p.min, p.max) != (base[pid].min, base[pid].max)}

    # --- Events fuer Web-UI (Server-Sent Events) ------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def broadcast(self, event: str, data):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass
