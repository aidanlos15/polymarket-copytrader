# Deploying to an always-on cloud VM

This runs both trackers + the web dashboard 24/7 on a Linux server, independent of your
laptop. ~$5/month. Paper-trading only unless you explicitly enable live trading.

## 1. Create the VM
Pick any provider — **DigitalOcean**, **Hetzner**, or **AWS Lightsail** are all easy:
- OS: **Ubuntu 22.04 or 24.04**
- Size: the smallest (1 vCPU / 1 GB) is plenty (~$4–6/mo; Hetzner is cheapest)
- In the provider's firewall, **allow inbound TCP 22 (SSH) and 8080 (dashboard)**

Note the VM's public IP.

## 2. Copy the project up
From your Mac (in the project folder):
```bash
# easiest: copy the whole folder
scp -r ~/Desktop/* root@VM_IP:/root/polymarket-src/
ssh root@VM_IP
cd /root/polymarket-src
```
(Or push to a private GitHub repo and `git clone` it on the VM.)

## 3. Install + start everything
```bash
bash deploy/install.sh
```
This installs Python, sets up `/opt/polymarket`, seeds ~1000 historical trades for each
target, and starts three auto-restarting services: `polymarket-rn1`,
`polymarket-swisstony`, `polymarket-dashboard`.

## 4. Secure the dashboard (do this!)
Edit the password before relying on it:
```bash
sudo nano /etc/systemd/system/polymarket-dashboard.service   # set DASHBOARD_USER / DASHBOARD_PASS
sudo systemctl daemon-reload && sudo systemctl restart polymarket-dashboard
```

## 5. View it
Open `http://VM_IP:8080` in any browser, from any device. It auto-refreshes every 30s and
shows both trackers' KPIs and positions. Log in with the user/pass you set.

The styled **.xlsx files** still exist on the server at `/opt/polymarket/data/` — pull one
down anytime with: `scp root@VM_IP:/opt/polymarket/data/polymarket_paper_trades.xlsx .`

## Managing it
```bash
systemctl status polymarket-rn1            # is it running?
journalctl -u polymarket-rn1 -f            # live logs
sudo systemctl restart polymarket-rn1      # restart after a config change
```

## Going live later (REAL MONEY — optional, not now)
Only after the friction-adjusted paper edge is proven. On the VM:
1. Put your key in a locked-down file: `echo 0xYOURKEY | sudo tee /opt/polymarket/.pk >/dev/null && sudo chmod 600 /opt/polymarket/.pk`
2. In the relevant service file add:
   `Environment=ENABLE_LIVE_TRADING=true`
   `Environment=PRIVATE_KEY_FILE=/opt/polymarket/.pk`
   `Environment=SIGNATURE_TYPE=0`   (or 1/2 + FUNDER_ADDRESS for proxy accounts)
3. Do the one-time USDC + CTF allowances (see main README), then
   `daemon-reload` + `restart`. Start with a tiny `LIVE_MAX_ORDER_USD`.
> A private key on a cloud box is a real responsibility: keep the VM patched, SSH
> key-only, firewalled, and never put the key anywhere but that chmod-600 file.
```
