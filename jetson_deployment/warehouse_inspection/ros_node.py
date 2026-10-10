"""ROS 2 node that gates rover motion and inspects products at QR locations."""

import copy
import json
import math
import os
import re
import threading
import time

import cv2
import rclpy
from ament_index_python.packages import get_package_share_directory
from cv_bridge import CvBridge
from geometry_msgs.msg import Twist
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import String

from jetson_runtime import (
    DamageDetector,
    InspectionOutbox,
    append_jsonl,
    decode_qr_codes,
    qr_visible,
)


def _source_or_installed_path(relative_path):
    try:
        share = get_package_share_directory("warehouse_inspection")
        installed = os.path.join(share, relative_path)
        if os.path.exists(installed):
            return installed
    except Exception:
        pass
    deployment_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(deployment_dir, os.path.basename(relative_path))


def _normalise_name(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _product_name_from_label(label):
    value = str(label or "").strip().lower()
    for suffix in ("_perfect", "_damaged"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


def _zero_twist():
    return Twist()


class QrStopAndInspectNode(Node):
    """Pause the motor command stream while a QR location is inspected."""

    def __init__(self):
        super().__init__("qr_stop_and_inspect")
        callback_group = ReentrantCallbackGroup()
        state_dir = os.path.expanduser("~/.local/state/warehouse_inspection")
        os.makedirs(state_dir, exist_ok=True)

        self.declare_parameter("image_topic", "/camera/image_raw")
        self.declare_parameter("nav_cmd_topic", "/cmd_vel_nav")
        self.declare_parameter("motor_cmd_topic", "/cmd_vel")
        self.declare_parameter("status_topic", "/inspection/status")
        self.declare_parameter("product_model", _source_or_installed_path("models/product_types.onnx"))
        self.declare_parameter("damage_model", _source_or_installed_path("models/best.onnx"))
        self.declare_parameter("product_classes", _source_or_installed_path("models/product_classes.json"))
        self.declare_parameter("server_url", os.environ.get("WAREHOUSE_SERVER_URL", ""))
        self.declare_parameter("server_token", os.environ.get("WAREHOUSE_SERVER_TOKEN", ""))
        self.declare_parameter("outbox_path", os.path.join(state_dir, "inspection_outbox.sqlite3"))
        self.declare_parameter("log_path", os.path.join(state_dir, "inspection_events.jsonl"))
        self.declare_parameter("mission_id", "MISSION_01")
        self.declare_parameter("device_id", "jetson-edge-01")
        self.declare_parameter("product_confidence", 0.25)
        self.declare_parameter("damage_confidence", 0.25)
        self.declare_parameter("qr_settle_seconds", 0.6)
        self.declare_parameter("qr_clear_frames", 5)
        self.declare_parameter("cmd_vel_rate", 20.0)
        self.declare_parameter("cmd_vel_timeout", 0.5)

        self.image_topic = str(self.get_parameter("image_topic").value)
        self.nav_cmd_topic = str(self.get_parameter("nav_cmd_topic").value)
        self.motor_cmd_topic = str(self.get_parameter("motor_cmd_topic").value)
        self.status_topic = str(self.get_parameter("status_topic").value)
        self.log_path = str(self.get_parameter("log_path").value)
        self.mission_id = str(self.get_parameter("mission_id").value)
        self.device_id = str(self.get_parameter("device_id").value)
        self.qr_settle_seconds = float(self.get_parameter("qr_settle_seconds").value)
        self.qr_clear_frames = max(1, int(self.get_parameter("qr_clear_frames").value))
        self.cmd_vel_timeout = float(self.get_parameter("cmd_vel_timeout").value)

        with open(str(self.get_parameter("product_classes").value), "r", encoding="utf-8") as stream:
            class_config = json.load(stream)
        class_names = class_config["class_names"]
        product_aliases = class_config.get("qr_item_aliases", {})
        self.product_aliases = {
            _normalise_name(expected): _normalise_name(model_name)
            for expected, model_name in product_aliases.items()
        }
        self.known_products = {
            _normalise_name(_product_name_from_label(label)) for label in class_names
        }

        self.product_detector = DamageDetector(
            str(self.get_parameter("product_model").value),
            image_size=int(class_config.get("input_size", 640)),
            confidence=float(self.get_parameter("product_confidence").value),
            backend="auto",
            class_names=class_names,
        )
        self.condition_detector = DamageDetector(
            str(self.get_parameter("damage_model").value),
            image_size=416,
            confidence=float(self.get_parameter("damage_confidence").value),
            backend="auto",
            class_names=["damaged_product", "undamaged_product"],
        )

        self.outbox = InspectionOutbox(
            str(self.get_parameter("outbox_path").value),
            str(self.get_parameter("server_url").value),
            str(self.get_parameter("server_token").value),
        )
        self.outbox.start()
        if self.outbox.server_url:
            self.get_logger().info("Inspection events will sync to %s" % self.outbox.server_url)
        else:
            self.get_logger().info("No server URL set; events will remain in the local SQLite outbox")

        self.bridge = CvBridge()
        self._lock = threading.RLock()
        self._inspection_active = False
        self._processing = False
        self._inspection_error = False
        self._active_since = 0.0
        self._pending_qr = None
        self._latched_payload = None
        self._clear_frames = 0
        self._last_nav_cmd = None
        self._last_nav_cmd_time = 0.0
        self._worker_thread = None

        self._status_pub = self.create_publisher(String, self.status_topic, 10)
        self._motor_pub = self.create_publisher(Twist, self.motor_cmd_topic, 10)
        self.create_subscription(
            Image, self.image_topic, self._image_callback, 5, callback_group=callback_group
        )
        self.create_subscription(
            Twist, self.nav_cmd_topic, self._nav_cmd_callback, 10, callback_group=callback_group
        )
        cmd_period = 1.0 / max(1.0, float(self.get_parameter("cmd_vel_rate").value))
        self.create_timer(cmd_period, self._publish_gated_command, callback_group=callback_group)

        self.get_logger().info(
            "QR inspection ready: image=%s nav=%s motor=%s" %
            (self.image_topic, self.nav_cmd_topic, self.motor_cmd_topic)
        )

    def _publish_status(self, value):
        message = String()
        message.data = str(value)
        self._status_pub.publish(message)

    def _nav_cmd_callback(self, message):
        with self._lock:
            self._last_nav_cmd = copy.deepcopy(message)
            self._last_nav_cmd_time = time.monotonic()

    def _publish_gated_command(self):
        with self._lock:
            stopped = self._inspection_active or self._inspection_error
            command = copy.deepcopy(self._last_nav_cmd)
            command_time = self._last_nav_cmd_time
        if stopped or command is None or time.monotonic() - command_time > self.cmd_vel_timeout:
            self._motor_pub.publish(_zero_twist())
        else:
            self._motor_pub.publish(command)

    @staticmethod
    def _largest_qr(detections):
        if not detections:
            return None
        return max(
            detections,
            key=lambda detection: max(0, detection["bbox"][2] - detection["bbox"][0]) *
            max(0, detection["bbox"][3] - detection["bbox"][1]),
        )

    def _image_callback(self, message):
        try:
            frame = self.bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
        except Exception as exc:
            self.get_logger().warning("Could not convert camera image: %s" % exc)
            return

        visible = qr_visible(frame)
        qr_detections = decode_qr_codes(frame) if visible else []
        qr = self._largest_qr(qr_detections)
        payload = qr["raw_payload"] if qr else None
        now = time.monotonic()
        work = None

        with self._lock:
            if visible:
                self._clear_frames = 0
                if self._processing or self._inspection_error:
                    return
                if self._latched_payload is not None:
                    if payload is None or payload == self._latched_payload:
                        return
                    self._latched_payload = None
                if not self._inspection_active:
                    self._inspection_active = True
                    self._active_since = now
                    self._pending_qr = None
                    self._publish_status("STOPPING_FOR_QR")
                    self.get_logger().info("QR visible; stopping rover before product inspection")
                if qr is not None:
                    self._pending_qr = qr
                if self._pending_qr is not None and now - self._active_since >= self.qr_settle_seconds:
                    self._processing = True
                    work = (frame.copy(), copy.deepcopy(self._pending_qr))
                    self._publish_status("STOPPED_INSPECTING")
            else:
                self._clear_frames += 1
                if self._clear_frames >= self.qr_clear_frames:
                    if self._inspection_active and not self._processing:
                        self._inspection_active = False
                        self._pending_qr = None
                        self._publish_status("QR_LOST_BEFORE_DECODE")
                        self.get_logger().warning("QR left camera view before it could be decoded; rover resumed")
                    if not self._inspection_active:
                        self._latched_payload = None

        if work is not None:
            worker = threading.Thread(target=self._inspect_worker, args=work, name="qr-product-inspection")
            worker.daemon = True
            with self._lock:
                self._worker_thread = worker
            worker.start()

    def _select_product_detection(self, detections, qr_bbox, frame_shape):
        if not detections:
            return None
        frame_height, frame_width = frame_shape[:2]
        qr_x1, qr_y1, qr_x2, qr_y2 = qr_bbox
        qr_center_x = (qr_x1 + qr_x2) / 2.0
        qr_lower_y = qr_y2

        def rank(detection):
            x1, y1, x2, y2 = detection["bbox"]
            center_x = (x1 + x2) / 2.0
            center_y = (y1 + y2) / 2.0
            dx = (center_x - qr_center_x) / max(1.0, float(frame_width))
            dy = (center_y - qr_lower_y) / max(1.0, float(frame_height))
            distance = math.sqrt(dx * dx + dy * dy)
            above_qr_penalty = max(0.0, qr_lower_y - center_y) / max(1.0, float(frame_height))
            return detection["confidence"] - 0.25 * distance - 0.25 * above_qr_penalty

        return max(detections, key=rank)

    @staticmethod
    def _crop_detection(frame, detection):
        if detection is None:
            return frame
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = detection["bbox"]
        margin_x = max(2, int((x2 - x1) * 0.04))
        margin_y = max(2, int((y2 - y1) * 0.04))
        x1 = max(0, x1 - margin_x)
        y1 = max(0, y1 - margin_y)
        x2 = min(width, x2 + margin_x)
        y2 = min(height, y2 + margin_y)
        crop = frame[y1:y2, x1:x2]
        return crop if crop.size else frame

    def _inspect_worker(self, frame, qr_detection):
        try:
            report = self._build_report(frame, qr_detection)
            event_id = self.outbox.enqueue(report)
            try:
                append_jsonl(self.log_path, report)
            except Exception as exc:
                self.get_logger().warning("JSONL audit log failed; event remains in SQLite: %s" % exc)

            details = report["detection_details"]
            self.get_logger().info(
                "Inspection %s: expected=%s observed=%s condition=%s event=%s" %
                (report["inspection_status"], details["expected_product"],
                 details["observed_product"], details["condition_status"], event_id)
            )
            self._publish_status(report["inspection_status"])
            with self._lock:
                self._latched_payload = qr_detection["raw_payload"]
                self._inspection_active = False
                self._processing = False
                self._inspection_error = False
                self._pending_qr = None
        except Exception as exc:
            self.get_logger().error("Inspection failed; rover remains stopped: %s" % exc)
            with self._lock:
                self._processing = False
                self._inspection_error = True
            self._publish_status("INSPECTION_ERROR_STOPPED")

    def _build_report(self, frame, qr_detection):
        qr_data = qr_detection["data"]
        expected_product = str(qr_data.get("item", "Unknown Item")).strip()

        product_detections = self.product_detector.predict(frame)
        selected_product = self._select_product_detection(
            product_detections, qr_detection["bbox"], frame.shape
        )
        observed_class = selected_product["label"] if selected_product else ""
        observed_base = _product_name_from_label(observed_class)
        observed_slug = _normalise_name(observed_base)
        observed_product = observed_base.replace("_", " ").strip().title() if observed_base else ""
        product_confidence = selected_product["confidence"] if selected_product else 0.0

        expected_slug = self.product_aliases.get(
            _normalise_name(expected_product), _normalise_name(expected_product)
        )
        expected_supported = expected_slug in self.known_products
        if expected_product in ("", "Unknown Item") or expected_slug == "unknownitem":
            validation_status = "INVALID_QR_PAYLOAD"
        elif not expected_supported:
            validation_status = "EXPECTED_PRODUCT_UNSUPPORTED"
        elif not observed_product:
            validation_status = "PRODUCT_NOT_RECOGNIZED"
        elif observed_slug == expected_slug:
            validation_status = "MATCH"
        else:
            validation_status = "WRONG_OBJECT"

        crop = self._crop_detection(frame, selected_product)
        condition_detections = self.condition_detector.predict(crop)
        condition_detection = max(
            condition_detections, key=lambda detection: detection["confidence"]
        ) if condition_detections else None
        condition_label = condition_detection["label"] if condition_detection else ""
        if "damaged" in condition_label.lower():
            is_damaged = True
            condition_status = "DAMAGED"
        elif "undamaged" in condition_label.lower():
            is_damaged = False
            condition_status = "UNDAMAGED"
        else:
            is_damaged = False
            condition_status = "UNKNOWN"

        wrong_object = validation_status == "WRONG_OBJECT"
        if wrong_object:
            inspection_status = "WRONG_OBJECT"
            severity = "HIGH"
            action_required = "REMOVE_WRONG_OBJECT"
        elif validation_status != "MATCH":
            inspection_status = validation_status
            severity = "NONE"
            action_required = "MANUAL_INSPECTION"
        elif condition_status == "UNKNOWN":
            inspection_status = "CONDITION_UNKNOWN"
            severity = "NONE"
            action_required = "MANUAL_INSPECTION"
        elif is_damaged:
            inspection_status = "DAMAGED"
            severity = condition_detection["severity"]
            action_required = condition_detection["action"]
        else:
            inspection_status = "OK"
            severity = "LOW"
            action_required = "NONE"

        product_bbox = selected_product["bbox"] if selected_product else None
        details = {
            "source": "ros2_camera",
            "device_id": self.device_id,
            "inspection_status": inspection_status,
            "validation_status": validation_status,
            "expected_product": expected_product,
            "observed_product": observed_product or None,
            "observed_product_class": observed_class or None,
            "product_confidence": round(float(product_confidence), 4),
            "condition_status": condition_status,
            "condition_label": condition_label or None,
            "condition_confidence": round(float(condition_detection["confidence"]), 4)
            if condition_detection else 0.0,
            "wrong_object": wrong_object,
            "qr_payload": qr_detection["raw_payload"],
            "qr_bbox": qr_detection["bbox"],
            "product_bbox": product_bbox,
            "product_detection_count": len(product_detections),
            "condition_detection_count": len(condition_detections),
        }
        return {
            "device_id": self.device_id,
            "mission_id": self.mission_id,
            "timestamp": time.time(),
            "item": expected_product,
            "product_type": qr_data.get("product_type", "Unknown"),
            "cost": float(qr_data.get("cost", 0.0)),
            "shelf_id": qr_data.get("shelf_id", "SHELF-UNKNOWN"),
            "sku": qr_data.get("sku"),
            "batch_id": qr_data.get("batch_id"),
            "x": float(qr_data.get("x", 0.0)),
            "y": float(qr_data.get("y", 0.0)),
            "is_damaged": bool(is_damaged),
            "condition_status": condition_status,
            "inspection_status": inspection_status,
            "expected_product": expected_product,
            "observed_product": observed_product or None,
            "wrong_object": wrong_object,
            "damage_label": condition_label or "condition_unknown",
            "damage_confidence": details["condition_confidence"],
            "severity": severity,
            "action_required": action_required,
            # The existing laptop API expects these two legacy keys; the values are QR data.
            "barcode_type": "QR_CODE",
            "raw_barcode": qr_detection["raw_payload"],
            "raw_qr_payload": qr_detection["raw_payload"],
            "detection_details": details,
        }

    def stop_and_close(self):
        self._motor_pub.publish(_zero_twist())
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=10.0)
        self.outbox.close()


def main(args=None):
    rclpy.init(args=args)
    node = QrStopAndInspectNode()
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_and_close()
        executor.remove_node(node)
        node.destroy_node()
        rclpy.shutdown()
