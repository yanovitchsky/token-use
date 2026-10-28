#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "${BIN_DIR}"

if ! python3 -m pip install --user -e "${ROOT}"; then
  cat > "${BIN_DIR}/tokuse" <<EOF
#!/bin/sh
export PYTHONPATH="${ROOT}/src"
exec python3 -m tokuse "\$@"
EOF
  chmod +x "${BIN_DIR}/tokuse"
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "Install Xcode Command Line Tools for the menu bar app"
  exit 1
fi

swiftc "${ROOT}/macos/MenuBar.swift" -o "${BIN_DIR}/tokuse-menu" -framework AppKit -framework SwiftUI

PLIST="${HOME}/Library/LaunchAgents/com.yannakoun.tokuse.plist"
mkdir -p "${HOME}/Library/LaunchAgents"
cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yannakoun.tokuse</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BIN_DIR}/tokuse-menu</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "${PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
