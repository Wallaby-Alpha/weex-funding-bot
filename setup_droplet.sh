#!/bin/bash
# setup_droplet.sh
# One-click setup script for WEEX Funding Capitulation Bot on DigitalOcean Droplet
set -e

INSTALL_DIR="/opt/weex-funding-bot"

echo "=========================================================="
echo " 🚀 Setting up WEEX Funding Capitulation Bot on Droplet"
echo "=========================================================="

echo "=== 1. Updating System & Installing Prerequisites ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl

echo "=== 2. Setting up Directory at ${INSTALL_DIR} ==="
sudo mkdir -p ${INSTALL_DIR}
sudo chown -R $USER:$USER ${INSTALL_DIR}
cd ${INSTALL_DIR}

echo "=== 3. Pulling Repository from GitHub ==="
if [ ! -d ".git" ]; then
    git clone https://github.com/Wallaby-Alpha/weex-funding-bot.git .
else
    git pull origin main
fi

echo "=== 4. Creating Python Virtual Environment ==="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install ccxt pandas numpy requests

echo "=== 5. Setting up Configuration Template ==="
if [ ! -f "weex_funding_config.json" ]; then
    cp weex_funding_config.example.json weex_funding_config.json
    echo "Created weex_funding_config.json. Default is DRY_RUN = True."
fi

echo "=== 6. Installing Systemd Service (Auto-Start & Restart) ==="
sudo bash -c "cat <<EOF > /etc/systemd/system/weex-funding-bot.service
[Unit]
Description=WEEX Funding Rate Capitulation Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/weex_funding_bot.py
Restart=always
RestartSec=15
StandardOutput=append:/var/log/weex_funding_bot.log
StandardError=append:/var/log/weex_funding_bot.log

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
echo "Systemd service created: weex-funding-bot.service"

echo "=========================================================="
echo " ✅ Setup Complete!"
echo " Next Steps:"
echo " 1. Edit your configuration with your credentials:"
echo "    nano ${INSTALL_DIR}/weex_funding_config.json"
echo " 2. Test run in foreground (verify logs and dry-run scan):"
echo "    cd ${INSTALL_DIR} && ./venv/bin/python weex_funding_bot.py"
echo " 3. Start background systemd service:"
echo "    sudo systemctl enable weex-funding-bot"
echo "    sudo systemctl start weex-funding-bot"
echo " 4. Check status & logs:"
echo "    sudo systemctl status weex-funding-bot"
echo "    tail -f /var/log/weex_funding_bot.log"
echo "=========================================================="
