"""Sicheres Speichern der Nutzerdaten (controller.json, midi_map.json, state.json, Presets).

Ein Raspberry Pi wird oft einfach vom Strom getrennt. Ein normales write_text() leert
die Datei erst und schreibt dann; faellt genau dazwischen der Strom aus (oder hat das
Dateisystem den Inhalt noch nicht auf die SD-Karte geschrieben), bleibt eine leere oder
halbe Datei uebrig, und beim naechsten Start waeren alle Belegungen weg.

Darum:
- save_json schreibt in <datei>.tmp, fsync, und ersetzt dann atomar (os.replace).
  Die vorige Fassung bleibt als <datei>.bak liegen.
- load_json liest die Datei; ist sie kaputt oder leer, wird <datei>.bak genommen und die
  kaputte Datei als <datei>.broken aufgehoben (statt sie spaeter zu ueberschreiben).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("pynth.storage")


def _fsync_dir(folder: Path):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read(path: Path):
    text = path.read_text()
    if not text.strip():
        raise ValueError("Datei ist leer")
    return json.loads(text)


def save_json(path, data, *, sort_keys: bool = False, backup: bool = True):
    path = Path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if backup and path.exists():
        try:
            _read(path)                                  # nur eine intakte Fassung als .bak
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        except (OSError, ValueError):
            pass
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def load_json(path, default=None):
    """Datei laden, bei Schaden auf .bak ausweichen. Fehlt die Datei: default."""
    path = Path(path)
    bak = path.with_name(path.name + ".bak")
    try:
        return _read(path)
    except FileNotFoundError:
        return default                                   # bewusst geloescht = zuruecksetzen
    except (OSError, ValueError) as e:
        log.warning("%s ist beschaedigt (%s)", path, e)
        try:
            os.replace(path, path.with_name(path.name + ".broken"))
        except OSError:
            pass
    try:
        data = _read(bak)
        log.warning("Nutze Sicherung %s", bak)
        save_json(path, data, backup=False)
        return data
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("Auch %s nicht lesbar (%s)", bak, e)
        return default
