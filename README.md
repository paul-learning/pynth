# Pynth – Raspberry-Pi-Synth mit Web-Reglern

Polyphoner Subtraktiv-Synth in Python (numpy/scipy), spielbar per MIDI-Keyboard
(USB oder BLE) und steuerbar über eine kleine Web-App, die der Pi selbst hostet.

## Design auf einen Blick

```
 MIDI-Keyboard ─┐                      ┌──────────── Web-UI (Browser, Handy) ───────────┐
 Knob-Controller┼─► midi.py ──notes──► │  Slider, Presets, MIDI-Learn, Test-Klaviatur   │
 ESP32 BLE  ────┘      │  CC            └───────▲───────────────────────────┬───────────┘
                       ▼                        │ SSE (Live-Updates)        │ POST
                 ParamStore (params.py)  ◄──────┴───────────────────────────┘
                 eine Liste aller Regler, IDs wie "filter.cutoff"
                       │ liest pro Block einen Schnappschuss
                       ▼
 engine.py:  8 Voices × (Osc1 + Osc2 + Noise white/pink) × ADSR × Velocity
             → Biquad-Tiefpass (Cutoff + LFO, 256er-Teilblöcke)
             → Master (gerampt) → Delay → Hall → tanh-Limiter → sounddevice/ALSA
```

* **Ein Parametermodell für alles.** Jeder Regler ist genau einmal in `pynth/params.py`
  definiert (Bereich, Kurve lin/exp, Raster, Einheit, Auswahlwerte). Web-UI, MIDI-CC,
  Presets und Engine nutzen dieselbe ID. MIDI-CC 0–127 und Slider arbeiten normalisiert
  0..1, die Umrechnung in Hz/Sekunden passiert nur dort. Neuer Regler = eine Zeile.
* **Engine**: alles vektorisiert, Phasen-Akkumulatoren statt `np.linspace` (Phase läuft
  über Blockgrenzen weiter), PolyBLEP gegen Aliasing bei Saw/Square, ADSR mit
  Bool-Masken, Voice-Stealing (gleiche Note → leiseste ausklingende → älteste),
  neue Noten starten ab aktuellem Pegel (kein Klick), Sustain-Pedal (CC 64).
* **Bereiche in der UI**: Oszillator 1, Oszillator 2, Noise (white/pink), Mix (Lautstärken
  Osc 1, Osc 2, Noise + Master), Hüllkurve, Filter, LFO, Effekte.
* **Feste Werte** (in `engine.py` oben, als Konstanten änderbar): 8 Stimmen,
  Pitchbend ±2 Halbtöne, Velocity-Empfindlichkeit 0,6.
* **Filter** ist global (paraphon): ein Filter auf die Summe aller Stimmen. Spart auf dem
  Pi viel CPU. Pro-Stimme-Filter + Filter-Hüllkurve ist der nächste Ausbauschritt.
* **Web-Stack**: nur Python-Standardbibliothek (`http.server` + Server-Sent Events),
  eine HTML-Datei mit Vanilla-JS. Nichts zu installieren, kaum RAM. Dreht man einen
  MIDI-Knopf, bewegt sich der Slider im Browser mit.
* **Presets**: JSON in `presets/`, verschachtelt nach Gruppe. Fehlende Werte → Default.
  Ältere Formate werden beim Laden übersetzt (Mistral-Format und Pynth 0.1, z.B.
  `osc1.level` → `mix.osc1`); auch alte `midi_map.json`-Einträge werden umgeschrieben.
  Der letzte Zustand wird alle 5 s in `state.json` gesichert und beim Start geladen.

## Installation auf dem Pi

```bash
# als Benutzer pynth
git clone https://github.com/paul-learning/pynth.git ~/pynth
python3 -m venv ~/pynth_venv                 # falls noch nicht vorhanden
~/pynth_venv/bin/pip install -r ~/pynth/requirements.txt
cd ~/pynth
~/pynth_venv/bin/python -m pynth --bench              # CPU-Last messen
~/pynth_venv/bin/python -m pynth --blocksize 1024     # starten
```

**Update:** `cd ~/pynth && git pull` und danach `sudo systemctl restart pynth`.
`midi_map.json` und `state.json` sind Nutzerdaten, stehen in `.gitignore` und bleiben
bei `git pull` unangetastet. Fehlen sie, startet Pynth mit leerem Mapping bzw. Defaults.

Dann im Browser: **http://pynth.local:8080** (oder http://192.168.178.148:8080).
Ohne Keyboard spielen: Klaviatur unten oder Computertastatur (A W S E D F T G Z H U J K O L).

Nützliche Optionen:

| Option | Zweck |
|---|---|
| `--blocksize 2048` | weniger Latenz (Standard 8192 = 186 ms) |
| `--device 1` | anderes Audio-Gerät, z.B. USB-Interface (`--list-devices`) |
| `--channels 2` | falls ein Interface kein Mono kann |
| `--preset "Warm Pad"` | Preset beim Start |
| `--no-audio` | nur UI testen, z.B. am Laptop |
| `--render demo.wav` | Demo ohne Soundkarte rendern |

Autostart: siehe `deploy/pynth.service` (Kommentar oben in der Datei, startet mit `--blocksize 1024`).

## Latenz

Die alten Underruns bei kleinen Blöcken kamen sehr wahrscheinlich vom langsamen Code
(`np.linspace`, Python-Logik pro Sample). Die neue Engine braucht für 8 Stimmen + FX auf
einem Laptop ~3 % CPU bei 8192 Samples. Auf dem Pi mit `--bench` prüfen: liegt die Last
bei 2048 unter ~50 %, lohnt sich `--blocksize 2048` (46 ms) oder sogar 1024 (23 ms).
Die Web-UI zeigt CPU-Last und Underruns live an.

## MIDI

* Alle Eingänge außer "Midi Through" werden automatisch geöffnet, auch später
  eingesteckte (Scan alle 3 s). BLE-MIDI vom ESP32 erscheint nach dem Pairing ebenso.
* **MIDI-Learn**: in der Web-UI auf **L** neben dem Regler klicken, dann Knopf am
  Controller drehen. Rechtsklick auf die Nummer löscht die Zuordnung.
  Gespeichert in `midi_map.json` als `{"<kanal>:<cc>": "<param-id>"}`.
* Fest: Noten, Pitchbend, CC 64 Sustain, CC 123 All Notes Off.

### Knob-Bänke (z.B. Samson Graphite M25: 8 Knobs, 4 Pads)

Die 4 Pads schalten die Bank um, jede Bank belegt die 8 Knobs neu, also bis zu 32 Regler.
Alle Bänke starten leer und werden selbst belegt (eine alte Vorbelegung in `controller.json`
wird beim Start automatisch geleert, eigene Änderungen bleiben):

* **Bank belegen**: Pad drücken (oder in der Web-UI die Bank wählen), dann **L** am Regler
  klicken und einen der 8 Knobs drehen. Der Regler landet auf diesem Knob in der aktiven Bank.
  Dreht man stattdessen einen Regler, der kein Bank-Knob ist (z.B. den großen Drehregler),
  gilt die Zuordnung in allen Bänken (z.B. Master-Lautstärke). Rechtsklick auf L entfernt.
  Alternativ im Kasten "Controller-Knobs" pro Knob einen Regler auswählen.
* **MIDI-Monitor**: unter "Controller einrichten" stehen die letzten 40 eingehenden Nachrichten
  (mit Kanal 1–16). Damit sieht man, was Pads und Tasten wirklich senden.
* **Einrichten**: Web-UI → "Controller einrichten" → **Einrichten**, dann Knob 1 bis 8 jeweils
  ein Stück drehen und Pad 1 bis 4 drücken. Funktioniert egal ob der Controller CCs, Noten
  oder Program Changes sendet. Solange die Pads nicht eingerichtet sind, spielen sie einfach Töne.
  Leicht zitternde Potis werden beim Anlernen ignoriert (ein Knob braucht ≥ 4 Schritte Bewegung,
  ein Pad als CC einen Sprung auf ≥ 64, schon belegte Knobs zählen nicht als Pad).
* **Belegung ändern**: im Kasten "Controller-Knobs" pro Knob einen Regler auswählen.
* **Knobs nach Bankwechsel** (umstellbar unter "Controller einrichten"):
  *sofort greifen* (Standard): der Knob setzt direkt seinen Wert, voller Weg 0–100 %, kann
  nach einem Bankwechsel springen. *Erst abholen* (Soft-Takeover): der Knob greift erst, wenn
  er den aktuellen Wert überstreicht ("○ abholen" → "● greift"), dadurch springt nichts.
* Ein Eingang ist immer nur einmal belegt: wird ein Knob als Controller-Knob gelernt, fällt
  seine direkte "L"-Zuordnung weg und umgekehrt.
* **Farben**: orange umrandet mit K1–K8 = Regler der aktiven Bank. Orange Zahl statt "L" =
  Regler direkt (ohne Bank) einem CC zugeordnet.
* **Mod-Rad** (CC 1) steuert standardmäßig die LFO-Tiefe, umstellbar in der Web-UI.
* Gespeichert in `controller.json` (Nutzerdaten, nicht im Repo).
* Eingehende MIDI-Nachrichten anzeigen: `~/pynth_venv/bin/python -m pynth --midi-monitor`
  (oder `aseqdump -l` / `aseqdump -p <port>`). Die Web-UI zeigt die letzte Nachricht ebenfalls.

## Dateien

```
pynth/params.py   Parametermodell + Live-Event-Verteilung
pynth/dsp.py      Oszillatoren, LFO, Biquad, Delay-/Comb-/Allpass-Linien, Hall
pynth/engine.py   Voices, ADSR, Noise, Signalkette
pynth/midi.py     MIDI-Eingang, Hotplug, CC-Mapping, Learn
pynth/controller.py Knob-Bänke, Pads, Soft-Takeover, Mod-Rad
pynth/web.py      HTTP-API + SSE
pynth/presets.py  Preset-Format (JSON)
web/index.html    Web-UI
presets/*.json    6 Start-Presets
```

## Ideen für später

Pro-Stimme-Filter mit Filter-Hüllkurve · Keytracking · zweiter LFO / Mod-Matrix · relative Encoder ·
Feedback an LED-Ring-Controller beim Preset-Wechsel · Mistral-Agent für Presets
(braucht nur `POST /api/param` bzw. ein Preset-JSON).
