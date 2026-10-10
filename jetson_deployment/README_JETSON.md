# Jetson Nano setup and laptop server relay

Use `jetson_inspector.py` for the full system. The Nano captures one camera stream, reads the barcode, runs the damage model locally, stores each report in its own SQLite outbox, then sends the structured result to the laptop server. The laptop receives the categorized result and stores it in the project's `warehouse_inventory.db`. The Nano does not need the laptop to run inference.

## How the pieces work

1. **Camera input:** the script reads a USB camera or CSI camera through OpenCV/GStreamer.
2. **Barcode category:** QR/1D/2D decoding reads the project's JSON or key/value payload, for example `ITEM:snack|TYPE:food|COST:25|SHELF:A1`, and extracts product, shelf, price, SKU, and coordinates.
3. **Damage category:** `best.onnx` runs on the Nano through OpenCV DNN. It returns class, confidence, and bounding box. The Nano combines that with the barcode data into one report.
4. **Local storage:** the report is inserted first into `nano_inspections.sqlite3` and also appended to `inspection_events.jsonl`. This is the durable upload queue.
5. **Laptop relay:** a background sender POSTs the report to `/api/inspection_event`. It sends only the categorized JSON result, not the camera image or video. If the laptop or network is unavailable, the event stays queued on the Nano and is retried with backoff.
6. **Central storage and dashboard:** the laptop inserts the event into `warehouse_inventory.db`. The event ID makes retries idempotent. The existing dashboard reads `/api/inventory` from that database.

If damage is detected without a readable barcode, the Nano queues an `Unidentified product` damage report. If neither a barcode nor a damage detection is found, it stores no inspection event. A barcode that remains in view is deduplicated for 30 seconds by default; a change between damaged and undamaged is recorded immediately.

`barcode_scanner_jetson.py` and `jetson_realtime_classifier.py` are standalone local-only tools. Use `jetson_inspector.py` when you want local storage and laptop relay.

## 1. Start the laptop server

On the laptop, open a terminal in the project folder and start the existing web server:

```powershell
.\.venv\Scripts\python.exe web_demo_server.py
```

The server listens on port `8080` and now binds to `0.0.0.0` so another device on the same LAN can reach it. In the server terminal, open the dashboard on the laptop at `http://localhost:8080`.

On the laptop, run `ipconfig` and note the Wi-Fi or Ethernet **IPv4 Address**, for example `192.168.1.20`. Keep the laptop and Nano on the same trusted private network. If Windows Firewall asks, allow Python on the **Private** network. If it does not ask, add an inbound TCP rule for port `8080` limited to the Private profile.

Optional: protect the receiver with a shared token. Set this before starting the server:

```powershell
$env:WAREHOUSE_API_TOKEN = "replace-with-a-long-random-token"
.\.venv\Scripts\python.exe web_demo_server.py
```

The same token must be set as `WAREHOUSE_SERVER_TOKEN` on the Nano. If no token is configured on the server, the event endpoint accepts requests from devices that can reach port 8080. Keep it on a trusted LAN and do not expose this development server directly to the internet.

## 2. Copy the deployment folder to the Nano

Copy the whole `jetson_deployment` folder, including `best.onnx` and `jetson_runtime.py`, into the Nano user's home directory. For example, from a laptop terminal:

```powershell
scp -r .\jetson_deployment nano@<JETSON_IP>:/home/nano/
```

Replace `<JETSON_IP>` with the Nano's address. A USB drive works too. Then connect to the Nano and install its system dependencies:

```bash
ssh nano@<JETSON_IP>
cd ~/jetson_deployment
bash install_jetson_deps.sh
```

Check the installed Jetson Linux release with `cat /etc/nv_tegra_release`. This package targets the JetPack 4.6.x software line. If you are setting up the Nano from scratch, use JetPack 4.6.6, which NVIDIA lists as supporting Jetson Nano; JetPack 4.6 is based on Ubuntu 18.04 and includes OpenCV 4.1.1. The installer uses JetPack's system OpenCV/NumPy and installs `pyzbar` plus `libzbar0` for 1D barcodes. OpenCV handles QR codes as a fallback. See NVIDIA's [JetPack 4.6.6 release page](https://developer.nvidia.com/jetpack-sdk-466).

## 3. Run the full pipeline manually

For a CSI camera, use:

```bash
cd ~/jetson_deployment
python3 jetson_inspector.py --model best.onnx --csi --server-url http://192.168.1.20:8080 --headless
```

Replace `192.168.1.20` with the laptop's IPv4 address. For a USB webcam, use `--camera 0` instead of `--csi`:

```bash
python3 jetson_inspector.py --model best.onnx --camera 0 --server-url http://192.168.1.20:8080 --headless
```

If token authentication is enabled, set the same token in the Nano shell before starting:

```bash
export WAREHOUSE_SERVER_TOKEN="replace-with-a-long-random-token"
```

To check connectivity before starting the camera, try `curl http://192.168.1.20:8080/api/stats` from the Nano. A JSON response means the laptop server is reachable. If `curl` is missing, install it with `sudo apt-get install curl`.

For a first run with a display attached, omit `--headless`; press `q` to quit and `s` to save a snapshot. In headless mode, use Ctrl+C to stop.

## 4. Confirm storage and delivery

- On the Nano, `nano_inspections.sqlite3` is the durable local queue and `inspection_events.jsonl` is a readable local audit log. Both are in the deployment folder by default.
- When an event is stored locally, the terminal prints `[local] Stored event ...`.
- When the laptop accepts it, the terminal prints `[sync] Server stored event ...`.
- Open `http://localhost:8080` on the laptop. The dashboard refreshes its inventory view from the laptop database. The Nano's sender marks an event delivered only after receiving the server acknowledgement.
- If the link drops, the Nano keeps inspecting and queueing locally. It retries when the server becomes reachable. On restart, it resumes sending the pending rows from SQLite.

## 5. Run automatically after Nano boot

First confirm the manual command works. Then create `/etc/systemd/system/warehouse-inspector.service` on the Nano and adjust the laptop IP and camera mode:

```ini
[Unit]
Description=Warehouse Jetson Nano Inspector
After=network-online.target nvargus-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=nano
WorkingDirectory=/home/nano/jetson_deployment
ExecStart=/usr/bin/python3 /home/nano/jetson_deployment/jetson_inspector.py --model /home/nano/jetson_deployment/best.onnx --csi --server-url http://192.168.1.20:8080 --headless
# If server token authentication is enabled, also add:
# Environment=WAREHOUSE_SERVER_TOKEN=replace-with-the-same-token
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now warehouse-inspector.service
sudo systemctl status warehouse-inspector.service
```

View logs with `journalctl -u warehouse-inspector.service -f`. If the laptop gets a different IP from DHCP, update the service URL or reserve a stable address for the laptop in the router.

## Model and runtime notes

- `best.onnx` is the binary damaged/undamaged model used by the project's integrated inspector. It was exported from `runs/detect/damage_binary_model/weights/best.pt` at opset 12 with input `1x3x416x416` and classes `damaged_product` and `undamaged_product`.
- The newer `specific_product_damage_model` has 11 classes and uses 640x640 input. To use it, export that checkpoint to ONNX at opset 12 on the development computer, copy it here, and pass `--input-size 640 --class-names "cinthol_pack_of_4_perfect,diet_coca_cola_perfect,kurkure_perfect,lays_perfect,superyou_protein_coffee_flacoured_perfect,yogabar_protein_wafer_perfect,diet_coca_cola_damaged,kurkure_damaged,lays_damaged,mountain_dew_damaged,pintola_protein_oats_damaged"`.
- The runner tries OpenCV CUDA DNN and falls back to CPU if CUDA inference fails. Actual acceleration depends on the Nano's OpenCV build.
- The damage output follows the source class rule: labels containing `undamaged` or `perfect` are acceptable; other labels are treated as damage. The source's demo-only mean-color override is not used.
- To re-export after retraining: `yolo export model=runs/detect/damage_binary_model/weights/best.pt format=onnx imgsz=416 batch=1 dynamic=False opset=12`. Copy the resulting ONNX file into the deployment folder.
- Optional damage GPIO is available in the standalone damage runner with `--gpio-pin <BOARD_PIN>`. Check the board's header pin map before wiring hardware.

The scripts were not run on a physical Nano in this workspace. Camera/Argus, GPIO, and CUDA availability depend on the device's JetPack image and attached hardware.
