"""MIDI-Looper: nimmt gespielte Noten auf (nicht den Klang) und spielt sie im Kreis ab.

Weil nur Noten-Ereignisse gespeichert werden, klingt die Schleife immer mit den
aktuellen Reglern: Filter, Hüllkurve, Effekte usw. lassen sich live verdrehen.
Kostet praktisch keine CPU (eine Liste mit Zeitstempeln).

Tasten (Controller oder Web-UI):
  Record  leer/gestoppt -> scharf, die Aufnahme startet mit der ersten Note
          (kein Leerlauf am Anfang). Alte Schleife wird dabei ersetzt.
          waehrend Aufnahme -> Aufnahme beenden und sofort abspielen
          waehrend Wiedergabe -> Overdub: Gespieltes kommt zur Schleife dazu
          waehrend Overdub -> Overdub beenden, Schleife laeuft weiter
  Stop    beendet Aufnahme / Wiedergabe / Overdub (Schleife bleibt erhalten)
  Play    spielt die Schleife von vorn, beendet eine laufende Aufnahme / Overdub
  Clear   (nur Web-UI) Schleife loeschen

Alles hier laeuft im Audio-Thread (Engine.render), Befehle kommen ueber die
Event-Queue der Engine. Zeiten sind Samples ab Schleifenanfang.
"""
from __future__ import annotations

import bisect

MAX_SECONDS = 60.0        # laengste Aufnahme, danach stoppt sie von selbst
MIN_SECONDS = 0.25        # kuerzere Aufnahmen (versehentlich) werden verworfen

STATES = ("empty", "armed", "recording", "stopped", "playing", "overdub")


class Looper:
    def __init__(self, samplerate: int, notify=None):
        self.sr = samplerate
        self.max_len = int(MAX_SECONDS * samplerate)
        self.notify = notify            # Callback(info) bei Zustandswechsel (Web-UI)
        self.events: list[tuple] = []   # sortiert: (t, seq, kind, note, vel)
        self.pending: list[tuple] = []  # Overdub-Noten, kommen beim naechsten Durchlauf dazu
        self.length = 0
        self.pos = 0
        self.state = "empty"
        self._idx = 0                   # naechstes abzuspielendes Ereignis
        self._seq = 0
        self._held: set[int] = set()    # waehrend Aufnahme/Overdub gedrueckte Noten
        self._sustained: set[int] = set()   # losgelassen, aber vom Pedal gehalten
        self._pedal = False

    # ------------------------------ Status ------------------------------
    def info(self) -> dict:
        sr = self.sr
        return {"state": self.state, "length": round(self.length / sr, 3),
                "pos": round(self.pos / sr, 3), "max": MAX_SECONDS,
                "notes": sum(1 for e in self.events if e[2] == "on")}

    def _set(self, state: str):
        self.state = state
        if self.notify:
            try:
                self.notify(self.info())
            except Exception:
                pass

    # ------------------------------ Befehle ------------------------------
    def command(self, cmd: str) -> bool:
        """Liefert True, wenn die Schleifen-Noten jetzt verstummen muessen."""
        s = self.state
        if cmd == "record":
            if s in ("empty", "stopped"):
                self._held.clear()
                self._sustained.clear()
                self._set("armed")
            elif s == "armed":                      # nochmal Record = abbrechen
                self._set("stopped" if self.length else "empty")
            elif s == "recording":
                if self._finish_recording():
                    self._start_play()
            elif s == "playing":
                self._held.clear()
                self._sustained.clear()
                self._set("overdub")
            elif s == "overdub":
                self._finish_overdub()
                self._set("playing")
        elif cmd == "stop":
            if s == "armed":
                self._set("stopped" if self.length else "empty")
            elif s == "recording":
                self._finish_recording()
            elif s in ("playing", "overdub"):
                if s == "overdub":
                    self._finish_overdub()
                self.pos = 0
                self._set("stopped")
                return True
        elif cmd == "play":
            if s == "recording":
                if self._finish_recording():
                    self._start_play()
            elif s == "overdub":
                self._finish_overdub()
                self._set("playing")
            elif s in ("stopped", "playing") and self.length:
                self._start_play()
                return s == "playing"               # Neustart von vorn: alte Noten aus
        elif cmd == "clear":
            was = s in ("playing", "overdub")
            self.events, self.pending, self.length, self.pos = [], [], 0, 0
            self._set("empty")
            return was
        return False

    def _start_play(self):
        self.pos = 0
        self._idx = 0
        self._set("playing")

    def _add(self, lst, t, kind, note, vel=0):
        self._seq += 1
        lst.append((t, self._seq, kind, note, vel))

    def _close_held(self, lst, t):
        """Noch gedrueckte / vom Pedal gehaltene Noten bei t loslassen."""
        for n in sorted(self._held | self._sustained):
            self._add(lst, t, "off", n)
        self._held.clear()
        self._sustained.clear()

    def _finish_recording(self) -> bool:
        if self.pos < MIN_SECONDS * self.sr:
            self.events, self.length, self.pos = [], 0, 0
            self._set("empty")
            return False
        self.length = self.pos
        self._close_held(self.events, self.length - 1)
        self.events.sort()
        self.pos = 0
        self._set("stopped")
        return True

    def _finish_overdub(self):
        self._close_held(self.pending, self.pos)
        self._merge()

    def _merge(self):
        if self.pending:
            self.events = sorted(self.events + self.pending)
            self.pending = []
            self._idx = bisect.bisect_left(self.events, (self.pos,))

    # ------------------------------ Eingang ------------------------------
    def capture(self, kind: str, note: int, val):
        """Live gespieltes Ereignis (Audio-Thread, am Blockanfang)."""
        s = self.state
        if s == "armed":
            if kind != "on":
                return
            self.events, self.pending, self.length, self.pos, self._idx = [], [], 0, 0, 0
            self._pedal = False
            self._set("recording")
            s = "recording"
        if s not in ("recording", "overdub"):
            return
        lst = self.events if s == "recording" else self.pending
        t = self.pos
        if kind == "on":
            self._sustained.discard(note)
            self._held.add(note)
            self._add(lst, t, "on", note, val)
        elif kind == "off":
            if note not in self._held:
                return                              # vor der Aufnahme angeschlagen
            self._held.discard(note)
            if self._pedal:
                self._sustained.add(note)           # Pedal haelt: Note-Off kommt beim Loslassen
            else:
                self._add(lst, t, "off", note)
        elif kind == "pedal":
            self._pedal = bool(val)
            if not val:
                for n in sorted(self._sustained):
                    self._add(lst, t, "off", n)
                self._sustained.clear()

    # ------------------------------ Abspielen ------------------------------
    def tick(self, n: int, speed: float = 1.0) -> list[tuple]:
        """Einen Audio-Block weiter. Liefert die Schleifen-Ereignisse (kind, note, vel).
        speed: Wiedergabe-Tempo (1 = original). Die Position laeuft dann schneller/langsamer
        durch die Schleife, die Tonhoehe bleibt gleich. Aufnahme laeuft immer in Echtzeit."""
        s = self.state
        if s == "recording":
            self.pos += n
            if self.pos >= self.max_len:             # Maximum erreicht: Aufnahme stoppt selbst
                self.pos = self.max_len
                self._finish_recording()
            return []
        if s not in ("playing", "overdub") or not self.length:
            return []
        out = []
        ev, L = self.events, self.length
        end = self.pos + n * max(0.1, float(speed))
        while True:
            stop = min(end, L)
            while self._idx < len(ev) and ev[self._idx][0] < stop:
                _, _, kind, note, vel = ev[self._idx]
                out.append((kind, note, vel))
                self._idx += 1
            if end < L:
                break
            end -= L                                 # Schleifenende: von vorn
            self.pos = 0
            self._merge()
            ev = self.events
            self._idx = 0
        self.pos = end
        return out
