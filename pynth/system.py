"""Pi ausschalten / neu starten und Pynth aktualisieren (git pull + Neustart) aus der Web-UI.

Der Dienst laeuft als User pynth ohne root-Rechte. Erlaubt sind per sudo genau diese
Befehle (deploy/pynth-system.sudoers, eingerichtet mit deploy/install-system.sh), sonst nichts.
Fehlt die Regel (z.B. am Laptop), meldet die API das nur.

Das Update braucht kein root: das Repo gehoert dem User pynth. Weil die Web-UI keine
Anmeldung hat, ist es durch eine kurze PIN geschuetzt (nicht das System-Passwort, das wuerde
unverschluesselt uebers WLAN gehen). Gesetzt wird sie am Pi mit `python -m pynth --set-pin`,
gespeichert nur als Hash in pin.json. Nach 5 Fehlversuchen ist 5 Minuten Pause.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from .storage import load_json, save_json

log = logging.getLogger("pynth.system")

SYSTEMCTL = "/usr/bin/systemctl"
COMMANDS = {
    "poweroff": [SYSTEMCTL, "--no-block", "poweroff"],
    "reboot": [SYSTEMCTL, "--no-block", "reboot"],
}
RESTART = [SYSTEMCTL, "--no-block", "restart", "pynth.service"]
BRANCH = "main"
PIN_ITER = 100_000
MAX_FAILS, LOCK_S = 5, 300
MESSAGES = {
    "poweroff": "Pi fährt herunter. Wenn die grüne LED aus ist (ca. 20 s), kannst du den Stecker ziehen.",
    "reboot": "Pi startet neu. Die Seite lädt von selbst neu, sobald Pynth wieder läuft (ca. 1 Minute).",
}


def _sudo(args, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)


def _hash(pin: str, salt: bytes, iterations: int = PIN_ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations).hex()


def set_pin(pin_file: Path) -> int:
    """Kommandozeile: PIN fuer das Update aus der Web-UI setzen (python -m pynth --set-pin)."""
    print("PIN fuer 'Update' in der Web-UI (4 bis 32 Zeichen, z.B. 4-6 Ziffern).")
    pin = getpass.getpass("Neue PIN: ")
    if not 4 <= len(pin) <= 32:
        print("Die PIN muss 4 bis 32 Zeichen haben. Nichts geaendert.")
        return 1
    if getpass.getpass("PIN wiederholen: ") != pin:
        print("Die beiden Eingaben sind verschieden. Nichts geaendert.")
        return 1
    salt = secrets.token_bytes(16)
    save_json(pin_file, {"salt": salt.hex(), "iter": PIN_ITER, "hash": _hash(pin, salt)}, backup=False)
    try:
        os.chmod(pin_file, 0o600)
    except OSError:
        pass
    print(f"PIN gespeichert in {pin_file}")
    return 0


class System:
    def __init__(self, broadcast, before_shutdown=None, repo: Path | None = None, pin_file: Path | None = None):
        """broadcast(event, data): Live-Meldung an alle Browser.
        before_shutdown(): Zustand sichern, Noten aus (vor Ausschalten/Neustart).
        repo: Git-Checkout von Pynth, pin_file: pin.json (PIN-Hash fuer das Update)."""
        self.broadcast = broadcast
        self.before_shutdown = before_shutdown or (lambda: None)
        self.repo = Path(repo) if repo else None
        self.pin_file = Path(pin_file) if pin_file else None
        self.busy = None
        self._lock = threading.Lock()
        self._allowed: dict[str, bool] = {}
        self._fails = 0
        self._locked_until = 0.0
        self.update_log: list[str] = []

    def allowed(self, cmd: str) -> bool:
        """Darf pynth den Befehl per sudo ausfuehren? (sudo -l prueft nur, fuehrt nichts aus)"""
        if not self._allowed.get(cmd):          # nur "ja" merken: Regel kann spaeter dazukommen
            args = RESTART if cmd == "restart" else COMMANDS[cmd]
            try:
                self._allowed[cmd] = _sudo(["-l", *args], timeout=5).returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._allowed[cmd] = False
        return self._allowed[cmd]

    def status(self) -> dict:
        return {"setup": all(self.allowed(c) for c in COMMANDS), "busy": self.busy,
                "update_setup": self._update_ready(), "update_log": self.update_log}

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

    # --- Update: git pull + Neustart -------------------------------------------------

    def _pin(self) -> dict | None:
        d = load_json(self.pin_file) if self.pin_file and self.pin_file.exists() else None
        return d if isinstance(d, dict) and d.get("hash") and d.get("salt") else None

    def _update_ready(self) -> dict:
        """Was fehlt noch fuers Update? (fuer den Hinweis in der Web-UI)"""
        return {"pin": self._pin() is not None, "restart": self.allowed("restart"),
                "git": bool(self.repo and (self.repo / ".git").exists())}

    def check_pin(self, pin) -> str | None:
        """None = PIN stimmt, sonst Fehlermeldung."""
        now = time.monotonic()
        if now < self._locked_until:
            return f"Zu viele Fehlversuche, bitte {int(self._locked_until - now) + 1} s warten"
        stored = self._pin()
        if stored is None:
            return ("Keine PIN gesetzt. Einmal am Pi ausführen: "
                    "sudo bash /home/pynth/pynth/deploy/install-system.sh")
        try:
            ok = hmac.compare_digest(_hash(str(pin or ""), bytes.fromhex(stored["salt"]),
                                           int(stored.get("iter", PIN_ITER))), stored["hash"])
        except (ValueError, TypeError):
            ok = False
        if ok:
            self._fails = 0
            return None
        self._fails += 1
        log.warning("Update: falsche PIN (%d. Versuch)", self._fails)
        if self._fails >= MAX_FAILS:
            self._fails = 0
            self._locked_until = now + LOCK_S
            return f"Falsche PIN. Zu viele Fehlversuche, Update für {LOCK_S // 60} Minuten gesperrt"
        return "Falsche PIN"

    def update(self, pin) -> dict:
        """PIN pruefen, dann im Hintergrund: git pull, C-Kern vorab uebersetzen, Pynth neu starten.
        Fortschritt kommt als Live-Meldung "update" an alle Browser."""
        if not (self.repo and (self.repo / ".git").exists()):
            return {"ok": False, "error": "Kein Git-Checkout gefunden"}
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "Läuft schon: " + self.busy}
            err = self.check_pin(pin)
            if err:
                return {"ok": False, "error": err}
            if not self.allowed("restart"):
                return {"ok": False, "error": "Neustart nicht eingerichtet. Einmal am Pi ausführen: "
                                              "sudo bash /home/pynth/pynth/deploy/install-system.sh"}
            self.busy = "update"
        self.update_log = []
        threading.Thread(target=self._update_worker, name="update", daemon=True).start()
        return {"ok": True}

    def _say(self, msg: str, state: str = "running"):
        log.info("Update: %s", msg)
        self.update_log.append(msg)
        self.broadcast("update", {"state": state, "msg": msg, "log": self.update_log})

    def _git(self, *args, timeout=30) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              timeout=timeout, env=env)

    def _update_worker(self):
        try:
            self._do_update()
        except Exception as e:                  # nie mit busy haengen bleiben
            log.exception("Update fehlgeschlagen")
            self._say(f"Fehler: {e}", "error")
        finally:
            self.busy = None

    def _do_update(self):
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != BRANCH:
            return self._say(f"Der Pi ist auf Branch „{branch}“, nicht „{BRANCH}“. Bitte per SSH wechseln.",
                             "error")
        old = self._git("rev-parse", "HEAD").stdout.strip()
        self._say("Hole Änderungen von GitHub ...")
        try:
            r = self._git("pull", "--ff-only", "origin", BRANCH, timeout=120)
        except subprocess.TimeoutExpired:
            return self._say("Keine Antwort von GitHub (Internet?). Nichts geändert.", "error")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().splitlines()
            return self._say("git pull fehlgeschlagen, nichts neu gestartet:\n" + "\n".join(msg[-6:]), "error")
        new = self._git("rev-parse", "HEAD").stdout.strip()
        if new == old:
            return self._say("Schon aktuell, kein Neustart nötig.", "done")
        commits = self._git("log", "--oneline", "--no-decorate", f"{old}..{new}").stdout.strip().splitlines()
        self._say(f"{len(commits)} neue Änderung(en):\n" + "\n".join(commits[:15])
                  + ("\n..." if len(commits) > 15 else ""))
        changed = set(self._git("diff", "--name-only", old, new).stdout.split())
        notes = []
        if changed & {"deploy/pynth.service", "deploy/pynth-system.sudoers"}:
            notes.append("Dienst-Datei/sudo-Regel geändert: per SSH "
                         "sudo bash /home/pynth/pynth/deploy/install-system.sh ausführen.")
        if "requirements.txt" in changed:
            notes.append("requirements.txt geändert: per SSH ~/pynth_venv/bin/pip install -r ~/pynth/requirements.txt")
        if any(f.startswith("deploy/pynth-wifi") or f == "deploy/install-wifi.sh" for f in changed):
            notes.append("WLAN-Skript geändert: per SSH sudo bash /home/pynth/pynth/deploy/install-wifi.sh")
        for n in notes:
            self._say("Hinweis: " + n)
        # C-Kern und Bytecode jetzt bauen, dann startet Pynth gleich danach schnell
        if "pynth/_kernel.c" in changed:
            self._say("Übersetze den C-Kern ...")
        try:
            subprocess.run([sys.executable, "-c", "import pynth.fast"], cwd=str(self.repo),
                           capture_output=True, timeout=300)
            subprocess.run([sys.executable, "-m", "compileall", "-q", "pynth"], cwd=str(self.repo),
                           capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            log.info("Vorab-Uebersetzen uebersprungen: %s", e)
        self._say("Starte Pynth neu ... die Seite lädt gleich von selbst neu.", "restarting")
        try:
            self.before_shutdown()
        except Exception:
            log.exception("Zustand vor dem Neustart nicht gesichert")
        r = _sudo(RESTART)
        if r.returncode != 0:
            self._say("Neustart fehlgeschlagen: " + ((r.stderr or r.stdout).strip()[-300:] or str(r.returncode))
                      + ". Das Update ist geladen, wirkt aber erst nach einem Neustart.", "error")
