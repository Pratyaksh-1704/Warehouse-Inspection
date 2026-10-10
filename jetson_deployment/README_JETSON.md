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

## ROS topics and ESP32 connection

Defaults:

- Camera image input: /camera/image_raw (sensor_msgs/Image)
- Navigation velocity input: /cmd_vel_nav (geometry_msgs/Twist)
- Motor/ESP32 velocity output: /cmd_vel (geometry_msgs/Twist)
- Inspection status output: /inspection/status (std_msgs/String)

Route the navigation stack's velocity output to /cmd_vel_nav. The ESP32 motor driver (or its ROS bridge) must consume /cmd_vel. The node republishes the latest navigation command while moving and continuously publishes a zero Twist while scanning and inspecting. If your ESP32 bridge uses other topic names or message types, connect it to /cmd_vel through the matching bridge or change the launch arguments. This repo contains the ROS topic gate; it does not contain firmware for the ESP32.

The node expects an existing ROS camera publisher. It does not open a local camera device itself. Change image_topic to the image topic already used by your camera node.

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
  nav_cmd_topic:=/controller/cmd_vel \
  motor_cmd_topic:=/esp32/cmd_vel
~~~

Inspect status and logs:

~~~bash
ros2 topic echo /inspection/status
tail -f ~/.local/state/warehouse_inspection/inspection_events.jsonl
~~~

The SQLite upload outbox is stored at ~/.local/state/warehouse_inspection/inspection_outbox.sqlite3.

## Jetson / ROS 2 platform note

ROS 2 Humble's Tier 1 Ubuntu target is Ubuntu 22.04 (Jammy) on ARM64 ([ROS 2 Humble release notes](https://www.openrobotics.org/blog/2022/5/24/ros-2-humble-hawksbill-release)). NVIDIA's Jetson Nano / JetPack 4.6.6 image is based on Ubuntu 18.04 ([Jetson Linux R32.7.6](https://developer.nvidia.com/embedded/linux-tegra-r3276)), so the stock Nano image is not a supported Humble platform. Confirm the Jetson model and OS/JetPack before deployment; this repository exports ONNX for OpenCV DNN but cannot make an incompatible ROS/OS image compatible.
