"""Polyphone Synth-Engine.

Ablauf pro Audio-Block (z.B. 8192 Samples):
  1. Note-/Bend-Events aus der Queue abarbeiten (thread-sicher)
  2. LFO fuer den ganzen Block berechnen
  3. Jede aktive Stimme: 2 Oszillatoren (+ Rauschen) * ADSR * Velocity
  4. Summe durch den Tiefpass (Biquad, in 256er-Teilbloecken, damit Cutoff-
     Aenderungen und LFO weich laufen)
  5. Master-Lautstaerke (gerampt), Delay, Hall, Soft-Clip

Alle Phasen (Oszillatoren, LFO) laufen ueber Blockgrenzen weiter, Filter und
Effekte behalten ihren Zustand -> keine Knackser zwischen Bloecken.
"""
from __future__ import annotations

import math
import queue
import time

import numpy as np
from scipy.signal import lfilter

from . import dsp
from .params import ParamStore

MAX_VOICES = 8            # feste Polyphonie
BEND_RANGE = 2.0          # Pitchbend +-2 Halbtoene
VELOCITY_SENS = 0.6       # 0 = Anschlag egal, 1 = voll anschlagdynamisch
# Pink-Noise-Filter (Paul Kellet, "economy"), Ausgang auf ~gleiche Lautheit wie White skaliert
PINK_B = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786])
PINK_A = np.array([1.0, -2.494956002, 2.017265875, -0.522189400])
PINK_GAIN = 12.0
SUB_BLOCK = 256
EXP_K = 6.9   # exp(-6.9) ~ -60 dB: Decay/Release sind "fertig" nach der eingestellten Zeit


class Voice:
    __slots__ = ("active", "note", "vel", "phase1", "phase2", "t", "start_level",
                 "released", "t_rel", "rel_level", "age", "held_by_pedal")

    def __init__(self):
        self.active = False
        self.note = -1
        self.vel = 0.0
        self.phase1 = 0.0
        self.phase2 = 0.0
        self.t = 0
        self.start_level = 0.0
        self.released = False
        self.t_rel = 0
        self.rel_level = 0.0
        self.age = 0
        self.held_by_pedal = False


def _gated_env(t: np.ndarray, start: float, a: float, d: float, s: float) -> np.ndarray:
    """Attack (linear ab start_level) -> Decay (exponentiell auf Sustain)."""
    env = np.empty_like(t)
    m = t < a
    env[m] = start + (1.0 - start) * (t[m] / a)
    m = ~m
    env[m] = s + (1.0 - s) * np.exp(-EXP_K * (t[m] - a) / d)
    return env


class Engine:
    def __init__(self, params: ParamStore, samplerate: int = 44100):
        self.params = params
        self.sr = samplerate
        self.voices = [Voice() for _ in range(MAX_VOICES)]
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.bend = 0.0
        self.pedal = False
        self.lfo_phase = 0.0
        self.filter_zi = np.zeros(2)
        self.cutoff_now: float | None = None
        self.volume_now: float | None = None
        self.delay = dsp.FeedbackLine(int(1.6 * samplerate))
        self.reverb = dsp.Reverb(samplerate)
        self.rng = np.random.default_rng()
        self.pink_zi = np.zeros(3)
        self._age = 0
        self._ramp_cache: dict[int, np.ndarray] = {}
        self.stats = {"load": 0.0, "voices": 0, "peak": 0.0, "blocks": 0, "underruns": 0}

    # ---------- thread-sichere Steuerung (MIDI-Thread, Web-Thread) ----------
    def note_on(self, note: int, velocity: int = 100):
        if velocity <= 0:
            self.note_off(note)
        else:
            self.events.put(("on", int(note), int(velocity)))

    def note_off(self, note: int):
        self.events.put(("off", int(note), 0))

    def pitch_bend(self, value: float):
        """value -1..1"""
        self.events.put(("bend", 0, float(value)))

    def sustain_pedal(self, down: bool):
        self.events.put(("pedal", 0, bool(down)))

    def panic(self):
        self.events.put(("panic", 0, 0))

    # ---------------------------- Voice-Handling ----------------------------
    def _level(self, v: Voice, p) -> float:
        if not v.active:
            return 0.0
        sr = self.sr
        if v.released:
            return v.rel_level * math.exp(-EXP_K * v.t_rel / (p["env.release"] * sr))
        return float(_gated_env(np.array([float(v.t)]), v.start_level,
                                p["env.attack"] * sr, p["env.decay"] * sr, p["env.sustain"])[0])

    def _release(self, v: Voice, p):
        v.rel_level = self._level(v, p)
        v.released = True
        v.t_rel = 0
        v.held_by_pedal = False

    def _alloc(self, note: int, p) -> Voice:
        pool = self.voices
        for v in pool:                                  # gleiche Note -> neu anschlagen
            if v.active and v.note == note:
                return v
        for v in pool:                                  # freie Stimme
            if not v.active:
                return v
        released = [v for v in pool if v.released]     # leiseste ausklingende Stimme
        if released:
            return min(released, key=lambda v: self._level(v, p))
        return min(pool, key=lambda v: v.age)           # sonst die aelteste (Voice Stealing)

    def _handle_events(self, p):
        while True:
            try:
                kind, note, val = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "on":
                v = self._alloc(note, p)
                v.start_level = self._level(v, p)       # Start ab aktuellem Pegel: kein Klick
                if not v.active:
                    v.phase1 = v.phase2 = 0.0
                v.vel = (1.0 - VELOCITY_SENS) + VELOCITY_SENS * (val / 127.0)
                v.note, v.t, v.released, v.held_by_pedal = note, 0, False, False
                v.active = True
                self._age += 1
                v.age = self._age
            elif kind == "off":
                for v in self.voices:
                    if v.active and v.note == note and not v.released:
                        if self.pedal:
                            v.held_by_pedal = True
                        else:
                            self._release(v, p)
            elif kind == "bend":
                self.bend = val
            elif kind == "pedal":
                self.pedal = val
                if not val:
                    for v in self.voices:
                        if v.active and v.held_by_pedal and not v.released:
                            self._release(v, p)
            elif kind == "panic":
                for v in self.voices:
                    v.active = False
                self.pedal = False

    # ------------------------------- Rendering ------------------------------
    def _ramp(self, n: int) -> np.ndarray:
        r = self._ramp_cache.get(n)
        if r is None:
            r = self._ramp_cache[n] = np.arange(n, dtype=np.float64)
        return r

    def _render_osc(self, wave, freq, phase, n, ramp, pitch_mod):
        inc = min(freq / self.sr, 0.45)
        if pitch_mod is None:
            ph = phase + inc * (ramp + 1.0)
            dt = inc
        else:
            dt = inc * pitch_mod
            ph = phase + np.cumsum(dt)
        new_phase = float(ph[-1] % 1.0)
        return dsp.oscillator(wave, ph % 1.0, dt), new_phase

    def _render_voice(self, v: Voice, p, n, ramp, pitch_mod):
        """Liefert (Oszillator-Signal * Huellkurve, Huellkurve). Noise kommt spaeter dazu."""
        sr = self.sr
        semis = v.note - 69 + self.bend * BEND_RANGE
        out = np.zeros(n)
        f1 = 440.0 * 2.0 ** ((semis + 12 * p["osc1.octave"]) / 12.0)
        sig, v.phase1 = self._render_osc(p["osc1.wave"], f1, v.phase1, n, ramp, pitch_mod)
        if p["mix.osc1"] > 0:
            out += p["mix.osc1"] * sig
        f2 = 440.0 * 2.0 ** ((semis + 12 * p["osc2.octave"] + p["osc2.detune"] / 100.0) / 12.0)
        sig, v.phase2 = self._render_osc(p["osc2.wave"], f2, v.phase2, n, ramp, pitch_mod)
        if p["mix.osc2"] > 0:
            out += p["mix.osc2"] * sig

        a, d, s = p["env.attack"] * sr, p["env.decay"] * sr, p["env.sustain"]
        if v.released:
            r = p["env.release"] * sr
            env = v.rel_level * np.exp(-EXP_K * (v.t_rel + ramp) / r)
            v.t_rel += n
            if v.t_rel >= r:
                v.active = False
        else:
            env = _gated_env(v.t + ramp, v.start_level, a, d, s)
            v.t += n
        env *= v.vel
        return out * env, env

    def _noise(self, kind: str, n: int) -> np.ndarray:
        white = self.rng.standard_normal(n) * 0.5
        if kind != "pink":
            return white
        pink, self.pink_zi = lfilter(PINK_B, PINK_A, white, zi=self.pink_zi)
        return pink * PINK_GAIN

    def render(self, n: int) -> np.ndarray:
        t0 = time.perf_counter()
        p = dict(self.params.values)        # Schnappschuss: Werte aendern sich nicht mitten im Block
        self._handle_events(p)
        sr = self.sr
        ramp = self._ramp(n)

        # LFO
        rate = p["lfo.rate"]
        lfo = dsp.lfo_wave(p["lfo.wave"], (self.lfo_phase + ramp * rate / sr) % 1.0)
        self.lfo_phase = (self.lfo_phase + n * rate / sr) % 1.0
        target, depth = p["lfo.target"], p["lfo.depth"]
        if depth <= 0:
            target = "off"
        pitch_mod = 2.0 ** (lfo * depth * 2.0 / 12.0) if target == "pitch" else None

        # Stimmen
        # Stimmen. Noise wird nur einmal pro Block erzeugt und mit der Summe der
        # Huellkurven multipliziert (klingt gleich, kostet nur einen Bruchteil).
        mix = np.zeros(n)
        env_sum = np.zeros(n)
        for v in [v for v in self.voices if v.active]:
            sig, env = self._render_voice(v, p, n, ramp, pitch_mod)
            mix += sig
            env_sum += env
        if p["mix.noise"] > 0:
            mix += p["mix.noise"] * env_sum * self._noise(p["noise.type"], n)
        mix *= 0.3

        # Filter (Cutoff geglaettet, LFO in Teilbloecken)
        cutoff = p["filter.cutoff"]
        prev = self.cutoff_now if self.cutoff_now is not None else cutoff
        self.cutoff_now = cutoff
        nsub = max(1, -(-n // SUB_BLOCK))
        cuts = np.exp(np.linspace(math.log(prev), math.log(cutoff), nsub + 1)[1:])
        if target == "filter":
            centers = np.minimum(np.arange(nsub) * SUB_BLOCK + SUB_BLOCK // 2, n - 1)
            cuts = cuts * 2.0 ** (lfo[centers] * depth * 3.0)   # +-3 Oktaven
        q = 0.707 * 20.0 ** p["filter.resonance"]
        out = np.empty(n)
        zi = self.filter_zi
        for i in range(nsub):
            a0, a1 = i * SUB_BLOCK, min(n, (i + 1) * SUB_BLOCK)
            b, a = dsp.lowpass_coeffs(float(cuts[i]), q, sr)
            out[a0:a1], zi = lfilter(b, a, mix[a0:a1], zi=zi)
        self.filter_zi = zi

        # Master-Lautstaerke (gerampt gegen Zipper) + Tremolo
        vol = p["master.volume"]
        prev = self.volume_now if self.volume_now is not None else vol
        self.volume_now = vol
        gain = prev + (vol - prev) * (ramp / n)
        if target == "volume":
            gain = gain * (1.0 - depth * 0.5 * (1.0 + lfo))
        out *= gain

        # Delay (Feedback-Echo)
        d = int(p["fx.delay_time"] * sr)
        _, echo = self.delay.process(out, d, p["fx.delay_feedback"])
        if p["fx.delay_mix"] > 0:
            out = out + p["fx.delay_mix"] * echo
        # Hall
        if p["fx.reverb_mix"] > 0:
            out = out + p["fx.reverb_mix"] * self.reverb.process(out, p["fx.reverb_size"])

        out = np.tanh(out)                  # weicher Limiter statt hartem Clipping

        dur = n / sr
        st = self.stats
        st["load"] = 0.8 * st["load"] + 0.2 * ((time.perf_counter() - t0) / dur)
        st["voices"] = sum(1 for v in self.voices if v.active)
        st["peak"] = float(np.max(np.abs(out))) if n else 0.0
        st["blocks"] += 1
        return out.astype(np.float32)
