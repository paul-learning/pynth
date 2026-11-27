"""Presets als JSON-Dateien.

Format (verschachtelt nach Gruppe, Schluessel = Teil nach dem Punkt):
{
  "name": "Warm Pad",
  "osc1":   {"wave": "saw", "octave": 0},
  "osc2":   {"wave": "sine", "octave": 0, "detune": 5},
  "noise":  {"type": "white"},
  "mix":    {"osc1": 0.5, "osc2": 0.3, "noise": 0.0},
  "env":    {"attack": 0.5, "decay": 1.0, "sustain": 0.7, "release": 2.0},
  "filter": {"cutoff": 1000, "resonance": 0.5},
  "lfo":    {"wave": "sine", "target": "filter", "rate": 0.3, "depth": 0.2},
  "fx":     {"delay_mix": 0.1, "delay_time": 0.35, "delay_feedback": 0.3,
             "reverb_mix": 0.3, "reverb_size": 0.7},
  "master": {"volume": 0.7}
}
Fehlende Werte bekommen den Default, unbekannte werden ignoriert. Aeltere Formate
werden beim Laden uebersetzt: das Mistral-Format ("type", "volume", "envelope",
"effects") und Pynth 0.1 ("osc1.level" usw. -> "mix.osc1").
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .params import PARAMS, RENAMED

_ALIASES = {"envelope": "env", "effects": "fx"}
_KEY_ALIASES = {"type": "wave", "volume": "level", "reverb": "reverb_mix", "delay": "delay_mix"}


def nest(flat: dict, name: str) -> dict:
    out: dict = {"name": name}
    for p in PARAMS:
        g, k = p.id.split(".", 1)
        v = flat.get(p.id, p.default)
        if isinstance(v, float) and v.is_integer() and p.step:
            v = int(v)
        out.setdefault(g, {})[k] = v
    return out


def flatten(data: dict) -> dict:
    flat = {}
    for g, section in data.items():
        if not isinstance(section, dict):
            continue
        g = _ALIASES.get(g, g)
        for k, v in section.items():
            if g == "master" and k == "volume":
                flat["master.volume"] = v
                continue
            if g == "noise" and k == "type" and v not in ("white", "pink"):
                continue
            pid = f"{g}.{_KEY_ALIASES.get(k, k) if g != 'noise' else k}"
            flat[RENAMED.get(pid, pid)] = v
    return flat


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_").lower()
    return s or "preset"


class PresetStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[str]:
        names = []
        for f in sorted(self.folder.glob("*.json")):
            try:
                names.append(json.loads(f.read_text()).get("name", f.stem))
            except (OSError, ValueError):
                continue
        return names

    def _find(self, name: str) -> Path | None:
        for f in self.folder.glob("*.json"):
            try:
                if json.loads(f.read_text()).get("name", f.stem) == name:
                    return f
            except (OSError, ValueError):
                continue
        return None

    def load(self, name: str) -> dict:
        f = self._find(name)
        if f is None:
            raise KeyError(name)
        return flatten(json.loads(f.read_text()))

    def save(self, name: str, flat: dict) -> Path:
        f = self._find(name) or self.folder / f"{_slug(name)}.json"
        f.write_text(json.dumps(nest(flat, name), indent=2, ensure_ascii=False) + "\n")
        return f

    def delete(self, name: str) -> bool:
        f = self._find(name)
        if f:
            f.unlink()
        return f is not None
