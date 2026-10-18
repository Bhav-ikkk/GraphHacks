#!/usr/bin/env bash
set -e

echo "=========================================="
echo "  Deploying Syzygy EDL on AWS EC2"
echo "=========================================="

# 1. Install Docker and Docker Compose plugin if missing
if ! command -v docker &> /dev/null; then
    echo ">> Installing Docker..."
    sudo apt-get update -y
    sudo apt-get install -y docker.io docker-compose-plugin git curl
    sudo usermod -aG docker $USER
    echo ">> Docker installed."
fi

# 2. Setup .env file
if [ ! -f .env ]; then
    echo ">> .env not found. Creating from .env.example..."
    cp .env.example .env
    echo ">> Pointing FalkorDB to localhost for 0ms latency..."
    sed -i 's/FALKORDB_HOST=.*/FALKORDB_HOST=127.0.0.1/' .env
    echo ""
    echo "IMPORTANT: Please check your .env file to ensure FALKORDB_PASSWORD and SYZYGY_LLM_API_KEY are set."
fi

# 3. Build and Start Container
echo ">> Building and starting Syzygy EDL container..."
sudo docker compose up -d --build

# 4. Wait for startup and check health
echo ">> Verifying service health..."
sleep 4
if curl -s http://localhost:8000/api/status | grep -q '"ok":true'; then
    echo "=========================================="
    echo "  DEPLOYMENT SUCCESSFUL!"
    echo "=========================================="
    PUBLIC_IP=$(curl -s https://ifconfig.me || echo "your-ec2-ip")
    echo "Access your app at: http://${PUBLIC_IP}:8000"
    echo ""
    echo "NOTE: Ensure AWS Security Group allows Inbound TCP on Port 8000."
else
    echo ">> Container started! Check status at http://localhost:8000/api/status"
    sudo docker compose logs --tail 20
fi
