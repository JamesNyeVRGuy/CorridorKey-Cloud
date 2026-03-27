#!/bin/bash
# CorridorKey Node Agent — Linux installer
#
# Usage:
#   tar -xzf corridorkey-node-linux-x64.tar.gz
#   cd corridorkey-node
#   bash install.sh
#
# Installs to ~/.local/share/corridorkey-node/ with:
# - Symlink in ~/.local/bin/
# - XDG autostart desktop entry (optional)

set -e

INSTALL_DIR="${HOME}/.local/share/corridorkey-node"
BIN_DIR="${HOME}/.local/bin"
AUTOSTART_DIR="${HOME}/.config/autostart"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "CorridorKey Node Agent — Installer"
echo "==================================="
echo ""
echo "Install directory: ${INSTALL_DIR}"
echo ""

# Copy files
mkdir -p "${INSTALL_DIR}"
cp -a "${SCRIPT_DIR}"/* "${INSTALL_DIR}"/
chmod +x "${INSTALL_DIR}/corridorkey-node"

# Symlink to PATH
mkdir -p "${BIN_DIR}"
ln -sf "${INSTALL_DIR}/corridorkey-node" "${BIN_DIR}/corridorkey-node"

echo "Installed to ${INSTALL_DIR}"
echo "Binary linked to ${BIN_DIR}/corridorkey-node"

# Config file
if [ ! -f "${INSTALL_DIR}/node.env" ] && [ -f "${INSTALL_DIR}/node.env.example" ]; then
    cp "${INSTALL_DIR}/node.env.example" "${INSTALL_DIR}/node.env"
    echo ""
    echo "Config file created: ${INSTALL_DIR}/node.env"
    echo "Edit it to set your server URL and auth token."
fi

# Auto-start
echo ""
read -p "Start CorridorKey Node on login? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    mkdir -p "${AUTOSTART_DIR}"
    cat > "${AUTOSTART_DIR}/corridorkey-node.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=CorridorKey Node
Exec=${INSTALL_DIR}/corridorkey-node
Icon=corridorkey
Comment=CorridorKey GPU compute node
X-GNOME-Autostart-enabled=true
DESKTOP
    echo "Auto-start enabled (${AUTOSTART_DIR}/corridorkey-node.desktop)"
fi

echo ""
echo "Done! Run 'corridorkey-node' to start, or edit ${INSTALL_DIR}/node.env first."
