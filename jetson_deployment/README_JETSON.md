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

`esp32_serial_bridge` is the Jetson-side USB serial transport. `rover_inspection.launch.py` starts it with the QR inspection node. The bridge reads only `/cmd_vel_inspected` and sends versioned ASCII frames at 20 Hz using `CMD1,<sequence>,<linear_mm_per_s>,<angular_mrad_per_s>\n` at 115200 baud by default. It sends zero velocity if that ROS command stream goes stale. The ESP32 firmware must parse this same frame and must independently stop the L298N if serial frames stop arriving. This repository does not contain the ESP32 firmware or your board's GPIO pin map yet, so the serial transport is not a complete motor controller until that matching firmware is flashed.

For a four-wheel skid-steer rover with one L298N, the intended wiring is the two left motors on one bridge channel and the two right motors on the other. Confirm the motors are already grouped that way and that their combined current is within the driver's limits. The ESP32 should keep the ultrasonic obstacle stop local: a close obstacle overrides any nonzero Jetson command. Put the ultrasonic sensor facing forward while the rover is driving; the ESP32 should control the servo if it sweeps the sensor. An obstacle stop should not depend on ROS or the Jetson.

The node expects an existing ROS camera publisher. It does not open a local camera device itself. Camera A can remain assigned to the navigation stack's visual odometry or other navigation sensor node; Camera B must publish to image_topic for inspection. The navigation camera is not consumed directly by this inspection node.

## What ROS does in this system

ROS 2 Humble is the communication and process framework on the Jetson. The installed ROS distribution lives under /opt/ros/humble; this project is built in your workspace (for example ~/ros2_ws/src/warehouse_inspection) and installed under ~/ros2_ws/install. `ros2 launch warehouse_inspection rover_inspection.launch.py` starts `qr_inspector` from `warehouse_inspection/ros_node.py` and `esp32_serial_bridge` from `warehouse_inspection/serial_bridge.py`.

ROS is used here to:

- receive Camera B frames as `sensor_msgs/Image`;
- receive navigation velocity as `geometry_msgs/Twist` and publish a gated Twist stream for the motor bridge;
- transfer that gated velocity over USB serial to the ESP32;
- publish inspection and fault states on `/inspection/status`;
- run the camera subscriber, inspection callbacks, and periodic stop/forward command timer.

ROS does not perform the product categorization or damage classification. Those run inside the Jetson node: `jetson_runtime.py` decodes the QR payload and loads/runs the trained ONNX product-identity and damage models through OpenCV DNN. The node compares the expected item from the QR JSON with the model's observed product class, labels a different supported product as `WRONG_OBJECT`, checks damaged/undamaged condition, then writes the inspection event to SQLite/JSONL. The QR uses the existing JSON field format; `barcode_type` and `raw_barcode` remain only as legacy server field names, populated with `QR_CODE` and the QR text.

SLAM and map creation are separate from this package. After mapping, localization and Nav2 must load the saved map and generate navigation velocity from goals. Camera A is used by the navigation stack only if that stack is configured to use it; merely having the mapped camera does not make the rover follow the map. This package does not create maps, localize the rover, or select goals. It inspects Camera B, gates Nav2 velocity, and forwards the result to the ESP32. The ESP32's ultrasonic obstacle stop overrides that command locally.

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

`rosdep` installs the Python serial dependency. After connecting the ESP32 to a Jetson USB port, identify the device with `ls /dev/ttyUSB* /dev/ttyACM*` and pass the resulting path as `serial_port` when launching.

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
ros2 launch warehouse_inspection rover_inspection.launch.py \
  image_topic:=/camera/inspection/image_raw \
  nav_cmd_topic:=/cmd_vel_nav \
  motor_cmd_topic:=/cmd_vel_inspected \
  serial_port:=/dev/ttyUSB0
~~~

Override topic names when needed:

~~~bash
ros2 launch warehouse_inspection rover_inspection.launch.py \
  image_topic:=/camera/color/image_raw \
  nav_cmd_topic:=/cmd_vel_nav \
  motor_cmd_topic:=/cmd_vel_inspected
~~~

For two cameras, use Camera B's ROS image topic as `image_topic`. Confirm topic names first with `ros2 topic list` and `ros2 topic info /camera/inspection/image_raw` (replace with your actual topic). Keep Nav2's command output on `nav_cmd_topic`; the serial bridge consumes only `motor_cmd_topic`.

The watchdog's default limits can be overridden at launch:

~~~bash
ros2 launch warehouse_inspection rover_inspection.launch.py \
  image_topic:=/camera/inspection/image_raw \
  nav_cmd_topic:=/cmd_vel_nav \
  motor_cmd_topic:=/cmd_vel_inspected \
  camera_startup_timeout_sec:=20.0 \
  camera_timeout_sec:=1.5
~~~

Inspect status and logs:

~~~bash
ros2 topic echo /inspection/status
ros2 topic echo /rover/esp32_link_status
tail -f ~/.local/state/warehouse_inspection/inspection_events.jsonl
~~~

The SQLite upload outbox is stored at ~/.local/state/warehouse_inspection/inspection_outbox.sqlite3.

## Hardware stop test

The physical rover has not been tested by this repository. Before the first powered run, raise the drive wheels or restrain the rover and keep a reachable hardware emergency stop. Verify the L298N is driven only by ESP32 firmware, and that firmware receives only the Jetson's gated serial frames. Check that a fresh camera plus nonzero Nav2 command reaches the ESP32, a QR or camera timeout sends zero velocity, an obstacle makes the ESP32 stop locally, and loss of serial frames triggers the ESP32 watchdog. Restoring the camera or serial link must not restart movement without fresh navigation commands. Confirm stop behavior with the emergency stop ready before floor testing.

You said this board is a Jetson Nano. The original Nano's official Jetson Linux R32.7.5 image is based on Ubuntu 18.04; JetPack 6 and Ubuntu 22.04 target Orin devices. ROS 2 Humble's standard Ubuntu target is Jammy 22.04, so the stock original Nano image is not a supported Humble platform. [NVIDIA Nano release](https://developer.nvidia.com/embedded/linux-tegra-r3275), [NVIDIA JetPack 6](https://developer.nvidia.com/embedded/jetpack-sdk-621), [ROS 2 Humble supported platforms](https://docs.ros.org/en/humble/Releases/Release-Humble-Hawksbill.html#supported-platforms). Before building, check `lsb_release -a`, `uname -m`, `echo $ROS_DISTRO`, and `head -n 1 /etc/nv_tegra_release`; if Humble is installed through a custom image/container, verify this package and camera drivers there.
