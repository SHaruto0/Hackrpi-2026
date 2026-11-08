#!/usr/bin/env bash
set -e
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo -E"
fi

echo "=== [1/5] Installing system packages ==="
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3 python3-pip python3-venv unzip git libgl1 libglib2.0-0 || \
$SUDO apt-get install -y -qq python3 python3-pip python3-venv unzip git libgl1-mesa-glx libglib2.0-0

echo "=== [2/5] Setting up Python virtual environment ==="
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

python3 -m pip install --upgrade pip

# Install PyTorch if not already present on the image
if ! python3 -c "import torch" &>/dev/null; then
    if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
        echo "GPU detected! Installing CUDA PyTorch..."
        python3 -m pip install torch torchvision
    else
        echo "CPU instance detected! Installing fast lightweight CPU PyTorch..."
        python3 -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    fi
fi

echo "=== [3/5] Installing hloc, LightGlue, and pycolmap ==="
python3 -m pip install --upgrade "pycolmap>=3.13.0" || python3 -m pip install pycolmap
python3 -m pip install h5py scipy tqdm opencv-python-headless matplotlib plotly packaging gdown
python3 -m pip install git+https://github.com/cvg/LightGlue.git
if [ ! -d "Hierarchical-Localization" ]; then
    git clone --recursive https://github.com/cvg/Hierarchical-Localization.git
fi
python3 -m pip install -e Hierarchical-Localization --no-deps

echo "=== [4/5] Running build_poses.py (ARKit -> COLMAP empty_model) ==="
python3 build_poses.py --dataset_dir . --output_dir empty_model

echo "=== [5/5] Running build_map.py (SuperPoint + LightGlue + Triangulation) ==="
python3 build_map.py --dataset_dir . --empty_model_dir empty_model --final_model_dir final_model

echo "=== DONE! Your final_model is ready at: $(pwd)/final_model ==="
ls -lh final_model/
