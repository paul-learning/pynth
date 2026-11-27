"""WLAN-Status und -Wechsel fuer die Web-UI.

Status liest nmcli ohne Rechte. Scannen, Verbinden und Hotspot laufen ueber das
root-Skript /usr/local/sbin/pynth-wifi (per sudo, siehe deploy/install-wifi.sh).
Fehlt das (z.B. am Laptop), meldet die API das nur, der Synth laeuft normal weiter.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading

log = logging.getLogger("pynth.wifi")

HELPER = "/usr/local/sbin/pynth-wifi"
HOTSPOT = "pynth-hotspot"


def split_terse(line: str) -> list[str]:
    """nmcli -t: Felder mit ':' getrennt, ':' und '\\' im Wert mit '\\' maskiert."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class Wifi:
    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda status: None)
        self.busy = None          # z.B. "Verbinde mit X ..."
        self.last = None          # {"ok": bool, "msg": str}
        self._lock = threading.Lock()

    def _run(self, args, stdin=None, timeout=20):
        return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)

    def _helper(self, cmd, stdin=None, timeout=150):
        return self._run(["sudo", "-n", HELPER, cmd], stdin, timeout)

    def status(self) -> dict:
        st = {"available": False, "setup": False, "mode": "offline", "connection": None,
              "ip": None, "busy": self.busy, "last": self.last}
        if not shutil.which("nmcli"):
            return st
        st["setup"] = os.path.exists(HELPER)
        try:
            r = self._run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"], timeout=5)
        except (OSError, subprocess.SubprocessError):
            return st
        for line in r.stdout.splitlines():
            f = split_terse(line)
            if len(f) < 4 or f[1] != "wifi":
                continue
            st["available"], st["iface"] = True, f[0]
            if f[2] == "connected":
                st["mode"] = "hotspot" if f[3] == HOTSPOT else "client"
                st["connection"] = f[3]
            elif f[2].startswith("connecting"):
                st["mode"] = "connecting"
            try:
                ip = self._run(["nmcli", "-g", "IP4.ADDRESS", "dev", "show", f[0]], timeout=5).stdout
                st["ip"] = ip.split("|")[0].strip().split("/")[0] or None
            except (OSError, subprocess.SubprocessError):
                pass
            break
        return st

    def scan(self) -> dict:
        try:
            r = self._helper("scan", timeout=40)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"Scan fehlgeschlagen: {e}"}
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout).strip()[-300:]
                    or "WLAN-Setup nicht installiert (deploy/install-wifi.sh)"}
        nets, known, mode = {}, set(), None
        for line in r.stdout.splitlines():
            kind, _, rest = line.partition(":")
            if kind == "mode":
                mode = rest
            elif kind == "known":
                known.add(split_terse(rest)[0])
            elif kind == "net":
                f = split_terse(rest)
                if len(f) < 3 or not f[2]:
                    continue
                sig = int(f[0]) if f[0].isdigit() else 0
                if f[2] not in nets or nets[f[2]]["signal"] < sig:
                    nets[f[2]] = {"ssid": f[2], "signal": sig, "secure": f[1] not in ("", "--")}
        for n in nets.values():
            n["known"] = n["ssid"] in known
        return {"ok": True, "mode": mode, "cached": mode == "hotspot",
                "networks": sorted(nets.values(), key=lambda n: -n["signal"])}

    def _background(self, label, cmd, stdin=None):
        with self._lock:
            if self.busy:
                return False
            self.busy = label
        self.on_change(self.status())

        def work():
            try:
                r = self._helper(cmd, stdin)
                msg = (r.stdout.strip().splitlines() or [r.stderr.strip() or "fertig"])[-1]
                self.last = {"ok": r.returncode == 0, "msg": msg}
            except (OSError, subprocess.SubprocessError) as e:
                self.last = {"ok": False, "msg": str(e)}
            log.info("WLAN: %s", self.last["msg"])
            self.busy = None
            self.on_change(self.status())
        threading.Thread(target=work, name="wifi", daemon=True).start()
        return True

    def connect(self, ssid: str, password: str = "") -> bool:
        ssid, password = str(ssid), str(password or "")
        if not ssid or "\n" in ssid or "\n" in password:
            raise ValueError("ungueltige Eingabe")
        return self._background(f"Verbinde mit {ssid} ...", "connect", f"{ssid}\n{password}\n")

    def hotspot(self) -> bool:
        return self._background("Starte Hotspot ...", "hotspot")
