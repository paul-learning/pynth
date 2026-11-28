"""Pi ausschalten / neu starten aus der Web-UI.

Der Dienst laeuft als User pynth ohne root-Rechte. Erlaubt sind per sudo genau diese
Befehle (deploy/pynth-system.sudoers, eingerichtet mit deploy/install-system.sh), sonst nichts.
Fehlt die Regel (z.B. am Laptop), meldet die API das nur.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

log = logging.getLogger("pynth.system")

SYSTEMCTL = "/usr/bin/systemctl"
COMMANDS = {
    "poweroff": [SYSTEMCTL, "--no-block", "poweroff"],
    "reboot": [SYSTEMCTL, "--no-block", "reboot"],
}
MESSAGES = {
    "poweroff": "Pi fährt herunter. Wenn die grüne LED aus ist (ca. 20 s), kannst du den Stecker ziehen.",
    "reboot": "Pi startet neu. Die Seite lädt von selbst neu, sobald Pynth wieder läuft (ca. 1 Minute).",
}


def _sudo(args, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)


class System:
    def __init__(self, broadcast, before_shutdown=None):
        """broadcast(event, data): Live-Meldung an alle Browser.
        before_shutdown(): Zustand sichern, Noten aus (vor Ausschalten/Neustart)."""
        self.broadcast = broadcast
        self.before_shutdown = before_shutdown or (lambda: None)
        self.busy = None
        self._lock = threading.Lock()
        self._allowed: dict[str, bool] = {}

    def allowed(self, cmd: str) -> bool:
        """Darf pynth den Befehl per sudo ausfuehren? (sudo -l prueft nur, fuehrt nichts aus)"""
        if not self._allowed.get(cmd):          # nur "ja" merken: Regel kann spaeter dazukommen
            try:
                self._allowed[cmd] = _sudo(["-l", *COMMANDS[cmd]], timeout=5).returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._allowed[cmd] = False
        return self._allowed[cmd]

    def status(self) -> dict:
        return {"setup": all(self.allowed(c) for c in COMMANDS), "busy": self.busy}

    def power(self, cmd: str) -> dict:
        """cmd = "poweroff" oder "reboot"."""
        if cmd not in COMMANDS:
            raise ValueError("unbekannter Befehl")
        if not self.allowed(cmd):
            return {"ok": False, "error": "Nicht eingerichtet. Einmal am Pi ausführen: "
                                          "sudo bash /home/pynth/pynth/deploy/install-system.sh"}
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "Läuft schon: " + self.busy}
            self.busy = cmd
        try:
            self.before_shutdown()
        except Exception:                       # Ausschalten trotzdem, Zustand ist ohnehin max. 5 s alt
            log.exception("Zustand vor dem %s nicht gesichert", cmd)
        time.sleep(0.3)                          # Release der Noten ausklingen lassen
        log.info("System: %s (aus der Web-UI)", cmd)
        try:
            r = _sudo(COMMANDS[cmd])
        except (OSError, subprocess.SubprocessError) as e:
            r = None
            err = str(e)
        if r is None or r.returncode != 0:
            err = err if r is None else (r.stderr or r.stdout).strip()[-300:] or f"Fehler {r.returncode}"
            log.warning("System: %s fehlgeschlagen: %s", cmd, err)
            self.busy = None
            return {"ok": False, "error": err}
        self.broadcast("system", {"state": cmd, "msg": MESSAGES[cmd]})
        return {"ok": True, "msg": MESSAGES[cmd]}
