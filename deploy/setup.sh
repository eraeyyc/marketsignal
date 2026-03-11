#!/bin/bash
# MarketSignal — DigitalOcean VPS setup script
# Run as root on a fresh Ubuntu 22.04 droplet:
#   bash setup.sh

set -e

APP_USER="marketsignal"
APP_DIR="/home/$APP_USER/marketsignal"
LOG_DIR="/var/log/marketsignal"

echo "=== MarketSignal VPS Setup ==="

# ── System packages ────────────────────────────────────────────────────────────
echo "[1/6] Installing system packages..."
apt-get update -q
apt-get install -y -q python3 python3-pip python3-venv git ufw

# ── Create app user ────────────────────────────────────────────────────────────
echo "[2/6] Creating app user '$APP_USER'..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$APP_USER"
fi

# ── Clone repo ─────────────────────────────────────────────────────────────────
echo "[3/6] Cloning repo..."
if [ ! -d "$APP_DIR" ]; then
    git clone https://github.com/eraeyyc/marketsignal.git "$APP_DIR"
else
    echo "  Repo already exists — pulling latest..."
    git -C "$APP_DIR" pull
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── Python dependencies ────────────────────────────────────────────────────────
echo "[4/6] Installing Python dependencies..."
pip3 install -q -r "$APP_DIR/requirements.txt"

# ── Log directory ──────────────────────────────────────────────────────────────
echo "[5/6] Creating log directory..."
mkdir -p "$LOG_DIR"
chown "$APP_USER:$APP_USER" "$LOG_DIR"

# ── Systemd services ───────────────────────────────────────────────────────────
echo "[6/6] Installing systemd services..."
cp "$APP_DIR/deploy/adsb-collector.service"         /etc/systemd/system/
cp "$APP_DIR/deploy/notam-collector.service"        /etc/systemd/system/
cp "$APP_DIR/deploy/marketsignal-dashboard.service" /etc/systemd/system/
systemctl daemon-reload

# ── Firewall ───────────────────────────────────────────────────────────────────
echo "Configuring firewall..."
ufw allow OpenSSH
ufw allow 8501/tcp comment "Streamlit dashboard"
ufw --force enable

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Copy your secret files to the droplet:"
echo "     scp .env root@YOUR_IP:$APP_DIR/"
echo "     scp gdelt_credentials.json root@YOUR_IP:$APP_DIR/"
echo "     scp gdelt_events.db root@YOUR_IP:$APP_DIR/          # 768MB"
echo "     scp aircraft-database-complete.csv root@YOUR_IP:$APP_DIR/"
echo ""
echo "  2. Update GOOGLE_APPLICATION_CREDENTIALS in .env to:"
echo "     GOOGLE_APPLICATION_CREDENTIALS=$APP_DIR/gdelt_credentials.json"
echo ""
echo "  3. Start the services:"
echo "     systemctl enable --now adsb-collector"
echo "     systemctl enable --now notam-collector   # only once Cirium token is in .env"
echo "     systemctl enable --now marketsignal-dashboard"
echo ""
echo "  4. Check they're running:"
echo "     systemctl status adsb-collector"
echo "     tail -f /var/log/marketsignal/adsb.log"
echo ""
echo "  5. Dashboard will be at:  http://YOUR_IP:8501"
echo ""
