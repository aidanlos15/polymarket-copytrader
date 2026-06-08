# Deploy to DigitalOcean — full step-by-step

Runs both trackers + the web dashboard 24/7 on an always-on server, independent of your
laptop. ~$6/month. Paper-trading (dry-run) unless you explicitly enable live trading.

## 1. Create the droplet
1. Sign up / log in at https://www.digitalocean.com/
2. **Create → Droplets.**
3. **Choose an image:** Ubuntu **24.04 (LTS) x64**.
4. **Choose a plan:** Basic → Regular → the **$6/mo** option (1 GB / 1 vCPU).
5. **Choose a region:** pick the closest to you (e.g. New York / London).
6. **Authentication:** simplest is **Password** — set a strong root password and save it.
   (SSH keys are more secure if you know them; password is fine to start.)
7. **Create Droplet.** Wait ~30s, then copy its **public IP** (e.g. `203.0.113.45`).

## 2. Upload the app from your Mac
Open **Terminal** on your Mac and run (replace `IP` with your droplet's IP):
```bash
scp -r ~/Desktop/polymarket-copytrader root@IP:/root/
```
Enter the root password when prompted. This uploads the whole project folder.

## 3. Add your secret (Alchemy endpoint)
The Alchemy URL is NOT in the uploaded folder (it's kept out of git). Create it on the
server. SSH in first:
```bash
ssh root@IP
```
Then create the `.env` (paste your real Alchemy URL):
```bash
echo 'POLYGON_HTTP=https://polygon-mainnet.g.alchemy.com/v2/YOURKEY' > /root/polymarket-copytrader/.env
```

## 4. Install + start everything
```bash
cd /root/polymarket-copytrader
bash deploy/install.sh
```
This installs Python, copies the app to `/opt/polymarket`, copies your `.env`, seeds ~1000
historical trades per target, and starts three auto-restarting services:
`polymarket-rn1`, `polymarket-swisstony`, `polymarket-dashboard`.

## 5. Secure the dashboard password
```bash
nano /etc/systemd/system/polymarket-dashboard.service
```
Change `DASHBOARD_USER` and `DASHBOARD_PASS` to your own, save (Ctrl+O, Enter, Ctrl+X), then:
```bash
systemctl daemon-reload && systemctl restart polymarket-dashboard
```

## 6. Open the firewall + view it
DigitalOcean's droplet firewall (if you enabled one) must allow **TCP 8080**. The install
script already opens the in-VM `ufw`. Then open in any browser:
```
http://IP:8080
```
Log in with the user/pass you set. It auto-refreshes every 30s and shows both trackers.

## Managing it
```bash
systemctl status polymarket-rn1            # running?
journalctl -u polymarket-rn1 -f            # live log (Ctrl+C to stop watching)
journalctl -u polymarket-swisstony -f
systemctl restart polymarket-rn1           # after a config change
```
Pull a styled `.xlsx` down to your Mac anytime:
```bash
scp root@IP:/opt/polymarket/data/polymarket_paper_trades.xlsx ~/Desktop/
```

## Updating the code later
Re-upload and re-run install (it overwrites the app, keeps your data + .env):
```bash
scp -r ~/Desktop/polymarket-copytrader root@IP:/root/
ssh root@IP "cd /root/polymarket-copytrader && bash deploy/install.sh"
```

## Going live later (REAL MONEY — optional, not now)
Only after the friction-adjusted paper edge is proven. On the server, add to
`/opt/polymarket/.env`:
```
ENABLE_LIVE_TRADING=true
PRIVATE_KEY_FILE=/opt/polymarket/.pk
SIGNATURE_TYPE=0
LIVE_MAX_ORDER_USD=5
```
Put the key in a locked-down file:
`echo 0xYOURKEY > /opt/polymarket/.pk && chmod 600 /opt/polymarket/.pk`, do the one-time
USDC+CTF allowances (see main README), then `systemctl restart polymarket-rn1`.
> A private key on a server is a real responsibility — keep the droplet patched, use SSH
> keys, and never put the key anywhere but that chmod-600 file.
