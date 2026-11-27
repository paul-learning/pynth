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
    def __init__(self, params, engine, presets, midi, web_dir: Path, audio_info: dict):
        self.params, self.engine, self.presets, self.midi = params, engine, presets, midi
        self.web_dir = Path(web_dir)
        self.audio_info = audio_info

    def state(self) -> dict:
        return {
            "schema": self.params.schema(),
            "groups": GROUP_LABELS,
            "values": self.params.values,
            "preset": self.params.preset_name,
            "presets": self.presets.list(),
            "midi": self.midi.info() if self.midi else None,
            "status": self.status(),
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
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                self._json(app.state())
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
                    app.midi.learn(data.get("id"))
                    return self._json({"ok": True})
                if path == "/api/midi/unmap" and app.midi:
                    app.midi.unmap(data["id"])
                    return self._json({"ok": True})
                if path == "/api/panic":
                    app.engine.panic()
                    return self._json({"ok": True})
            except KeyError as e:
                return self._json({"error": f"unbekannt: {e}"}, 404)
            return self._json({"error": "not found"}, 404)

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="web", daemon=True).start()
    log.info("Web-UI: http://%s:%d", "pynth.local" if host in ("0.0.0.0", "") else host, port)
    return httpd
