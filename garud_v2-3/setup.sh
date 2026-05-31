#!/bin/bash
# GARUD v3 — One-time setup for macOS M4
set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

echo ""
echo "  ⬡  GARUD v3 — Full Setup"
echo "══════════════════════════════════════"

# Virtual env
if [ ! -d ".venv" ]; then
  echo -e "${YELLOW}Creating virtual environment with Python 3.11...${NC}"
  /opt/homebrew/bin/python3.11 -m venv .venv || python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip --quiet

echo -e "${YELLOW}Installing core packages...${NC}"
pip install torch torchvision --quiet
pip install "opencv-contrib-python>=4.9.0" --quiet   # includes face recognition
pip install ultralytics Pillow flask numpy --quiet

echo -e "${YELLOW}Installing Twilio (SMS alerts)...${NC}"
pip install twilio --quiet

echo -e "${YELLOW}Downloading AI models...${NC}"
python3 -c "
from ultralytics import YOLO
print('  Downloading yolov8n.pt ...')
YOLO('yolov8n.pt')
print('  Downloading yolov8n-pose.pt ...')
YOLO('yolov8n-pose.pt')
print('  Models ready ✓')
" 2>/dev/null || echo "  Models will download on first run"

# Create placeholder known face
echo -e "${YELLOW}Creating project folders...${NC}"
mkdir -p known_faces recordings logs

echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  GARUD v3 setup complete!${NC}"
echo ""
echo -e "${CYAN}  Run:${NC}"
echo "    source .venv/bin/activate"
echo "    python3 garud.py"
echo ""
echo -e "${CYAN}  Web dashboard:${NC}  http://localhost:8080"
echo -e "${CYAN}  Login:${NC}          admin / garud2024"
echo ""
echo -e "${CYAN}  To add face recognition:${NC}"
echo "    Copy a person's photo to known_faces/Name.jpg"
echo ""
echo -e "${CYAN}  To enable SMS/email alerts:${NC}"
echo "    Click ⚙ Settings in the app"
echo -e "${GREEN}══════════════════════════════════════${NC}"
