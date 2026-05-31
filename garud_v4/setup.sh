#!/bin/bash
# GARUD v4 — One-time setup for macOS M4
set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

echo ""
echo "  ⬡  GARUD v4 — Full Setup"
echo "══════════════════════════════════════"

if [ ! -d ".venv" ]; then
  echo -e "${YELLOW}Creating virtual environment...${NC}"
  /opt/homebrew/bin/python3.11 -m venv .venv || python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip --quiet

echo -e "${YELLOW}Installing packages...${NC}"
pip install torch torchvision --quiet
pip install "opencv-contrib-python>=4.9.0" --quiet
pip install ultralytics Pillow flask numpy --quiet
pip install twilio reportlab --quiet

echo -e "${YELLOW}Downloading AI models...${NC}"
python3 -c "
from ultralytics import YOLO
YOLO('yolov8n.pt')
YOLO('yolov8n-pose.pt')
print('  Models ready ✓')
" 2>/dev/null || echo "  Models will download on first run"

mkdir -p known_faces recordings logs reports continuous

echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}  GARUD v4 setup complete!${NC}"
echo ""
echo -e "${CYAN}  Run:${NC}"
echo "    source .venv/bin/activate"
echo "    python3 garud.py"
echo ""
echo -e "${CYAN}  Web dashboard:${NC}  http://localhost:8080"
echo -e "${CYAN}  Login:${NC}          admin / garud2024"
echo -e "${GREEN}══════════════════════════════════════${NC}"
