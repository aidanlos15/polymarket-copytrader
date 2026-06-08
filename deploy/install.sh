#!/usr/bin/env bash
# One-shot installer for an Ubuntu/Debian VM. Run from the project root:
#   bash deploy/install.sh
set -euo pipefail

APP=/opt/polymarket
REPO="$(cd "$(dirname "$0")/.." && pwd)"   # project root (parent of deploy/)

echo ">> Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

echo ">> Creating $APP ..."
sudo mkdir -p "$APP/data"
sudo cp "$REPO"/bot.py "$REPO"/polymarket_client.py "$REPO"/excel_client.py \
        "$REPO"/config.py "$REPO"/trader.py "$REPO"/dashboard.py \
        "$REPO"/onchain_detector.py "$REPO"/requirements.txt "$APP/"

# Secrets: copy the .env (POLYGON_HTTP = your Alchemy endpoint) if you created one.
if [ -f "$REPO/.env" ]; then
    sudo cp "$REPO/.env" "$APP/.env"
    echo ">> Copied .env (POLYGON_HTTP) into $APP"
else
    echo ">> WARNING: no .env found. On-chain detection needs POLYGON_HTTP."
    echo "   Create $APP/.env with:  POLYGON_HTTP=https://polygon-mainnet.g.alchemy.com/v2/YOURKEY"
fi

echo ">> Python venv + deps ..."
sudo python3 -m venv "$APP/.venv"
sudo "$APP/.venv/bin/pip" install --upgrade pip
sudo "$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

echo ">> (Optional) seeding history for both targets ..."
sudo "$APP/.venv/bin/python" - <<'PY' || true
import subprocess, os
for name, addr, xlsx in [
    ("RN1", "0x2005d16a84ceefa912d4e380cd32e7ff827875ea", "/opt/polymarket/data/polymarket_paper_trades.xlsx"),
    ("swisstony", "0x204f72f35326db932158cba6adff0b9a1da95e14", "/opt/polymarket/data/swisstony_paper_trades.xlsx"),
]:
    env = {**os.environ, "TARGET_NAME": name, "TARGET_ADDRESS": addr, "EXCEL_PATH": xlsx}
    print(f"seeding {name} ...")
    subprocess.run(["/opt/polymarket/.venv/bin/python", "/opt/polymarket/bot.py",
                    "--once", "--backfill", "1000"], env=env, check=False)
PY

echo ">> Installing systemd services ..."
sudo cp "$REPO"/deploy/polymarket-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-rn1 polymarket-swisstony polymarket-dashboard

echo ">> Opening firewall port 8080 (if ufw is active) ..."
sudo ufw allow 8080/tcp 2>/dev/null || true

echo ""
echo "DONE. Dashboard: http://<this-vm-ip>:8080"
echo "  - change DASHBOARD_USER/PASS in /etc/systemd/system/polymarket-dashboard.service"
echo "  - logs:   journalctl -u polymarket-rn1 -f"
echo "  - status: systemctl status polymarket-rn1"
