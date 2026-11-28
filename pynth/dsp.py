"""Vektorisierte DSP-Bausteine (numpy). Keine Python-Schleifen pro Sample."""
from __future__ import annotations

import math
import ctypes
import numpy as np

from . import fast

TWO_PI = 2.0 * math.pi


def lfilter(b, a, x, zi):
    """Wie scipy.signal.lfilter(b, a, x, zi=zi) -> (y, zf), a[0] = 1.
    Mit C-Kern ohne scipy (scipy zu laden kostet auf dem Pi beim Start Sekunden),
    sonst wird scipy erst beim ersten Aufruf geladen."""
    if fast.lib is None:
        from scipy.signal import lfilter as sp_lfilter
        return sp_lfilter(b, a, x, zi=zi)
    m = max(len(a), len(b)) - 1
    bb, aa = np.zeros(m + 1), np.zeros(m + 1)
    bb[:len(b)], aa[:len(a)] = b, a
    x = np.ascontiguousarray(x, dtype=np.float64)
    z = np.array(zi, dtype=np.float64)
    y = np.empty_like(x)
    fast.lib.iir_filter(len(x), fast.ptr(x), fast.ptr(y), fast.ptr(bb), fast.ptr(aa), m, fast.ptr(z))
    return y, z


def _polyblep(t: np.ndarray, dt) -> np.ndarray:
    """Band-limitierte Korrektur an Sprungstellen (gegen Aliasing bei Saw/Square)."""
    dt = np.broadcast_to(dt, t.shape)
    out = np.zeros_like(t)
    m = t < dt
    if m.any():
        x = t[m] / dt[m]
        out[m] = x + x - x * x - 1.0
    m = t > 1.0 - dt
    if m.any():
        x = (t[m] - 1.0) / dt[m]
        out[m] = x * x + x + x + 1.0
    return out


def oscillator(wave: str, t: np.ndarray, dt) -> np.ndarray:
    """t = Phase 0..1 pro Sample, dt = Phasenschritt (Frequenz/Samplerate)."""
    if wave == "sine":
        return np.sin(TWO_PI * t)
    if wave == "saw":
        return 2.0 * t - 1.0 - _polyblep(t, dt)
    if wave == "square":
        sq = np.where(t < 0.5, 1.0, -1.0)
        return sq + _polyblep(t, dt) - _polyblep((t + 0.5) % 1.0, dt)
    if wave == "triangle":
        return 4.0 * np.abs(t - 0.5) - 1.0
    return np.zeros_like(t)


def lfo_wave(wave: str, t: np.ndarray) -> np.ndarray:
    """Bipolar -1..1, ohne Band-Limitierung (LFO ist langsam)."""
    if wave == "sine":
        return np.sin(TWO_PI * t)
    if wave == "triangle":
        return 1.0 - 4.0 * np.abs(t - 0.5)
    if wave == "square":
        return np.where(t < 0.5, 1.0, -1.0)
    if wave == "saw":
        return 2.0 * t - 1.0
    return np.zeros_like(t)


def lowpass_coeffs(cutoff: float, q: float, sr: float):
    """RBJ-Biquad-Tiefpass."""
    cutoff = min(max(cutoff, 20.0), 0.45 * sr)
    w0 = TWO_PI * cutoff / sr
    cw, alpha = math.cos(w0), math.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    b0 = (1.0 - cw) / 2.0 / a0
    return (np.array([b0, 2.0 * b0, b0]),
            np.array([1.0, -2.0 * cw / a0, (1.0 - alpha) / a0]))


class FeedbackLine:
    """Ringpuffer fuer v[n] = x[n] + g * v[n-d].

    Wird blockweise in Stuecken der Laenge <= d verarbeitet, dadurch ist alles
    vektorisiert. Liefert (v, v verzoegert um d). Basis fuer Delay, Comb, Allpass.
    """

    def __init__(self, size: int):
        self.buf = np.zeros(size)
        self.w = 0
        self._w = ctypes.c_int(0)
        self._w_addr = ctypes.addressof(self._w)
        self._buf_addr = fast.ptr(self.buf)

    def process(self, x: np.ndarray, d: int, g: float):
        size = len(self.buf)
        d = max(1, min(int(d), size - 1))
        n = len(x)
        v = np.empty(n)
        vd = np.empty(n)
        if fast.lib is not None:
            x = np.ascontiguousarray(x, dtype=np.float64)
            self._w.value = self.w
            fast.lib.feedback_line(n, fast.ptr(x), fast.ptr(v), fast.ptr(vd), self._buf_addr,
                                   size, self._w_addr, d, float(g))
            self.w = self._w.value
            return v, vd
        i = 0
        while i < n:
            c = min(d, n - i)
            idx = np.arange(self.w, self.w + c)
            past = self.buf[(idx - d) % size]
            cur = x[i:i + c] + g * past
            self.buf[idx % size] = cur
            v[i:i + c] = cur
            vd[i:i + c] = past
            self.w = (self.w + c) % size
            i += c
        return v, vd


class Reverb:
    """Kleiner Schroeder-Hall: 4 parallele Combs + 2 Allpaesse, Mono."""

    COMBS = (1116, 1188, 1277, 1356)
    ALLPASSES = (556, 441)

    def __init__(self, sr: int):
        k = sr / 44100.0
        self.comb_d = [int(d * k) for d in self.COMBS]
        self.ap_d = [int(d * k) for d in self.ALLPASSES]
        self.combs = [FeedbackLine(d + 1) for d in self.comb_d]
        self.aps = [FeedbackLine(d + 1) for d in self.ap_d]
        self.damp_state = 0.0

    def process(self, x: np.ndarray, size: float) -> np.ndarray:
        g = 0.70 + 0.27 * size
        # sanfte Hoehendaempfung am Eingang (One-Pole), haelt den Hall weich
        y, zf = lfilter([0.4], [1.0, -0.6], x, zi=[self.damp_state])
        self.damp_state = zf[0]
        y = y * 0.25
        acc = np.zeros_like(x)
        for line, d in zip(self.combs, self.comb_d):
            acc += line.process(y, d, g)[0]
        out = acc * 0.25
        for line, d in zip(self.aps, self.ap_d):
            v, vd = line.process(out, d, 0.5)
            out = vd - 0.5 * v
        return out
