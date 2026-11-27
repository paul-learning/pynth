"""Start:  python -m pynth            (Audio + MIDI + Web-UI auf Port 8080)

Weitere Modi:
  python -m pynth --list-devices     Audio-Geraete anzeigen
  python -m pynth --bench            CPU-Last pro Block messen (ohne Soundkarte)
  python -m pynth --render demo.wav  Kurze Demo als WAV rendern (ohne Soundkarte)
  python -m pynth --no-audio         Nur Web-UI + MIDI (zum UI-Testen am Laptop)
  python -m pynth --midi-monitor     Eingehende MIDI-Nachrichten anzeigen (Strg+C beendet)
"""
from __future__ import annotations

import argparse
import gc
import logging
import os
import signal
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

from . import fast
from .engine import Engine
from .params import ParamStore
from .presets import PresetStore, flatten, nest
from .storage import load_json, save_json

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("pynth")


def save_state(state_file, params):
    try:
        st = nest(params.values, params.preset_name)
        st["ranges"] = params.ranges()                 # z.B. eingestellter Looper-Tempo-Bereich
        save_json(state_file, st)
    except OSError as e:
        log.warning("Kann %s nicht schreiben: %s", state_file, e)


def parse_args():
    ap = argparse.ArgumentParser(prog="pynth", description="Raspberry-Pi-Synth")
    ap.add_argument("--samplerate", type=int, default=44100)
    ap.add_argument("--blocksize", type=int, default=512,
                    help="Samples pro Audio-Block (kleiner = weniger Latenz, mehr Underrun-Risiko)")
    ap.add_argument("--latency", default="high",
                    help="ALSA-Puffer: 'low', 'high' oder Sekunden (z.B. 0.03). Mehr = sicherer, aber traeger")
    ap.add_argument("--rt-prio", type=int, default=70,
                    help="Echtzeit-Prioritaet des Audio-Threads (SCHED_FIFO 1-95, 0 = aus)")
    ap.add_argument("--device", default=None, help="Audio-Geraet (Name oder Index, siehe --list-devices)")
    ap.add_argument("--channels", type=int, default=1, help="1 = mono, 2 = gleiches Signal auf beide Kanaele")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", type=Path, default=ROOT, help="Ordner mit presets/, midi_map.json, state.json")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--no-midi", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--midi-monitor", action="store_true", help="eingehende MIDI-Nachrichten anzeigen")
    ap.add_argument("--render", type=Path, default=None)
    ap.add_argument("--preset", default=None, help="Preset beim Start laden")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def import_sounddevice():
    os.environ.setdefault("SOUNDDEVICE_BACKEND", "alsa")   # wie im funktionierenden Setup
    import sounddevice as sd
    return sd


def midi_monitor():
    import mido
    names = [n for n in mido.get_input_names() if not n.startswith(("Midi Through", "RtMidi"))]
    if not names:
        print("Kein MIDI-Eingang gefunden.")
        return
    ports = [mido.open_input(n) for n in names]
    print("Lausche auf:", ", ".join(names), "- Strg+C beendet")
    try:
        for port, msg in mido.ports.multi_receive(ports, yield_ports=True):
            if msg.type != "clock":
                print(f"{port.name[:24]:24s}  Kanal {getattr(msg, 'channel', -1) + 1:2d}  {msg}")
    except KeyboardInterrupt:
        pass


def bench(args):
    params = ParamStore()
    params.set("fx.delay_mix", 0.3)
    params.set("fx.reverb_mix", 0.3)
    params.set("lfo.depth", 0.5)
    params.set("lfo.target", "filter")
    params.set("env.release", 10.0)            # Stimmen bleiben die ganze Messung ueber aktiv
    eng = Engine(params, args.samplerate)
    print(f"DSP-Kern: {'C (schnell)' if fast.lib else 'numpy (C-Kern nicht verfuegbar)'}")
    print("8 Stimmen, LFO aufs Filter, Delay + Hall. Last = Rechenzeit / Blockdauer, "
          "Spitze = langsamster Block (der entscheidet ueber Knackser)")
    for bs in (256, 512, 1024, 2048):
        for v in eng.voices:
            v.active = False
        for n in (48, 52, 55, 59, 62, 64, 67, 71):
            eng.note_on(n, 100)
        eng.render(bs)
        budget = bs / args.samplerate
        runs = max(50, int(2.0 / budget))      # ~2 s Audio pro Blockgroesse
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            eng.render(bs)
            times.append(time.perf_counter() - t0)
        times.sort()
        mean = sum(times) / runs
        print(f"  blocksize {bs:5d} ({budget * 1000:4.1f} ms): {mean / budget * 100:4.0f}% Last, "
              f"Spitze {times[-1] / budget * 100:4.0f}%, "
              f"99% der Bloecke unter {times[int(runs * 0.99) - 1] / budget * 100:4.0f}%")


def make_realtime(prio: int):
    """Audio-Thread auf SCHED_FIFO setzen, damit Web/MIDI/WLAN ihn nicht ausbremsen.
    Braucht LimitRTPRIO im systemd-Dienst oder die Gruppe audio mit rtprio."""
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(prio))   # 0 = dieser Thread
        log.info("Audio-Thread laeuft mit Echtzeit-Prioritaet %d", prio)
    except (PermissionError, OSError, AttributeError) as e:
        log.warning("Keine Echtzeit-Prioritaet fuer Audio (%s). Dienst-Datei neu kopieren, "
                    "siehe README 'Latenz'", e)


def render_demo(args, params):
    eng = Engine(params, args.samplerate)
    n, sr = args.blocksize, args.samplerate
    chunks = []
    script = [(0.0, "on", [57, 60, 64]), (1.5, "off", [57, 60, 64]),
              (2.0, "on", [53, 57, 60, 65]), (3.5, "off", [53, 57, 60, 65]),
              (4.0, "on", [45]), (4.4, "off", [45]), (4.5, "on", [57]), (4.9, "off", [57])]
    total, t = 7.0, 0.0
    while t < total:
        for at, kind, notes in script:
            if t <= at < t + n / sr:
                for x in notes:
                    eng.note_on(x, 100) if kind == "on" else eng.note_off(x)
        chunks.append(eng.render(n))
        t += n / sr
    audio = np.concatenate(chunks)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(args.render), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    print(f"{args.render}: {len(audio) / sr:.1f}s, Peak {np.max(np.abs(audio)):.2f}")


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if args.list_devices:
        print(import_sounddevice().query_devices())
        return
    if args.bench:
        return bench(args)
    if args.midi_monitor:
        return midi_monitor()

    data = args.data
    presets = PresetStore(data / "presets")
    params = ParamStore()
    state_file = data / "state.json"
    if args.preset:
        params.load_flat(presets.load(args.preset), args.preset)
    else:                                              # letzter Zustand vor dem Neustart
        st = load_json(state_file)
        if isinstance(st, dict):
            for pid, r in (st.pop("ranges", None) or {}).items():
                try:
                    params.set_range(pid, *r)
                except (TypeError, ValueError):
                    pass
            params.load_flat(flatten(st), st.get("name", "Init"))
    params.dirty = False

    if args.render:
        return render_demo(args, params)

    engine = Engine(params, args.samplerate)

    from .controller import Controller
    controller = Controller(params, data / "controller.json")
    controller.on_transport = engine.looper_cmd        # Record/Stop/Play -> Looper
    midi = None
    if not args.no_midi:
        from .midi import MidiManager
        midi = MidiManager(engine, params, data / "midi_map.json", controller)
        midi.start()

    audio_info = {"samplerate": args.samplerate, "blocksize": args.blocksize,
                  "latency_ms": round(args.blocksize / args.samplerate * 1000), "audio": "aus"}
    stream = None
    if not args.no_audio:
        sd = import_sounddevice()
        ch = args.channels
        rt_pending = [args.rt_prio > 0]

        def callback(outdata, frames, time_info, status):
            if rt_pending[0]:                  # einmalig im Audio-Thread selbst
                rt_pending[0] = False
                make_realtime(args.rt_prio)
            if status.output_underflow:
                engine.stats["underruns"] += 1
            try:
                block = engine.render(frames)
            except Exception:
                log.exception("Fehler im Audio-Callback")
                outdata.fill(0)
                return
            outdata[:] = block[:, None] if ch > 1 else block.reshape(-1, 1)

        device = int(args.device) if args.device and args.device.isdigit() else args.device
        try:
            latency = float(args.latency)
        except ValueError:
            latency = args.latency
        stream = sd.OutputStream(samplerate=args.samplerate, blocksize=args.blocksize, latency=latency,
                                 dtype="float32", channels=ch, device=device, callback=callback)
        stream.start()
        audio_info["audio"] = str(sd.query_devices(stream.device)["name"])
        audio_info["latency_ms"] = round(stream.latency * 1000)
        log.info("Audio laeuft: %s, %d Hz, Block %d, Ausgabe-Latenz %.0f ms, DSP-Kern %s",
                 audio_info["audio"], args.samplerate, args.blocksize, stream.latency * 1000,
                 "C" if fast.lib else "numpy")

    else:                                              # ohne Soundkarte: Engine trotzdem takten
        def dummy_audio():
            while True:
                engine.render(args.blocksize)
                time.sleep(args.blocksize / args.samplerate)
        threading.Thread(target=dummy_audio, name="dummy-audio", daemon=True).start()

    # Weniger Aussetzer im Audio-Thread: andere Threads geben den GIL frueher ab,
    # und der Garbage Collector muss die beim Start angelegten Objekte nie mehr pruefen.
    sys.setswitchinterval(0.001)
    gc.collect()
    gc.freeze()

    from .web import App, serve
    httpd = serve(App(params, engine, presets, midi, ROOT / "web", audio_info, controller), args.host, args.port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(5.0):                          # alle 5 s Zustand sichern, falls geaendert
        if params.dirty:
            params.dirty = False
            save_state(state_file, params)
    log.info("Beende ...")
    if params.dirty:
        save_state(state_file, params)
    if stream:
        stream.stop()
        stream.close()
    if midi:
        midi.stop()
    httpd.shutdown()


if __name__ == "__main__":
    main()
