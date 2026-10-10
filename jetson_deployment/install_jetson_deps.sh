#!/usr/bin/env bash
set -eu

# Keep OpenCV and NumPy on the Jetson system Python so OpenCV camera/DNN support
# stays aligned with the installed JetPack image.
sudo apt-get update
sudo apt-get install -y python3-opencv python3-numpy

python3 -c 'import cv2, numpy; print("[+] OpenCV", cv2.__version__, "NumPy", numpy.__version__)'
echo "[+] Jetson OpenCV and NumPy dependencies are ready."
