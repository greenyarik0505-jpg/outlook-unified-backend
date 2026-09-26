#!/usr/bin/env bash
# ==============================================================================
# One-Click Deployment Script for Oracle Cloud VPS (Ubuntu 20.04 / 22.04 / 24.04)
# Configures Swap, Docker, Docker Compose, Firewall, and launches the Stack.
# ==============================================================================

set -e

echo "🚀 [1/5] Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common git ufw

# Configure Swap (2GB) - essential for Oracle Free Tier (prevent Chromium OOM killer)
if [ ! -f /swapfile ]; then
    echo "💾 [2/5] Creating 2GB swap space for headless browsers..."
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "✅ Swap enabled."
else
    echo "ℹ️  Swap already configured."
fi

# Install Docker if missing
if ! command -v docker &> /dev/null; then
    echo "🐳 [3/5] Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker "$USER"
    rm -f get-docker.sh
    echo "✅ Docker installed."
else
    echo "ℹ️  Docker is already installed."
fi

# Install Docker Compose Plugin if missing
sudo apt-get install -y docker-compose-plugin || true

# Configure Firewall (Oracle Cloud UFW & iptables)
echo "🔒 [4/5] Opening firewall ports (8000)..."
sudo ufw allow 8000/tcp || true
sudo ufw allow 22/tcp || true

# Oracle Linux / Ubuntu iptables often blocks ports by default
if command -v iptables &> /dev/null; then
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT || true
    sudo netfilter-persistent save 2>/dev/null || true
fi

echo "🚀 [5/5] Building and launching Docker container stack..."
sudo docker compose down 2>/dev/null || true
sudo docker compose up -d --build

echo ""
echo "======================================================================"
echo "🎉 DEPLOYMENT SUCCESSFUL!"
echo "======================================================================"
echo "Backend URL: http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP'):8000"
echo "API Docs:    http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP'):8000/docs"
echo "Mail API:    http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP'):8000/api/mail/inbox"
echo "OTP API:     http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP'):8000/api/mail/otp"
echo "======================================================================"
echo "To view live logs: sudo docker compose logs -f"
echo "======================================================================"
