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
  Velocity-Empfindlichkeit 0,6.
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
~/pynth_venv/bin/python -m pynth                     # starten (Block 512)
```

**Update:** `cd ~/pynth && git pull` und danach `sudo systemctl restart pynth`.
Nutzerdaten werden stromausfallsicher gespeichert (erst in eine Temp-Datei, dann atomar
ersetzt, vorige Fassung als `*.bak`). Ist eine Datei nach einem Stromausfall kaputt, lädt
Pynth automatisch die `.bak` und hebt die kaputte als `*.broken` auf.
Zusätzlich landet jede geänderte Fassung von `controller.json` und `midi_map.json` mit
Zeitstempel in `backups/` (die letzten 30). Zurückholen: Pynth stoppen, gewünschte Fassung
über die Datei kopieren, Pynth starten. Jede Änderung an den Bänken steht im Log
(`journalctl -u pynth | grep Bank`).
`midi_map.json` und `state.json` sind Nutzerdaten, stehen in `.gitignore` und bleiben
bei `git pull` unangetastet. Fehlen sie, startet Pynth mit leerem Mapping bzw. Defaults.

Dann im Browser: **http://pynth.local:8080** (oder http://192.168.178.148:8080).
Ohne Keyboard spielen: Klaviatur unten oder Computertastatur (A W S E D F T G Z H U J K O L).

Nützliche Optionen:

| Option | Zweck |
|---|---|
| `--blocksize 1024` | Samples pro Block (Standard 512 = 12 ms) |
| `--latency low` | ALSA-Puffer kleiner (`low`, `high` = Standard, oder Sekunden) |
| `--rt-prio 0` | Echtzeit-Priorität des Audio-Threads aus (Standard 70) |
| `--device 1` | anderes Audio-Gerät, z.B. USB-Interface (`--list-devices`) |
| `--channels 2` | falls ein Interface kein Mono kann |
| `--preset "Warm Pad"` | Preset beim Start |
| `--no-audio` | nur UI testen, z.B. am Laptop |
| `--render demo.wav` | Demo ohne Soundkarte rendern |

Autostart: siehe `deploy/pynth.service` (Kommentar oben in der Datei, startet mit `--blocksize 512`).

## Latenz

Die Schleifen pro Sample (Oszillatoren, Hüllkurven, Filter, Delay, Hall) laufen in einem
kleinen C-Kern (`pynth/_kernel.c`). Pynth übersetzt ihn beim ersten Start selbst mit `cc`
nach `pynth/_build/` (dauert ein paar Sekunden). Fehlt der Compiler (`sudo apt install gcc`),
rechnet die Engine wie früher mit numpy, klingt gleich, braucht aber ca. 3× so viel CPU.
Vergleichen: `PYNTH_NO_C=1 python -m pynth --bench`.

Das Filter ist im C-Kern ein State-Variable-Filter mit Cutoff pro Sample: gleicher Klang
wie der Biquad, aber kein Knacksen mehr bei hoher LFO-Tiefe. Square- und Saw-LFO werden an
den Sprüngen ~2 ms geglättet.

Gegen Aussetzer zusätzlich:
* Der Audio-Thread setzt sich selbst auf Echtzeit-Priorität (SCHED_FIFO 70, braucht
  `LimitRTPRIO` im Dienst), der Python-GIL wird alle 1 ms abgegeben, der Garbage Collector
  prüft die Start-Objekte nicht mehr.
* `deploy/pynth.service` stellt den CPU-Takt beim Start auf `performance`.

`--bench` zeigt pro Blockgröße die mittlere Last und den langsamsten Block ("Spitze").
Die Spitze sollte deutlich unter 100 % bleiben. Die Web-UI zeigt CPU-Last, Underruns
und die echte Ausgabe-Latenz (Block + ALSA-Puffer) live an.

## MIDI

* Alle Eingänge außer "Midi Through" werden automatisch geöffnet, auch später
  eingesteckte (Scan alle 3 s). BLE-MIDI vom ESP32 erscheint nach dem Pairing ebenso.
* **MIDI-Learn**: in der Web-UI auf **L** neben dem Regler klicken, dann Knopf am
  Controller drehen. Rechtsklick auf die Nummer löscht die Zuordnung.
  Gespeichert in `midi_map.json` als `{"<kanal>:<cc>": "<param-id>"}`.
* Fest: Noten, Pitchbend, CC 64 Sustain, CC 123 All Notes Off.
* **Pitchbend**: Bereich in der Web-UI unter Oszillator 2 ("Pitchbend", 0–12 Halbtöne,
  Standard ±2). Der Bend gleitet pro Sample, auch bei großen Blöcken ohne Stufen.

### Knob-Bänke (z.B. Samson Graphite M25: 8 Knobs, 4 Pads)

Die 4 Pads schalten die Bank um, jede Bank belegt die 8 Knobs neu, also bis zu 32 Regler.
Alle Bänke starten leer und werden selbst belegt:

* **Bank belegen**: Pad drücken (oder in der Web-UI die Bank wählen), dann **L** am Regler
  klicken und einen der 8 Knobs drehen. Der Regler landet auf diesem Knob in der aktiven Bank.
  Dreht man stattdessen einen Regler, der kein Bank-Knob ist (z.B. den großen Drehregler),
  gilt die Zuordnung in allen Bänken (z.B. Master-Lautstärke). Rechtsklick auf L entfernt.
  Alternativ im Kasten "Controller-Knobs" pro Knob einen Regler auswählen.
* **Weiter-Taste**: jeder Druck schaltet zur nächsten Bank (1 → 2 → 3 → 4 → 1). Standard sind
  Note 0 und CC 0 auf jedem Kanal, weil beim Graphite M25 alle vier Pads dasselbe senden
  (Note 0, im CC-Modus CC 0). Jeder Druck zählt einmal, egal ob das Pad beim Loslassen
  "aus" sendet oder pro Druck zwischen an/aus wechselt (Toggle). CC 0 direkt nach einem
  Program Change (PRESET-Taste am M25) wird ignoriert. Unter "Controller einrichten"
  anlernbar, Rechtsklick schaltet sie ab. Fest angelernte Pads/Knobs haben Vorrang.
  Stop/Play/Record steuern den Looper (siehe unten).
* **Controller-Presets**: Viele Controller (auch der M25) senden je nach Preset andere CCs/Noten.
  Die Knobs/Pads gelten für das Preset, auf dem sie angelernt wurden. Nach einem Preset-Wechsel
  am Controller "Einrichten" neu durchlaufen. Dreht man bei "L" einen Knob, der kein Bank-Knob
  ist, zeigt die Web-UI einen Hinweis: die Zuordnung gilt dann in allen Bänken.
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
* **Live-Ansicht** (Knopf „Live-Ansicht“ oben in der Web-UI): zeigt nur die 8 Knobs der aktiven
  Bank, groß fürs Tablet, mit Regler-Name und aktuellem Wert. Wechselt mit, wenn man per Pad die
  Bank umschaltet. Freie Knobs sind gestrichelt. „Detail-Ansicht“ führt zurück; der Browser merkt
  sich die zuletzt gewählte Ansicht.
  Mit **„✋ Touch an“** lassen sich die Werte auch am Tablet ändern: Kachel hoch/runter wischen,
  bei Auswahl-Reglern (z.B. Wellenform) die Option antippen, Doppeltipp = Standardwert. Standard ist
  „Touch aus“, damit beim Spielen nichts aus Versehen verstellt wird.
* **Bänke benennen**: unter den Bank-Tasten in „Controller-Knobs“ (z.B. „Filter“, „FX“); der Name
  steht dann in der Bank-Anzeige und groß in der Live-Ansicht.
* **Mod-Rad** (CC 1) steuert standardmäßig die LFO-Tiefe, umstellbar in der Web-UI.
* Gespeichert in `controller.json` (Nutzerdaten, nicht im Repo).
* Eingehende MIDI-Nachrichten anzeigen: `~/pynth_venv/bin/python -m pynth --midi-monitor`
  (oder `aseqdump -l` / `aseqdump -p <port>`). Die Web-UI zeigt die letzte Nachricht ebenfalls.

## WLAN: Hotspot und WLAN-Wechsel

Damit die Web-UI auch ohne Router/Internet erreichbar ist:

* **Bekanntes WLAN in Reichweite** → der Pi verbindet sich wie gewohnt, UI unter **http://pynth.local:8080**.
* **Kein bekanntes WLAN** (ca. 60 s nach dem Start oder nach Verbindungsverlust) → der Pi macht den
  Hotspot **„pynth“** auf (Passwort `pynthpynth`, änderbar in `/etc/default/pynth-wifi`).
  Handy damit verbinden, dann **http://pynth.home** (oder http://10.42.0.1). Port 80 wird auf 8080
  weitergeleitet, `:8080` braucht man also nicht mehr (geht aber weiter).
* **Im Hotspot**: alle 5 Minuten, wenn gerade kein Gerät verbunden ist, schaut der Pi kurz,
  ob ein bekanntes WLAN wieder da ist, und wechselt dorthin.
* **Im Hotspot, Kasten „WLAN“**: „Ins bekannte WLAN wechseln“ schaltet den Hotspot aus und verbindet ein
  gespeichertes WLAN in Reichweite (sonst ist der Hotspot nach ca. 1 Minute wieder da). Gespeicherte WLANs
  sind in der Liste markiert: antippen, Passwort leer lassen, „Verbinden“. „Neu suchen“ schaltet den Hotspot
  ca. 20 s aus, um die Liste zu aktualisieren.
* **Web-UI → Kasten „WLAN“**: WLANs suchen, auswählen, Passwort eingeben, verbinden. Neue Netze
  werden gespeichert. Klappt die Verbindung nicht, wird ein falsches Passwort zurückgenommen und
  der Pi geht zurück ins vorige WLAN bzw. in den Hotspot. Im Hotspot zeigt die Liste den letzten Scan
  von vorher (der WLAN-Chip kann nicht gleichzeitig Hotspot sein und suchen), ein Netz kann man
  auch per Namen eintippen.
* Hotspot und WLAN-Client gleichzeitig kann der Onboard-Chip des Pi nicht zuverlässig, deshalb
  wird umgeschaltet. Mit einem zweiten USB-WLAN-Stick ginge beides parallel.

Technik: `deploy/pynth-wifi` (läuft als root) mit NetworkManager/`nmcli`. Als Dienst
`pynth-wifi.service` überwacht es das WLAN. Die Web-UI (Benutzer `pynth`) darf per sudo nur
`pynth-wifi scan|connect|hotspot` aufrufen (`/etc/sudoers.d/pynth-wifi`). Das Skript liegt
nach der Installation in `/usr/local/sbin`, also nicht im Repo-Ordner, den `pynth` ändern kann.

**Einmalig einrichten** (als `pi`):

```bash
sudo -u pynth git -C /home/pynth/pynth pull
sudo bash /home/pynth/pynth/deploy/install-wifi.sh
sudo systemctl restart pynth
```

Das installiert avahi (pynth.local) und iw, setzt den Hostnamen auf `pynth`, legt das
Hotspot-Profil an und startet den Wächter. Das Heim-WLAN bleibt unverändert. Ändert ein späteres
Update `deploy/pynth-wifi`, das Installationsskript einfach erneut ausführen.

**Wenn etwas schiefgeht:**

* Logs: `journalctl -u pynth-wifi -f`
* Wächter aus (Hotspot startet nicht mehr): `sudo systemctl disable --now pynth-wifi`
* Hotspot sofort beenden: `sudo nmcli con down pynth-hotspot`
* Alles entfernen: `sudo bash /home/pynth/pynth/deploy/install-wifi.sh --uninstall`
* Kein Zugang per WLAN mehr: per Hotspot „pynth“ verbinden und `ssh pi@10.42.0.1`, oder LAN-Kabel,
  oder Tastatur/Bildschirm am Pi. Gespeicherte WLANs: `nmcli con show`, WLAN neu verbinden:
  `sudo nmcli dev wifi connect "<SSID>" password "<Passwort>"`.

## Looper (Record / Stop / Play)

Der Looper nimmt auf, **was** du spielst (Noten, Anschlag, Sustain-Pedal), nicht den Klang.
Beim Abspielen laufen die Noten wieder durch die Engine, daher wirken alle Regler live auf
die Schleife (Filter, Hüllkurve, LFO, Effekte …). Kostet praktisch keine CPU.

| Taste | leer / gestoppt | Aufnahme | Wiedergabe | Overdub |
|---|---|---|---|---|
| **Record** | scharf, Aufnahme startet mit der ersten Note (alte Schleife wird ersetzt) | fertig + sofort abspielen | Overdub: dazuspielen | Overdub fertig |
| **Stop** | – | fertig (Schleife bleibt) | aus | aus |
| **Play** | Schleife von vorn | fertig + sofort abspielen | von vorn | Overdub fertig |

* Die Schleife ist so lang wie von der ersten Note bis zum Druck auf Stop/Play/Record,
  höchstens 60 s (`MAX_SECONDS` in `pynth/looper.py`), danach stoppt die Aufnahme selbst.
* Live gespielte und geloopte Noten haben getrennte Stimmen, gleiche Töne stören sich nicht.
  Alle teilen sich die 8 Stimmen.
* **Tasten am Controller**: Web-UI → Looper → "Tasten am Controller anlernen": Feld anklicken,
  Taste drücken. Eine Taste, die vorher als Pad die Bank umgeschaltet hat, wird dabei als Pad
  entfernt. Tasten, die MIDI Machine Control (MMC-Sysex) senden, funktionieren ohne Anlernen.
* Panic stoppt auch die Wiedergabe. Die Schleife wird nicht gespeichert (weg nach Neustart).

## Dateien

```
pynth/params.py   Parametermodell + Live-Event-Verteilung
pynth/dsp.py      Oszillatoren, LFO, Biquad, Delay-/Comb-/Allpass-Linien, Hall
pynth/engine.py   Voices, ADSR, Noise, Signalkette
pynth/midi.py     MIDI-Eingang, Hotplug, CC-Mapping, Learn
pynth/controller.py Knob-Bänke, Pads, Soft-Takeover, Mod-Rad, Looper-Tasten
pynth/looper.py   MIDI-Looper (Aufnahme, Wiedergabe, Overdub)
pynth/web.py      HTTP-API + SSE
pynth/wifi.py     WLAN-Status, Scan, Verbinden (für die Web-UI)
deploy/pynth-wifi Fallback-Hotspot + WLAN-Wechsel (root), install-wifi.sh richtet alles ein
pynth/presets.py  Preset-Format (JSON)
web/index.html    Web-UI
presets/*.json    6 Start-Presets
```

## Ideen für später

Pro-Stimme-Filter mit Filter-Hüllkurve · Keytracking · zweiter LFO / Mod-Matrix · relative Encoder ·
Feedback an LED-Ring-Controller beim Preset-Wechsel · Mistral-Agent für Presets
(braucht nur `POST /api/param` bzw. ein Preset-JSON).
