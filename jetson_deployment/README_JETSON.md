# Jetson QR stop-and-inspect node

This package runs on the rover's Jetson after the SLAM map has been built and saved separately. It does not build maps or send navigation goals. It gates the velocity commands from the navigation stack, stops the rover when a QR code enters the camera view, checks the expected and observed products, then checks condition.

## Inspection sequence

1. Subscribe to the ROS camera image topic and navigation velocity topic.
2. Use OpenCV QR detection to detect a QR square. On the first sighting, immediately gate motor output to zero at 20 Hz.
3. Wait for the rover to settle, then decode the QR payload. If the square is visible but cannot yet be decoded, remain stopped and retry on later camera frames.
4. Run the product identity model, associate the nearest detected product with the QR position, and compare that class with the expected item in the QR.
5. Run the binary damage model on the selected product crop.
6. Store the event in a local SQLite outbox and JSONL log. If WAREHOUSE_SERVER_URL is set, upload it to /api/inspection_event; failed uploads stay queued for retry.
7. Resume forwarding navigation velocity after recording the inspection. The same QR is ignored until it leaves view for several frames, preventing duplicate scans while the rover is stopped.

If model inference fails, the node leaves the rover stopped and publishes INSPECTION_ERROR_STOPPED on the inspection status topic.

The inspection camera has a fail-stop watchdog. Motor output stays at zero until the first valid image arrives. The node allows up to 15 seconds for that image, then requires a valid image at least once per second by default. If the camera does not start or its stream goes stale, the node publishes zero velocity and latches the stop as CAMERA_TIMEOUT_STOPPED. A returning stream does not restart motion: restore the camera, verify the emergency stop is available, and restart the inspection node after checking the rover. Tune with camera_startup_timeout_sec and camera_timeout_sec if your camera has a different startup or frame rate.

## ROS topics and ESP32 connection

Defaults:

- Inspection camera image input: /camera/image_raw (sensor_msgs/Image). This is Camera B, aimed at the QR and products.
- Navigation velocity input: /cmd_vel_nav (geometry_msgs/Twist)
- Gated motor command output: /cmd_vel_inspected (geometry_msgs/Twist)
- Inspection status output: /inspection/status (std_msgs/String)

The command path must pass through the inspection node: navigation publishes to the node's input topic; this node is the only publisher to the gated output; the ESP32 bridge consumes only /cmd_vel_inspected. Do not let the ESP32 also consume the ungated navigation topic, or the rover could move while this node is publishing zero. The node forwards fresh navigation velocity while healthy and continuously publishes zero during inspection, on stale navigation commands, and on a camera timeout. Change launch arguments to match the topics in your Nav2 setup.

This repository currently has no ESP32 firmware, serial packet format, motor-driver configuration, or micro-ROS client. The ROS node therefore provides the Jetson-side gate, but the ESP32 needs a matching bridge before it can actuate motors. The ESP32 should also have its own command-heartbeat timeout that stops the motors if it stops receiving commands from the Jetson. The actual stop behavior (coast or active brake) depends on the ESP32 firmware and motor driver.

The node expects an existing ROS camera publisher. It does not open a local camera device itself. Camera A can remain assigned to the navigation stack's visual odometry or other navigation sensor node; Camera B must publish to image_topic for inspection. The navigation camera is not consumed directly by this inspection node.

## What ROS does in this system

ROS 2 Humble is the communication and process framework on the Jetson. The installed ROS distribution lives under /opt/ros/humble; this project is built in your workspace (for example ~/ros2_ws/src/warehouse_inspection) and installed under ~/ros2_ws/install. `ros2 launch warehouse_inspection qr_inspection.launch.py` starts the `qr_inspector` node implemented in `warehouse_inspection/ros_node.py`.

ROS is used here to:

- receive Camera B frames as `sensor_msgs/Image`;
- receive navigation velocity as `geometry_msgs/Twist` and publish a gated Twist stream for the motor bridge;
- publish inspection and fault states on `/inspection/status`;
- run the camera subscriber, inspection callbacks, and periodic stop/forward command timer.

ROS does not perform the product categorization or damage classification. Those run inside the Jetson node: `jetson_runtime.py` decodes the QR payload and loads/runs the trained ONNX product-identity and damage models through OpenCV DNN. The node compares the expected item from the QR JSON with the model's observed product class, labels a different supported product as `WRONG_OBJECT`, checks damaged/undamaged condition, then writes the inspection event to SQLite/JSONL. The QR uses the existing JSON field format; `barcode_type` and `raw_barcode` remain only as legacy server field names, populated with `QR_CODE` and the QR text.

SLAM and map creation are separate from this package. A separate ROS SLAM/Nav2 setup creates or loads the map and generates navigation velocity. This package does not launch SLAM, create a map, or select navigation goals; it only inspects the second camera and gates the velocity stream before it reaches the ESP32.

## QR payload compatibility

The QR decoder preserves the payload contract used by the previous QR/barcode generator. Generated QR codes contain compact JSON with these fields:

~~~json
{"item":"lays","type":"Snacks","cost":20,"shelf_id":"SHELF-4"}
~~~

The full QR payload may also contain x, y, sku, and batch_id. The reader accepts both this JSON and the existing compact form:

~~~text
ITEM:lays|TYPE:Snacks|COST:20|SHELF:SHELF-4
~~~

The existing laptop API requires barcode_type and raw_barcode; the event keeps those field names for compatibility, sets the type to QR_CODE, and stores the original QR text as the raw value. The richer inspection result is stored in detection_details, including expected and observed product, product confidence, condition, wrong_object, and the final inspection status.

## Models

- best.onnx: existing two-class damage model (damaged_product, undamaged_product), OpenCV DNN, fixed input 1x3x416x416.
- product_types.onnx: exported from the trained specific_product_damage_model checkpoint as ONNX opset 12, fixed input 1x3x640x640. It has 11 product-condition labels; the inspector removes the _perfect / _damaged suffix to get product identity. product_classes.json contains the class order and known QR item aliases.
- The product model currently recognizes these product families: Cinthol pack of 4, Diet Coca-Cola, Kurkure, Lays, SuperYou protein coffee, YogaBar protein wafer, Mountain Dew, and Pintola protein oats. It was trained with only some product/condition combinations. QR names that do not map to a trained product, such as coca cola can versus the trained diet coca cola class, are reported as EXPECTED_PRODUCT_UNSUPPORTED; they are not guessed as a match.
- If the identity model recognizes a supported but different product, the status is WRONG_OBJECT. If identity matches, condition becomes DAMAGED, OK, or CONDITION_UNKNOWN based on the binary condition model.

Both models run through OpenCV DNN without PyTorch installed on the Jetson. The product model uses 640-pixel input to preserve its training resolution. The binary condition model remains at 416 pixels.

## Install into a ROS 2 workspace

ROS 2 Humble should already be installed and sourced on the Jetson. From the cloned repository:

~~~bash
mkdir -p ~/ros2_ws/src
cp -r ~/Warehouse-Inspection/jetson_deployment ~/ros2_ws/src/warehouse_inspection
cd ~/ros2_ws
source /opt/ros/humble/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
~~~

The included dependency script installs system OpenCV and NumPy only:

~~~bash
cd ~/Warehouse-Inspection/jetson_deployment
bash install_jetson_deps.sh
~~~

On some Jetson images, use the OpenCV package bundled with JetPack instead of replacing it with Ubuntu's package. Keep ROS cv_bridge, OpenCV, and NumPy on compatible system Python packages.

## Start inspection

Start the laptop server separately if you want events synced into the existing warehouse_inventory.db. Set the server URL on the Jetson:

~~~bash
export WAREHOUSE_SERVER_URL=http://<LAPTOP_IP>:8080
# If server authentication is enabled:
export WAREHOUSE_SERVER_TOKEN=<same-token-as-server>
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
ros2 launch warehouse_inspection qr_inspection.launch.py
~~~

Override topic names when needed:

~~~bash
ros2 launch warehouse_inspection qr_inspection.launch.py \
  image_topic:=/camera/color/image_raw \
  nav_cmd_topic:=/cmd_vel_nav \
  motor_cmd_topic:=/cmd_vel_inspected
~~~

For two cameras, replace `/camera/color/image_raw` with the ROS image topic published by the inspection camera. Confirm topic names first with `ros2 topic list` and `ros2 topic info /camera/inspection/image_raw` (use your actual topic). Keep Nav2's command output on `nav_cmd_topic`, and configure the ESP32 bridge to listen only to `motor_cmd_topic`.

The watchdog's default limits can be overridden at launch:

~~~bash
ros2 launch warehouse_inspection qr_inspection.launch.py \
  image_topic:=/camera/inspection/image_raw \
  nav_cmd_topic:=/cmd_vel_nav \
  motor_cmd_topic:=/cmd_vel_inspected \
  camera_startup_timeout_sec:=20.0 \
  camera_timeout_sec:=1.5
~~~

Inspect status and logs:

~~~bash
ros2 topic echo /inspection/status
tail -f ~/.local/state/warehouse_inspection/inspection_events.jsonl
~~~

The SQLite upload outbox is stored at ~/.local/state/warehouse_inspection/inspection_outbox.sqlite3.

## Hardware stop test

The physical rover has not been tested by this repository. Before the first powered run, raise the drive wheels or restrain the rover, keep a reachable hardware emergency stop, and verify that the motor bridge consumes only `/cmd_vel_inspected`. With the wheels restrained, verify that a nonzero Nav2 command reaches the bridge while the inspection camera is fresh; a QR detection outputs zero; unplugging or disabling Camera B results in `CAMERA_TIMEOUT_STOPPED` and zero output after the configured timeout; restoring the camera does not resume motion until the node is deliberately restarted; and the ESP32 heartbeat timeout stops the motors if Jetson commands cease. Confirm actual motor stop/braking behavior with the emergency stop ready before floor testing.

On the Orin Nano, verify the exact installed OS and architecture (`lsb_release -a`, `uname -m`, and `echo $ROS_DISTRO`) before building. Since Humble is already installed on your board, keep sourcing `/opt/ros/humble/setup.bash` and use the existing JetPack/OpenCV/cv_bridge packages rather than replacing them with an incompatible Python/OpenCV installation.
