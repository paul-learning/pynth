#!/bin/bash
# Einmal als pi ausfuehren (und nach Updates, die deploy/pynth-wifi aendern):
#   sudo bash /home/pynth/pynth/deploy/install-wifi.sh
# Rueckgaengig:  sudo bash /home/pynth/pynth/deploy/install-wifi.sh --uninstall
set -eu
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo starten"; exit 1; }
DIR=$(cd "$(dirname "$0")" && pwd)

if [ "${1:-}" = --uninstall ]; then
    systemctl disable --now pynth-wifi.service 2>/dev/null || true
    nmcli con down pynth-hotspot 2>/dev/null || true
    nmcli con delete pynth-hotspot 2>/dev/null || true
    rm -f /etc/systemd/system/pynth-wifi.service /etc/sudoers.d/pynth-wifi /usr/local/sbin/pynth-wifi
    systemctl daemon-reload
    echo "Entfernt. /etc/default/pynth-wifi bleibt liegen (kann geloescht werden)."
    exit 0
fi

nmcli -t general status >/dev/null 2>&1 || { echo "NetworkManager laeuft nicht, Abbruch."; exit 1; }

echo "== Pakete (avahi fuer pynth.local, iw fuer den Hotspot)"
apt-get install -y avahi-daemon iw >/dev/null
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

echo "== Skript, Einstellungen, sudo-Regel"
install -o root -g root -m 0755 "$DIR/pynth-wifi" /usr/local/sbin/pynth-wifi
[ -f /etc/default/pynth-wifi ] || install -o root -g root -m 0600 "$DIR/pynth-wifi.conf" /etc/default/pynth-wifi
install -o root -g root -m 0440 "$DIR/pynth-wifi.sudoers" /etc/sudoers.d/pynth-wifi.new
if visudo -cqf /etc/sudoers.d/pynth-wifi.new; then
    mv /etc/sudoers.d/pynth-wifi.new /etc/sudoers.d/pynth-wifi
else
    rm -f /etc/sudoers.d/pynth-wifi.new; echo "sudo-Regel fehlerhaft, Abbruch."; exit 1
fi

echo "== Hostname"
if [ "$(hostname)" != pynth ]; then
    old=$(hostname)
    hostnamectl set-hostname pynth
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tpynth/" /etc/hosts
    grep -q '^127\.0\.1\.1' /etc/hosts || printf '127.0.1.1\tpynth\n' >> /etc/hosts
    systemctl restart avahi-daemon || true
    echo "Hostname von $old auf pynth geaendert"
fi

echo "== Waechter-Dienst"
install -o root -g root -m 0644 "$DIR/pynth-wifi.service" /etc/systemd/system/pynth-wifi.service
systemctl daemon-reload
systemctl enable pynth-wifi.service >/dev/null 2>&1
systemctl restart pynth-wifi.service

. /etc/default/pynth-wifi
echo
echo "Fertig. Web-UI: http://pynth.local:8080"
echo "Ohne bekanntes WLAN startet nach ${GRACE:-60}s der Hotspot \"${HOTSPOT_SSID:-pynth}\""
echo "(Passwort in /etc/default/pynth-wifi), dann: http://10.42.0.1:8080"
