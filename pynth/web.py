"""Mini-Webserver nur mit der Python-Standardbibliothek (keine Extra-Pakete).

GET  /                 -> Web-UI
GET  /api/state        -> Schema, Werte, Presets, MIDI-Status
GET  /api/events       -> Server-Sent Events (Live-Updates, z.B. wenn ein MIDI-Knopf dreht)
POST /api/param        {"id": "filter.cutoff", "value": 1200}  oder  {"id":..., "norm": 0.5}
POST /api/note         {"note": 60, "velocity": 100, "on": true}
POST /api/preset/load  {"name": "Warm Pad"}
POST /api/preset/save  {"name": "Mein Sound"}
POST /api/preset/delete{"name": "Mein Sound"}
POST /api/midi/learn   {"id": "filter.cutoff"}   (id null = abbrechen)
POST /api/midi/unmap   {"id": "filter.cutoff"}
POST /api/panic
POST /api/range        {"id": "looper.speed", "min": 0.25, "max": 5}   Reglerbereich (nur wo erlaubt)
POST /api/looper       {"cmd": "record"}                   record | stop | play | clear
POST /api/ctrl/bank    {"bank": 0}                         aktive Knob-Bank (0-3)
POST /api/ctrl/learn   {"slot": "knob", "index": 0, "wizard": true}   (slot null = abbrechen)
POST /api/ctrl/clear   {"slot": "pad", "index": 2}
POST /api/ctrl/assign  {"bank": 1, "knob": 7, "id": "lfo.rate"}   (id null = leer)
POST /api/ctrl/bankname{"bank": 1, "name": "Filter"}   (leer = kein Name)
POST /api/ctrl/modwheel{"id": "lfo.depth"}
POST /api/ctrl/takeover{"mode": "jump"}                    "jump" oder "pickup" (Soft-Takeover)
GET  /api/wifi        WLAN-Status (Modus client/hotspot, Verbindung, IP)
POST /api/wifi/scan                                       WLANs suchen
POST /api/wifi/connect{"ssid": "MeinWLAN", "password": "..."}  verbindet im Hintergrund
POST /api/wifi/hotspot                                    Hotspot jetzt starten
POST /api/wifi/known                                      Hotspot aus, bekanntes WLAN suchen (sonst Hotspot wieder an)
POST /api/wifi/rescan                                     WLAN-Liste neu (im Hotspot ca. 20 s weg)
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .params import GROUP_LABELS

log = logging.getLogger("pynth.web")


class App:
    def __init__(self, params, engine, presets, midi, web_dir: Path, audio_info: dict, controller=None):
        self.params, self.engine, self.presets, self.midi = params, engine, presets, midi
        self.controller = controller
        self.web_dir = Path(web_dir)
        self.audio_info = audio_info
        from .wifi import Wifi
        self.wifi = Wifi(on_change=lambda st: params.broadcast("wifi", st))

    def state(self) -> dict:
        return {
            "schema": self.params.schema(),
            "groups": GROUP_LABELS,
            "values": self.params.values,
            "preset": self.params.preset_name,
            "presets": self.presets.list(),
            "midi": self.midi.info() if self.midi else None,
            "controller": self.controller.info() if self.controller else None,
            "status": self.status(),
            "looper": self.engine.looper.info(),
        }

    def status(self) -> dict:
        s = dict(self.engine.stats)
        s.update(self.audio_info)
        return s


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _json(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = (app.web_dir / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")      # nach Update immer die neue UI
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                self._json(app.state())
            elif self.path == "/api/wifi":
                self._json(app.wifi.status())
            elif self.path == "/api/events":
                self._sse()
            else:
                self._json({"error": "not found"}, 404)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = app.params.subscribe()
            try:
                while True:
                    try:
                        event, data = q.get(timeout=1.0)
                    except queue.Empty:
                        event, data = "status", app.status()
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.params.unsubscribe(q)
                self.close_connection = True

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            p, path = app.params, self.path
            try:
                if path == "/api/param":
                    if "norm" in data:
                        ok = p.set_norm(data["id"], data["norm"], source="web")
                    else:
                        ok = p.set(data["id"], data["value"], source="web")
                    return self._json({"ok": ok}, 200 if ok else 404)
                if path == "/api/note":
                    if data.get("on", True):
                        app.engine.note_on(int(data["note"]), int(data.get("velocity", 100)))
                    else:
                        app.engine.note_off(int(data["note"]))
                    return self._json({"ok": True})
                if path == "/api/preset/load":
                    p.load_flat(app.presets.load(data["name"]), data["name"])
                    return self._json({"ok": True})
                if path == "/api/preset/save":
                    name = str(data["name"]).strip()[:60] or "Unbenannt"
                    app.presets.save(name, p.values)
                    p.preset_name = name
                    p.broadcast("presets", {"presets": app.presets.list(), "preset": name})
                    return self._json({"ok": True})
                if path == "/api/preset/delete":
                    ok = app.presets.delete(data["name"])
                    p.broadcast("presets", {"presets": app.presets.list(), "preset": p.preset_name})
                    return self._json({"ok": ok})
                if path == "/api/midi/learn" and app.midi:
                    if app.controller and data.get("id"):
                        app.controller.learn(None)
                    app.midi.learn(data.get("id"))
                    return self._json({"ok": True})
                if path == "/api/midi/unmap" and app.midi:
                    app.midi.unmap(data["id"])
                    return self._json({"ok": True})
                c = app.controller
                if path == "/api/ctrl/bank" and c:
                    c.set_bank(int(data["bank"]))
                    return self._json({"ok": True})
                if path == "/api/ctrl/learn" and c:
                    if app.midi:
                        app.midi.learn(None)
                    c.learn(data.get("slot"), int(data.get("index", 0)), bool(data.get("wizard")))
                    return self._json({"ok": True})
                if path == "/api/ctrl/clear" and c:
                    c.clear(data["slot"], int(data["index"]))
                    return self._json({"ok": True})
                if path == "/api/ctrl/assign" and c:
                    c.assign(int(data["bank"]), int(data["knob"]), data.get("id") or None)
                    return self._json({"ok": True})
                if path == "/api/ctrl/bankname" and c:
                    c.set_bank_name(int(data["bank"]), str(data.get("name") or ""))
                    return self._json({"ok": True})
                if path == "/api/ctrl/modwheel" and c:
                    c.set_modwheel(data.get("id") or None)
                    return self._json({"ok": True})
                if path == "/api/ctrl/takeover" and c:
                    c.set_takeover(str(data.get("mode")))
                    return self._json({"ok": True})
                if path == "/api/wifi/scan":
                    return self._json(app.wifi.scan())
                if path == "/api/wifi/connect":
                    ok = app.wifi.connect(data["ssid"], data.get("password", ""))
                    return self._json({"ok": ok, "error": None if ok else "WLAN ist gerade beschaeftigt"})
                if path == "/api/wifi/hotspot":
                    return self._json({"ok": app.wifi.hotspot()})
                if path == "/api/wifi/known":
                    return self._json({"ok": app.wifi.known()})
                if path == "/api/wifi/rescan":
                    return self._json({"ok": app.wifi.rescan()})
                if path == "/api/looper":
                    cmd = str(data.get("cmd"))
                    if cmd not in ("record", "stop", "play", "clear"):
                        return self._json({"error": "cmd"}, 400)
                    app.engine.looper_cmd(cmd)
                    return self._json({"ok": True})
                if path == "/api/range":
                    ok = p.set_range(data["id"], data["min"], data["max"])
                    return self._json({"ok": ok, "param": p.schema_of(data["id"])}, 200 if ok else 400)
                if path == "/api/panic":
                    app.engine.panic()
                    return self._json({"ok": True})
            except KeyError as e:
                return self._json({"error": f"unbekannt: {e}"}, 404)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"error": "not found"}, 404)

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="web", daemon=True).start()
    log.info("Web-UI: http://%s:%d", "pynth.local" if host in ("0.0.0.0", "") else host, port)
    return httpd
