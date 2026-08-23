#!/usr/bin/env bash
# WalkingPad widget installer: sets up the collector backend (Python venv +
# systemd user service) for the Omarchy shell plugin. Safe to re-run.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$HOME/.local/share/walkingpad"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/walkingpad.service"

command -v python3 >/dev/null 2>&1 || {
  echo "error: python3 is required (on Omarchy/Arch: sudo pacman -S python)" >&2
  exit 1
}

mkdir -p "$DATA_DIR" "$UNIT_DIR"

echo "Creating Python environment in $DATA_DIR/venv ..."
python3 -m venv "$DATA_DIR/venv"
"$DATA_DIR/venv/bin/pip" install --quiet --upgrade pip
"$DATA_DIR/venv/bin/pip" install --quiet --requirement "$PLUGIN_DIR/backend/requirements.txt"

echo "Installing systemd user service ..."
cat > "$UNIT" <<EOF
[Unit]
Description=WalkingPad step collector
StartLimitIntervalSec=0

[Service]
ExecStart=$DATA_DIR/venv/bin/python $PLUGIN_DIR/backend/collector.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now walkingpad.service

echo
echo "Done. The collector is running and will connect to your pad automatically"
echo "the next time it powers on."
echo
echo "Logs:    journalctl --user -u walkingpad -f"
echo "Toggle:  systemctl --user stop walkingpad   (or right-click the bar widget)"
echo
echo "Uninstall:"
echo "  systemctl --user disable --now walkingpad"
echo "  rm $UNIT && systemctl --user daemon-reload"
echo "  rm -rf $DATA_DIR   # deletes your step history"
