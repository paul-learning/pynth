#!/bin/bash
# Einmal als pi ausfuehren (und nach Updates, die deploy/pynth.service oder
# deploy/pynth-system.sudoers aendern):
#   sudo bash /home/pynth/pynth/deploy/install-system.sh
# Richtet ein: Dienst-Datei (Autostart, Status-LED), sudo-Regel fuer Ausschalten/Neustart
# aus der Web-UI.
# Rueckgaengig (nur die sudo-Regel):  sudo bash /home/pynth/pynth/deploy/install-system.sh --uninstall
set -eu
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo starten"; exit 1; }
DIR=$(cd "$(dirname "$0")" && pwd)

if [ "${1:-}" = --uninstall ]; then
    rm -f /etc/sudoers.d/pynth-system
    echo "sudo-Regel entfernt. Ausschalten/Update aus der Web-UI gehen nicht mehr."
    exit 0
fi

echo "== Dienst-Datei"
install -o root -g root -m 0644 "$DIR/pynth.service" /etc/systemd/system/pynth.service
systemctl daemon-reload
systemctl enable pynth.service >/dev/null 2>&1

echo "== sudo-Regel (nur Ausschalten/Neustart)"
install -o root -g root -m 0440 "$DIR/pynth-system.sudoers" /etc/sudoers.d/pynth-system.new
if visudo -cqf /etc/sudoers.d/pynth-system.new; then
    mv /etc/sudoers.d/pynth-system.new /etc/sudoers.d/pynth-system
else
    rm -f /etc/sudoers.d/pynth-system.new; echo "sudo-Regel fehlerhaft, Abbruch."; exit 1
fi

echo
echo "Fertig. Neu starten, damit alles greift:  sudo systemctl restart pynth"
