"""Laedt den C-Kern (_kernel.c) fuer die DSP-Schleifen pro Sample.

Beim ersten Start wird _kernel.c mit dem System-Compiler (`cc`) zu einer
kleinen .so uebersetzt und zwischengespeichert. Klappt das nicht (kein
Compiler, kein Schreibrecht), ist `lib` None und die Engine rechnet mit
numpy/scipy wie bisher. Abschalten zum Vergleichen: PYNTH_NO_C=1.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pynth.fast")

SRC = Path(__file__).with_name("_kernel.c")
WAVE_ID = {"sine": 0, "saw": 1, "square": 2, "triangle": 3}
NVP = 16                     # Parameter pro Stimme, siehe _kernel.c

_P = ctypes.c_void_p
_I = ctypes.c_int
_D = ctypes.c_double


def _candidates(tag: str):
    for d in (Path(__file__).resolve().parent / "_build",
              Path.home() / ".cache" / "pynth",
              Path(tempfile.gettempdir()) / f"pynth-{os.getuid()}"):
        yield d / f"kernel-{tag}.so"


def _compile(so: Path) -> bool:
    so.parent.mkdir(parents=True, exist_ok=True)
    tmp = so.with_suffix(f".{os.getpid()}.tmp")
    cmd = [os.environ.get("CC", "cc"), "-O2", "-shared", "-fPIC", "-o", str(tmp), str(SRC), "-lm"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("C-Kern nicht uebersetzt (%s)", e)
        return False
    if r.returncode != 0:
        log.warning("C-Kern: Compiler-Fehler:\n%s", r.stderr.strip()[-2000:])
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, so)
    return True


def _load():
    if os.environ.get("PYNTH_NO_C"):
        return None
    try:
        tag = hashlib.sha1(SRC.read_bytes()).hexdigest()[:12]
    except OSError:
        return None
    for so in _candidates(tag):
        try:
            if not so.exists() and not _compile(so):
                continue
            lib = ctypes.CDLL(str(so))
        except OSError as e:
            log.info("C-Kern %s nicht nutzbar: %s", so, e)
            continue
        lib.render_voices.argtypes = [_I, _I, _P, _P, _P, _P, _P]
        lib.render_voices.restype = None
        lib.svf_lowpass.argtypes = [_I, _P, _P, _P, _D, _D, _P]
        lib.svf_lowpass.restype = None
        lib.ladder_lowpass.argtypes = [_I, _P, _P, _P, _D, _D, _D, _D, _D, _P]
        lib.ladder_lowpass.restype = None
        lib.feedback_line.argtypes = [_I, _P, _P, _P, _P, _I, _P, _I, _D]
        lib.feedback_line.restype = None
        return lib
    log.warning("C-Kern nicht verfuegbar, Engine rechnet mit numpy (mehr CPU-Last). "
                "Abhilfe: sudo apt install gcc")
    return None


lib = _load()


_addr = ctypes.addressof
_cbuf = ctypes.c_char.from_buffer


def ptr(a) -> int:
    """Adresse eines zusammenhaengenden numpy-Arrays fuer ctypes.
    (Schneller als a.ctypes.data, das zaehlt bei ~20 Aufrufen pro Block.)"""
    try:
        return _addr(_cbuf(a))
    except (TypeError, ValueError):          # z.B. schreibgeschuetztes Array
        return a.ctypes.data
