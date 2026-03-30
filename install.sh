#!/bin/bash
# =============================================================================
# Frame Art Server — Installer for Raspberry Pi (or any Linux box)
# =============================================================================
set -e

INSTALL_DIR="/opt/frame-art-server"
SERVICE_NAME="frame-art"
VENV_DIR="$INSTALL_DIR/venv"

echo "============================================"
echo "  Frame Art Server — Installer"
echo "============================================"
echo ""

# Check for root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash install.sh"
    exit 1
fi

# Install system dependencies
echo "[1/5] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3-dev \
    libjpeg-dev zlib1g-dev libfreetype6-dev

# Create install directory
echo "[2/5] Setting up install directory..."
mkdir -p "$INSTALL_DIR"
cp -r ./* "$INSTALL_DIR/"

# Create virtual environment
echo "[3/5] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# Create working directories
echo "[4/5] Creating working directories..."
mkdir -p "$INSTALL_DIR/art_cache/processed"
mkdir -p "$INSTALL_DIR/my_art"

# Install systemd service
echo "[5/5] Installing systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << 'EOF'
[Unit]
Description=Frame Art Server — Custom art for Samsung Frame TV
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/frame-art-server
ExecStart=/opt/frame-art-server/venv/bin/python frame_art_server.py
Restart=on-failure
RestartSec=30
User=root

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=frame-art

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}

echo ""
echo "============================================"
echo "  Installation complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit the config:    sudo nano $INSTALL_DIR/config.yaml"
echo "     - Set your TV's IP address"
echo "     - (Optional) Add Rijksmuseum API key"
echo "     - Customize art queries and schedule"
echo ""
echo "  2. Test without TV:    cd $INSTALL_DIR && sudo venv/bin/python frame_art_server.py --test-fetch"
echo ""
echo "  3. Pair with your TV:  cd $INSTALL_DIR && sudo venv/bin/python frame_art_server.py --once"
echo "     (Accept the pairing prompt on your TV with the remote)"
echo ""
echo "  4. Start the service:  sudo systemctl start $SERVICE_NAME"
echo "     View logs:          sudo journalctl -u $SERVICE_NAME -f"
echo ""
