#!/usr/bin/env bash
set -eu

# Keep OpenCV and NumPy on the JetPack system Python. Do not replace them with
# generic pip wheels: CSI/GStreamer and CUDA support depend on the Nano image.
sudo apt-get update
sudo apt-get install -y python3-opencv python3-numpy python3-pip libzbar0

# pyzbar is optional for QR-only use, but enables 1D barcode formats.
if python3 -m pip install --user 'pyzbar==0.1.9'; then
    echo "[+] pyzbar installed for 1D barcode decoding."
else
    echo "[!] pyzbar install failed. QR decoding remains available through OpenCV."
fi

python3 -c 'import cv2, numpy; print("[+] OpenCV", cv2.__version__, "NumPy", numpy.__version__)'
echo "[+] Jetson Nano Python dependencies are ready."
