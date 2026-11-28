"""MIDI-Looper mit fester Laenge in Takten: nimmt gespielte Noten auf (nicht den Klang)
und spielt sie im Kreis ab.

Weil nur Noten-Ereignisse gespeichert werden, klingt die Schleife immer mit den
aktuellen Reglern: Filter, Hüllkurve, Effekte usw. lassen sich live verdrehen.
Kostet praktisch keine CPU (eine Liste mit Zeitstempeln).

Zeiten sind Schlaege (Viertel) ab Schleifenanfang, nicht Sekunden. Die Schleife ist
immer `bars` Takte zu 4 Schlaegen lang; BPM aendern macht sie schneller/langsamer, die
Noten bleiben auf ihren Schlaegen (und die Tonhoehe gleich).

Tasten (Controller oder Web-UI):
  Record  leer/gestoppt -> ein Takt Vorzaehler (Click), dann Aufnahme ueber alle Takte,
          danach laeuft die Schleife von selbst weiter. Alte Schleife wird dabei ersetzt.
          waehrend Vorzaehler -> abbrechen
          waehrend Aufnahme -> weiter aufnehmen, auch ueber das Schleifenende (Overdub)
          waehrend Wiedergabe -> Overdub: Gespieltes kommt zur Schleife dazu
          waehrend Overdub -> Overdub beenden, Schleife laeuft weiter
  Stop    beendet Vorzaehler / Aufnahme / Wiedergabe / Overdub (Schleife bleibt erhalten)
  Play    gestoppt/leer -> Schleife von vorn (auch leer: nur Click, Rec = gleich dazuspielen)
          waehrend Aufnahme / Overdub -> beenden, Schleife laeuft im Takt weiter
          waehrend Wiedergabe -> von vorn
  Takt +/- (Vor-/Zurueck-Taste) aendern den Parameter looper.bars. Weniger Takte schneiden
          nichts weg: die Noten dahinter sind nur stumm und kommen mit mehr Takten wieder.
  Clear   (nur Web-UI) Schleife loeschen

Quantisierung (looper.quantize) wirkt beim Abspielen auf die ganze Schleife: gespeichert
werden die Original-Zeiten, gespielt wird eine aufs Raster gerueckte Kopie (`_play`). Der
Anschlag rueckt aufs Raster, das Loslassen um denselben Betrag (die Notenlaenge bleibt).
Regler drehen wirkt sofort auf alles, "aus" bringt das Original-Timing zurueck.

Alles hier laeuft im Audio-Thread (Engine.render), Befehle kommen ueber die
Event-Queue der Engine.
"""
from __future__ import annotations

import bisect
import math

BEATS_PER_BAR = 4
MAX_BARS = 32
COUNT_IN_EARLY = 0.25     # Schlaege: so knapp vor der Eins gespielt zaehlt als "auf der Eins"
QUANT_GRID = {"aus": 0.0, "1/4": 1.0, "1/8": 0.5, "1/16": 0.25}

STATES = ("empty", "countin", "recording", "stopped", "playing", "overdub")


class Looper:
    def __init__(self, samplerate: int, notify=None):
        self.sr = samplerate
        self.notify = notify            # Callback(info) bei Zustandswechsel und Schleifenanfang (Web-UI)
        self.events: list[tuple] = []   # Original: (t, seq, kind, note, vel, nid), t in Schlaegen
        self._play: list[tuple] = []    # gespielt: events quantisiert, sortiert (nid = seq des Anschlags)
        self.pending: list[tuple] = []  # Overdub-Noten, kommen beim naechsten Durchlauf dazu
        self._early: list[tuple] = []   # im Vorzaehler knapp vor der Eins gespielt
        self.bars = 4
        self.bpm = 120.0
        self.grid = 0.0                 # Quantisierung in Schlaegen, 0 = aus
        self.pos = 0.0                  # Schlaege ab Schleifenanfang (im Vorzaehler negativ)
        self.state = "empty"
        self._idx = 0                   # naechstes abzuspielendes Ereignis
        self._seq = 0
        self._held: dict[int, tuple] = {}       # gedrueckte Noten -> (nid, Verschiebung im Vorzaehler)
        self._sustained: dict[int, tuple] = {}  # losgelassen, aber vom Pedal gehalten
        self._pedal = False
        self._sounding: set[int] = set()        # von der Schleife angeschlagen, noch nicht losgelassen
        self._skip: set[int] = set()            # seq: beim naechsten Durchlauf einmal auslassen

    @property
    def length(self) -> float:
        return float(self.bars * BEATS_PER_BAR)

    # ------------------------------ Status ------------------------------
    def notes(self) -> int:
        L = self.length
        return sum(1 for e in self.events if e[2] == "on" and e[0] < L)

    def info(self) -> dict:
        return {"state": self.state, "pos": round(self.pos, 4), "bars": self.bars,
                "beats": BEATS_PER_BAR, "bpm": self.bpm, "notes": self.notes()}

    def _set(self, state: str):
        self.state = state
        if self.notify:
            try:
                self.notify(self.info())
            except Exception:
                pass

    def _idle(self):
        self.pos = 0.0
        self._idx = 0
        self._set("stopped" if self.notes() else "empty")

    # ------------------------------ Befehle ------------------------------
    def command(self, cmd: str) -> bool:
        """Liefert True, wenn die Schleifen-Noten jetzt verstummen muessen."""
        s = self.state
        running = s in ("playing", "overdub")
        if cmd == "record":
            if s in ("empty", "stopped"):
                self._held.clear()
                self._sustained.clear()
                self._early = []
                self.pos = -float(BEATS_PER_BAR)
                self._set("countin")
            elif s == "countin":                    # nochmal Record = abbrechen
                self._idle()
            elif s == "recording":                  # weiter aufnehmen, auch ueber das Ende hinaus
                self.events.sort()
                self._rebuild()
                self._idx = bisect.bisect_left(self._play, (self.pos,))
                self._set("overdub")
            elif s == "playing":
                self._held.clear()
                self._sustained.clear()
                self._set("overdub")
            elif s == "overdub":
                self._finish_overdub()
                self._set("playing")
        elif cmd == "stop":
            if s == "countin":
                self._idle()
            elif s == "recording":
                self._close_held(self.events, self.pos)
                self.events.sort()
                self._rebuild()
                self._idle()
            elif running:
                if s == "overdub":
                    self._finish_overdub()
                self._sounding.clear()
                self._idle()
                return True
        elif cmd == "play":
            if s == "recording":                    # fertig, Schleife laeuft im Takt weiter
                self._close_held(self.events, self.pos)
                self.events.sort()
                self._rebuild()
                self._idx = bisect.bisect_left(self._play, (self.pos,))
                self._set("playing")
            elif s == "overdub":
                self._finish_overdub()
                self._set("playing")
            elif s in ("empty", "stopped", "countin", "playing"):
                self._sounding.clear()
                self.pos = 0.0
                self._idx = 0
                self._skip.clear()
                self._set("playing")
                return s == "playing"               # Neustart von vorn: alte Noten aus
        elif cmd == "clear":
            self.events, self.pending, self._early, self._play = [], [], [], []
            self._sounding.clear()
            self._skip.clear()
            self._idle()
            return running
        return False

    def _add(self, lst, t, kind, note, vel=0, nid=None):
        self._seq += 1
        lst.append((t, self._seq, kind, note, vel, self._seq if nid is None else nid))
        return self._seq

    def _close_held(self, lst, t):
        """Noch gedrueckte / vom Pedal gehaltene Noten bei t loslassen."""
        for n, (nid, _) in sorted({**self._held, **self._sustained}.items()):
            self._add(lst, t, "off", n, nid=nid)
        self._held.clear()
        self._sustained.clear()

    def _finish_overdub(self):
        self._close_held(self.pending, self.pos)
        self._merge()

    def _merge(self):
        if self.pending:
            self.events = sorted(self.events + self.pending)
            self.pending = []
            self._rebuild()
            self._idx = bisect.bisect_left(self._play, (self.pos,))

    def _qtime(self, t: float) -> float:
        return round(t / self.grid) * self.grid if self.grid else t

    def _rebuild(self):
        """Gespielte Fassung neu: Anschlaege aufs Raster, Loslassen um denselben Betrag."""
        L = self.length
        if not self.grid:
            self._play = list(self.events)
            return
        delta = {e[1]: self._qtime(e[0]) - e[0] for e in self.events if e[2] == "on"}
        play = []
        for e in self.events:
            t = e[0]
            if t < L:                               # hinter dem Ende (weniger Takte): bleibt stumm
                t += delta.get(e[5], 0.0)
                if t >= L:
                    t -= L
                elif t < 0:
                    t += L
            play.append((t,) + e[1:])
        play.sort()
        self._play = play

    # ------------------------------ Eingang ------------------------------
    def capture(self, kind: str, note: int, val):
        """Live gespieltes Ereignis (Audio-Thread, am Blockanfang)."""
        s = self.state
        if s == "countin":
            if self.pos < -COUNT_IN_EARLY:
                return                              # zu frueh: nur live spielen
            lst, t = self._early, 0.0
            shift = -self.pos                       # knapp vor der Eins -> auf die Eins
        elif s in ("recording", "overdub"):
            lst = self.events if s == "recording" else self.pending
            t, shift = self.pos, 0.0
        else:
            return
        L = self.length
        if kind == "on":
            self._sustained.pop(note, None)
            seq = self._add(lst, t, "on", note, val)
            self._held[note] = (seq, shift)
            if self.grid and self._qtime(t) >= L:   # rueckt auf den Anfang: dort gleich nicht
                self._skip.add(seq)                 # nochmal anschlagen (klingt schon live)
        elif kind == "off":
            if note not in self._held:
                return                              # vor der Aufnahme angeschlagen
            nid, shift = self._held.pop(note)
            if self._pedal:
                self._sustained[note] = (nid, shift)    # Pedal haelt: Note-Off kommt beim Loslassen
            else:
                self._add(lst, 0.0 if s == "countin" else (self.pos + shift) % L, "off", note, nid=nid)
        elif kind == "pedal":
            self._pedal = bool(val)
            if not val:
                for n, (nid, d) in sorted(self._sustained.items()):
                    self._add(lst, 0.0 if s == "countin" else (self.pos + d) % L, "off", n, nid=nid)
                self._sustained.clear()

    # ------------------------------ Abspielen ------------------------------
    def tick(self, n: int, bpm: float = 120.0, bars: int = 4, quantize: str = "aus",
             click: bool = False):
        """Einen Audio-Block (n Samples) weiter.

        Liefert (Ereignisse, Clicks):
          Ereignisse [(sample, kind, note, vel)]: Schleifen-Noten, sample = Versatz im Block
          Clicks [(sample, betont)]: Metronom-Schlaege (immer im Vorzaehler, sonst wenn click)
        """
        self.bpm = max(1.0, float(bpm))
        out: list[tuple] = []
        grid = QUANT_GRID.get(quantize, 0.0)
        if grid != self.grid:                       # Raster gedreht: ganze Schleife neu rechnen
            self.grid = grid
            self._rebuild()
            for note in sorted(self._sounding):     # sonst haengt eine Note, deren Ende verrutscht
                out.append((0, "off", note, 0))
            self._sounding.clear()
            if self.state != "recording":
                self._idx = bisect.bisect_left(self._play, (self.pos,))
        clicks: list[tuple] = []
        bars = max(1, min(MAX_BARS, int(bars)))
        s = self.state
        if bars != self.bars:
            self.bars = bars
            self._rebuild()
            if s in ("recording", "playing", "overdub") and self.pos >= self.length:
                p = self.pos % self.length          # weniger Takte: Position springt nach vorn
                self.pos = 0.0
                self._wrap(out, 0)
                self.pos = p
            if self.state != "recording":
                self._idx = bisect.bisect_left(self._play, (self.pos,))
            s = self.state
        rate = self.bpm / 60.0 / self.sr            # Schlaege pro Sample
        adv = n * rate
        if s == "countin":
            t0, t1 = self.pos, self.pos + adv
            self._clicks(t0, min(t1, 0.0), 0.0, rate, n, clicks)
            if t1 < 0.0:
                self.pos = t1
                return out, clicks
            done = -t0                              # bis zur Eins verbrauchte Schlaege
            self.events, self.pending = sorted(self._early), []
            self._early = []
            self._rebuild()
            self._sounding.clear()
            self._skip.clear()
            self._idx = len(self._play)             # Aufnahme: nichts abspielen
            self.pos = 0.0
            self._set("recording")
            self._run(adv - done, done, rate, n, False, click, out, clicks)
            return out, clicks
        if s not in ("recording", "playing", "overdub"):
            return out, clicks
        self._run(adv, 0.0, rate, n, s != "recording", click, out, clicks)
        return out, clicks

    def _clicks(self, t0, t1, off, rate, n, clicks):
        """Metronom-Schlaege (ganze Schlaege) im Bereich [t0, t1), off = Schlaege seit Blockanfang."""
        k = math.ceil(t0 - 0.5 * rate)              # halbes Sample Toleranz: Rundung am Blockrand
        while k < t1:
            o = round((off + k - t0) / rate)
            if o >= n:
                break                               # gehoert schon zum naechsten Block
            clicks.append((max(0, o), k % BEATS_PER_BAR == 0))
            k += 1

    def _run(self, remaining, off, rate, n, play, click, out, clicks):
        t = self.pos
        while True:
            L = self.length
            end = min(t + remaining, L)
            if click:
                self._clicks(t, end, off, rate, n, clicks)
            if play:
                ev = self._play
                while self._idx < len(ev) and ev[self._idx][0] < end:
                    et, seq, kind, note, vel, _ = ev[self._idx]
                    self._idx += 1
                    if seq in self._skip:
                        self._skip.discard(seq)
                        continue
                    out.append((min(n - 1, round((off + max(0.0, et - t)) / rate)), kind, note, vel))
                    if kind == "on":
                        self._sounding.add(note)
                    else:
                        self._sounding.discard(note)
            if t + remaining < L:
                self.pos = t + remaining
                return
            used = L - t
            off += used
            remaining -= used
            t = 0.0
            self.pos = 0.0
            self._wrap(out, min(n - 1, round(off / rate)))
            play = self.state != "recording"

    def _wrap(self, out, sample):
        """Schleifenende erreicht (pos steht schon am Anfang)."""
        L = self.length
        if self.state == "recording":               # erster Durchlauf fertig: ab jetzt abspielen
            self._close_held(self.events, L - 1e-6)
            self.events.sort()
            self._rebuild()
            self.state = "playing"
        self._merge()
        self._idx = 0
        # Noten, deren Loslassen hinter dem (gekuerzten) Ende liegt, sonst haengen sie
        for note in sorted(self._sounding):
            first = next((e for e in self._play if e[3] == note and e[0] < L), None)
            if first is None or first[2] == "on":
                out.append((sample, "off", note, 0))
                self._sounding.discard(note)
        self._set(self.state)                       # Web-UI: Position neu abgleichen
