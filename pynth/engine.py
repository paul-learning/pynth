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

from . import dsp, fast
from .looper import Looper
from .params import ParamStore

MAX_VOICES = 8            # feste Polyphonie
VELOCITY_SENS = 0.6       # 0 = Anschlag egal, 1 = voll anschlagdynamisch
# Pink-Noise-Filter (Paul Kellet, "economy"), Ausgang auf ~gleiche Lautheit wie White skaliert
PINK_B = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786])
PINK_A = np.array([1.0, -2.494956002, 2.017265875, -0.522189400])
PINK_GAIN = 12.0
SUB_BLOCK = 256           # Filter-Teilblock ohne LFO (Cutoff-Glaettung)
SUB_BLOCK_LFO = 64        # mit LFO aufs Filter: feiner, sonst "treppt" der Cutoff hoerbar (Knackser)
LFO_SMOOTH_S = 0.002      # Square/Saw-LFO: Spruenge ueber ~2 ms glaetten (One-Pole)
HEADROOM = 0.25           # Pegel pro Stimme vor dem Filter; 8 Stimmen bleiben unter dem Limiter
EXP_K = 6.9   # exp(-6.9) ~ -60 dB: Decay/Release sind "fertig" nach der eingestellten Zeit


class Voice:
    __slots__ = ("active", "note", "vel", "phase1", "phase2", "t", "start_level",
                 "released", "t_rel", "rel_level", "age", "held_by_pedal", "src")

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
        self.src = 0            # 0 = live gespielt, 1 = Looper (eigene Stimmen, stoeren sich nicht)


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
        self.bend_now = 0.0       # Pitchbend am Ende des letzten Blocks (fuer weichen Verlauf)
        self.pedal = False
        self.lfo_phase = 0.0
        self.filter_zi = np.zeros(2)          # Biquad-Zustand (numpy-Pfad)
        self.svf_state = np.zeros(2)          # SVF-Zustand (C-Pfad)
        self.lfo_zi = np.zeros(1)             # Glaettung fuer Square/Saw-LFO
        c = math.exp(-1.0 / (LFO_SMOOTH_S * samplerate))
        self.lfo_smooth = (np.array([1.0 - c]), np.array([1.0, -c]))
        self.cutoff_now: float | None = None
        self.volume_now: float | None = None
        self.delay = dsp.FeedbackLine(int(1.6 * samplerate))
        self.reverb = dsp.Reverb(samplerate)
        self.rng = np.random.default_rng()
        self.pink_zi = np.zeros(3)
        self._age = 0
        self._ramp_cache: dict[int, np.ndarray] = {}
        self.looper = Looper(samplerate, notify=lambda info: params.broadcast("looper", info))
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

    def looper_cmd(self, cmd: str):
        """"record" | "stop" | "play" | "clear" (wird im Audio-Thread ausgefuehrt)"""
        self.events.put(("loop", cmd, 0))

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

    def _alloc(self, note: int, p, src: int = 0) -> Voice:
        pool = self.voices
        for v in pool:                                  # gleiche Note -> neu anschlagen
            if v.active and v.note == note and v.src == src:
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
            if kind == "loop":
                if self.looper.command(note):
                    self._loop_notes_off(p)
                continue
            if kind in ("on", "off", "pedal"):
                self.looper.capture(kind, note, val)
            self._apply(kind, note, val, p)

    def _loop_notes_off(self, p):
        for v in self.voices:
            if v.active and v.src == 1 and not v.released:
                self._release(v, p)

    def _apply(self, kind, note, val, p, src: int = 0):
        """Ein Ereignis ausfuehren. src 0 = live, 1 = Looper (eigene Stimmen)."""
        if kind == "on":
            v = self._alloc(note, p, src)
            v.start_level = self._level(v, p)       # Start ab aktuellem Pegel: kein Klick
            if not v.active:
                v.phase1 = v.phase2 = 0.0
            v.vel = (1.0 - VELOCITY_SENS) + VELOCITY_SENS * (val / 127.0)
            v.note, v.t, v.released, v.held_by_pedal, v.src = note, 0, False, False, src
            v.active = True
            self._age += 1
            v.age = self._age
        elif kind == "off":
            for v in self.voices:
                if v.active and v.note == note and v.src == src and not v.released:
                    if self.pedal and src == 0:     # Pedal gilt nur live (der Looper nimmt es mit auf)
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
            self.looper.command("stop")

    # ------------------------------- Rendering ------------------------------
    def _ramp(self, n: int) -> np.ndarray:
        r = self._ramp_cache.get(n)
        if r is None:
            r = self._ramp_cache[n] = np.arange(n, dtype=np.float64)
        return r

    def _render_osc(self, wave, inc, phase, ramp, pitch_mod):
        """Alle Stimmen auf einmal: inc/phase haben Form (V,), Ergebnis (V, n)."""
        inc = np.minimum(inc, 0.45)[:, None]
        if pitch_mod is None:
            ph = phase[:, None] + inc * (ramp + 1.0)
            dt = inc
        else:
            dt = inc * pitch_mod[None, :]
            ph = phase[:, None] + np.cumsum(dt, axis=1)
        new_phase = ph[:, -1] % 1.0
        return dsp.oscillator(wave, ph % 1.0, dt), new_phase

    def _env(self, v: Voice, p, n, ramp):
        sr = self.sr
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
        return env * v.vel

    def _render_voices(self, voices, p, n, ramp, pitch_mod):
        """Alle aktiven Stimmen vektorisiert (eine numpy-Rechnung statt eine pro Stimme).
        Liefert (Summe Oszillatoren * Huellkurve, Summe der Huellkurven)."""
        sr = self.sr
        semis = np.array([v.note - 69 for v in voices], dtype=np.float64)
        env = np.stack([self._env(v, p, n, ramp) for v in voices])
        osc = np.zeros((len(voices), n))
        for k, octave_key, extra in ((1, "osc1.octave", 0.0), (2, "osc2.octave", p["osc2.detune"] / 100.0)):
            level = p[f"mix.osc{k}"]
            attr = f"phase{k}"
            phase = np.array([getattr(v, attr) for v in voices])
            inc = 440.0 * 2.0 ** ((semis + 12 * p[octave_key] + extra) / 12.0) / sr
            if level > 0:
                sig, phase = self._render_osc(p[f"osc{k}.wave"], inc, phase, ramp, pitch_mod)
                osc += level * sig
            else:                                   # stumm: nur Phase weiterdrehen (spart CPU)
                adv = inc * (pitch_mod.sum() if pitch_mod is not None else n)
                phase = (phase + adv) % 1.0
            for v, ph in zip(voices, phase):
                setattr(v, attr, float(ph))
        return (osc * env).sum(axis=0), env.sum(axis=0)

    def _render_voices_c(self, voices, p, n, pitch_mod):
        """Wie _render_voices, aber die Schleife pro Sample laeuft im C-Kern."""
        sr = self.sr
        a, d, s = p["env.attack"] * sr, p["env.decay"] * sr, p["env.sustain"]
        r = p["env.release"] * sr
        w1, w2 = fast.WAVE_ID.get(p["osc1.wave"], 0), fast.WAVE_ID.get(p["osc2.wave"], 0)
        l1, l2 = float(p["mix.osc1"]), float(p["mix.osc2"])
        o1 = 12 * p["osc1.octave"]
        o2 = 12 * p["osc2.octave"] + p["osc2.detune"] / 100.0
        k = 440.0 / sr
        vp, ph = [], []
        for v in voices:
            semi = v.note - 69
            vp += (w1, min(k * 2.0 ** ((semi + o1) / 12.0), 0.45), l1,
                   w2, min(k * 2.0 ** ((semi + o2) / 12.0), 0.45), l2,
                   1.0 if v.released else 0.0, v.t_rel if v.released else v.t,
                   v.start_level, a, d, s, v.rel_level, r, v.vel, 0.0)
            ph += (v.phase1, v.phase2)
        vp = np.array(vp)
        ph = np.array(ph)
        mix, env_sum = np.zeros(n), np.zeros(n)
        pm = fast.ptr(pitch_mod) if pitch_mod is not None else None
        fast.lib.render_voices(len(voices), n, fast.ptr(vp), fast.ptr(ph), pm,
                               fast.ptr(mix), fast.ptr(env_sum))
        for i, v in enumerate(voices):
            v.phase1, v.phase2 = float(ph[2 * i]), float(ph[2 * i + 1])
            if v.released:
                v.t_rel += n
                if v.t_rel >= r:
                    v.active = False
            else:
                v.t += n
        return mix, env_sum

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
        for kind, note, vel in self.looper.tick(n, p.get("looper.speed", 1.0)):     # Schleife: Noten wie live gespielt
            self._apply(kind, note, vel, p, src=1)
        sr = self.sr
        ramp = self._ramp(n)

        # LFO
        rate = p["lfo.rate"]
        lfo = dsp.lfo_wave(p["lfo.wave"], (self.lfo_phase + ramp * rate / sr) % 1.0)
        if p["lfo.wave"] in ("square", "saw"):      # Spruenge ~2 ms weich machen, sonst Klick
            lfo, self.lfo_zi = lfilter(*self.lfo_smooth, lfo, zi=self.lfo_zi)
        self.lfo_phase = (self.lfo_phase + n * rate / sr) % 1.0
        target, depth = p["lfo.target"], p["lfo.depth"]
        if depth <= 0:
            target = "off"
        pitch_mod = 2.0 ** (lfo * depth * 2.0 / 12.0) if target == "pitch" else None
        # Pitchbend: pro Sample von altem zu neuem Wert gleiten (kein Treppeneffekt)
        b0, b1 = self.bend_now, self.bend
        self.bend_now = b1
        if b0 or b1:
            bend = 2.0 ** ((b0 + (b1 - b0) * (ramp + 1.0) / n) * p["bend.range"] / 12.0)
            pitch_mod = bend if pitch_mod is None else pitch_mod * bend

        # Stimmen. Noise wird nur einmal pro Block erzeugt und mit der Summe der
        # Huellkurven multipliziert (klingt gleich, kostet nur einen Bruchteil).
        active = [v for v in self.voices if v.active]
        if active and fast.lib is not None:
            mix, env_sum = self._render_voices_c(active, p, n, pitch_mod)
        elif active:
            mix, env_sum = self._render_voices(active, p, n, ramp, pitch_mod)
        else:
            mix, env_sum = np.zeros(n), np.zeros(n)
        if p["mix.noise"] > 0:
            mix += p["mix.noise"] * env_sum * self._noise(p["noise.type"], n)
        mix *= HEADROOM

        # Filter: C-Kern mit Cutoff pro Sample (SVF), sonst Biquad in Teilbloecken
        cutoff = p["filter.cutoff"]
        prev = self.cutoff_now if self.cutoff_now is not None else cutoff
        self.cutoff_now = cutoff
        q = 0.707 * 20.0 ** p["filter.resonance"]
        if fast.lib is not None:
            out = self._filter_c(mix, n, ramp, prev, cutoff, q, lfo if target == "filter" else None, depth)
        else:
            out = self._filter_np(mix, n, prev, cutoff, q, lfo if target == "filter" else None, depth)
        return self._post(out, p, n, ramp, lfo, target, depth, t0)

    def _filter_c(self, mix, n, ramp, prev, cutoff, q, lfo, depth):
        """SVF mit Cutoff pro Sample: weicher Verlauf, auch bei voller LFO-Tiefe."""
        lc0 = math.log(prev)
        logc = lc0 + (math.log(cutoff) - lc0) * ((ramp + 1.0) / n)
        if lfo is not None:
            logc = logc + lfo * (depth * 3.0 * math.log(2.0))     # +-3 Oktaven
        cuts = np.exp(logc)
        np.clip(cuts, 20.0, 0.45 * self.sr, out=cuts)
        out = np.empty(n)
        fast.lib.svf_lowpass(n, fast.ptr(mix), fast.ptr(out), fast.ptr(cuts),
                             1.0 / q, float(self.sr), fast.ptr(self.svf_state))
        return out

    def _filter_np(self, mix, n, prev, cutoff, q, lfo, depth):
        sr = self.sr
        target = "filter" if lfo is not None else ""
        sub = SUB_BLOCK_LFO if target == "filter" else SUB_BLOCK
        nsub = max(1, -(-n // sub))
        cuts = np.exp(np.linspace(math.log(prev), math.log(cutoff), nsub + 1)[1:])
        if target == "filter":
            centers = np.minimum(np.arange(nsub) * sub + sub // 2, n - 1)
            cuts = cuts * 2.0 ** (lfo[centers] * depth * 3.0)   # +-3 Oktaven
        out = np.empty(n)
        zi = self.filter_zi
        for i in range(nsub):
            a0, a1 = i * sub, min(n, (i + 1) * sub)
            b, a = dsp.lowpass_coeffs(float(cuts[i]), q, sr)
            out[a0:a1], zi = lfilter(b, a, mix[a0:a1], zi=zi)
        self.filter_zi = zi
        return out

    def _post(self, out, p, n, ramp, lfo, target, depth, t0):
        sr = self.sr

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
