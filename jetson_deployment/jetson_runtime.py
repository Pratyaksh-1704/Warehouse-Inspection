"""Shared QR, camera, ONNX inference, and store-and-forward helpers."""

import json
import os
import sqlite3
import threading
import time
import uuid
import urllib.request

import cv2
import numpy as np


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_qr_payload(payload):
    """Parse the existing warehouse QR JSON or ITEM|TYPE|COST|SHELF payload."""
    text = (payload or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            source = json.loads(text)
            return {
                "item": str(source.get("item", source.get("product_name", source.get("name", "Unknown Item")))),
                "product_type": str(source.get("type", source.get("product_type", "General"))),
                "cost": _as_float(source.get("cost", source.get("price", source.get("cost_in_rupees", 0.0)))),
                "shelf_id": str(source.get("shelf_id", source.get("self_id", source.get("shelf", "SHELF-UNKNOWN")))),
                "x": _as_float(source.get("x", 0.0)),
                "y": _as_float(source.get("y", 0.0)),
                "sku": source.get("sku"),
                "batch_id": source.get("batch_id"),
            }
        except Exception:
            pass

    delimiter = "|" if "|" in text else (";" if ";" in text else ",")
    fields = {}
    for token in text.split(delimiter):
        token = token.strip()
        separator = ":" if ":" in token else ("=" if "=" in token else None)
        if separator:
            key, value = token.split(separator, 1)
            fields[key.strip().upper()] = value.strip()

    cost_text = fields.get("COST", fields.get("PRICE", fields.get("COST_INR", "0")))
    cost_text = cost_text.replace("₹", "").replace("RS.", "").replace("RS", "").strip()
    return {
        "item": fields.get("ITEM", fields.get("PRODUCT", fields.get("NAME", "Unknown Item"))),
        "product_type": fields.get("TYPE", fields.get("PRODUCT_TYPE", fields.get("PROD", "General"))),
        "cost": _as_float(cost_text),
        "shelf_id": fields.get("SHELF", fields.get("SHELF_ID", fields.get("SELF_ID", "SHELF-UNKNOWN"))),
        "x": _as_float(fields.get("X", fields.get("X-COORD", fields.get("X_COORD", 0.0)))),
        "y": _as_float(fields.get("Y", fields.get("Y-COORD", fields.get("Y_COORD", 0.0)))),
        "sku": fields.get("SKU", fields.get("ID", fields.get("PRODUCT_ID"))),
        "batch_id": fields.get("BATCH", fields.get("BATCH_ID")),
    }


def qr_event(detection, mission_id):
    data = detection["data"]
    return {
        "timestamp": time.time(),
        "formatted_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mission_id": mission_id,
        "rover_pose": {"x": data["x"], "y": data["y"], "heading_deg": 0.0},
        "shelf_id": data["shelf_id"],
        "product_id": data["sku"] or data["item"],
        "item": data["item"],
        "product_type": data["product_type"],
        "cost": data["cost"],
        "currency": "INR (Rs.)",
        "qr_code_type": "QR_CODE",
        "raw_qr_payload": detection["raw_payload"],
        # Preserve the existing server/database fields for payload compatibility.
        "barcode_type": "QR_CODE",
        "raw_barcode": detection["raw_payload"],
    }


def _make_qr_detection(payload, polygon, frame):
    height, width = frame.shape[:2]
    points = [(int(point[0]), int(point[1])) for point in polygon]
    if points:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x1 = max(0, min(xs))
        y1 = max(0, min(ys))
        x2 = min(width - 1, max(xs))
        y2 = min(height - 1, max(ys))
    else:
        x1, y1, x2, y2 = 0, 0, width - 1, height - 1
    return {
        "raw_payload": payload,
        "qr_code_type": "QR_CODE",
        "data": parse_qr_payload(payload),
        "polygon": points,
        "bbox": [x1, y1, x2, y2],
    }


def qr_visible(frame):
    """Return true when OpenCV sees a QR quadrilateral, even before decoding it."""
    if frame is None or frame.size == 0:
        return False
    detector = cv2.QRCodeDetector()
    try:
        found, points = detector.detectMulti(frame)
        if found and points is not None:
            return True
    except Exception:
        pass
    try:
        found, points = detector.detect(frame)
        return bool(found and points is not None)
    except Exception:
        return False


def decode_qr_codes(frame):
    """Decode QR payloads using OpenCV only."""
    if frame is None or frame.size == 0:
        return []
    detector = cv2.QRCodeDetector()
    try:
        ok, texts, points, _ = detector.detectAndDecodeMulti(frame)
        if ok and texts is not None and points is not None:
            found = []
            for text, point_array in zip(texts, points):
                text = (text or "").strip()
                if text:
                    polygon = [(point[0], point[1]) for point in point_array]
                    found.append(_make_qr_detection(text, polygon, frame))
            if found:
                return found
    except Exception:
        pass
    try:
        text, points, _ = detector.detectAndDecode(frame)
        if text:
            polygon = [(point[0], point[1]) for point in points.reshape(-1, 2)] if points is not None else []
            return [_make_qr_detection(text, polygon, frame)]
    except Exception:
        pass
    return []


def draw_qr_overlay(frame, detections, fps=None, status="SCANNING"):
    canvas = frame.copy()
    height, width = canvas.shape[:2]
    cv2.rectangle(canvas, (0, 0), (width, 42), (24, 30, 38), -1)
    title = "WAREHOUSE QR INSPECTION | {} QR code(s) | {}".format(len(detections), status)
    if fps is not None:
        title += " | {:.1f} FPS".format(fps)
    cv2.putText(canvas, title, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 205), 2, cv2.LINE_AA)

    for detection in detections:
        x1, y1, x2, y2 = detection["bbox"]
        polygon = detection["polygon"]
        if len(polygon) >= 4:
            points = np.array(polygon, dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(canvas, [points], True, (0, 220, 80), 2)
        else:
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 220, 80), 2)
        data = detection["data"]
        label = "{} | {} | Rs.{:.2f} | {}".format(
            data["item"], data["product_type"], data["cost"], data["shelf_id"]
        )
        y_text = max(60, y1 - 8)
        cv2.putText(canvas, label[:96], (max(6, x1), y_text), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 2, cv2.LINE_AA)
    return canvas


class DamageDetector:
    """OpenCV DNN runner for a fixed-size YOLOv8 ONNX export (no PyTorch required)."""

    def __init__(self, model_path, image_size=416, confidence=0.25, nms_threshold=0.45,
                 backend="auto", class_names=None):
        if not os.path.isfile(model_path):
            raise IOError("ONNX model not found: {}".format(model_path))
        self.image_size = int(image_size)
        self.confidence = float(confidence)
        self.nms_threshold = float(nms_threshold)
        self.class_names = class_names or ["damaged_product", "undamaged_product"]
        self.net = cv2.dnn.readNetFromONNX(model_path)
        self.active_backend = "CPU"
        if backend in ("auto", "cuda"):
            try:
                self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
                self.active_backend = "CUDA"
            except Exception as exc:
                if backend == "cuda":
                    raise RuntimeError("OpenCV CUDA DNN is unavailable: {}".format(exc))
                self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        print("[damage] OpenCV DNN backend: {}".format(self.active_backend))

    def _forward(self, blob):
        self.net.setInput(blob)
        try:
            output = self.net.forward()
        except Exception:
            if self.active_backend != "CUDA":
                raise
            print("[damage] CUDA inference failed; switching to CPU DNN.")
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.active_backend = "CPU"
            self.net.setInput(blob)
            output = self.net.forward()
        if isinstance(output, (tuple, list)):
            output = output[0]
        return np.asarray(output)

    def predict(self, frame):
        height, width = frame.shape[:2]
        side = self.image_size
        scale = min(float(side) / width, float(side) / height)
        resized_width = int(round(width * scale))
        resized_height = int(round(height * scale))
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        pad_x = (side - resized_width) // 2
        pad_y = (side - resized_height) // 2
        letterboxed = np.full((side, side, 3), 114, dtype=np.uint8)
        letterboxed[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = resized
        blob = cv2.dnn.blobFromImage(letterboxed, 1.0 / 255.0, (side, side), swapRB=True, crop=False)
        output = self._forward(blob)

        if output.ndim == 3:
            output = output[0]
        if output.ndim != 2:
            raise RuntimeError("Unexpected ONNX output shape: {}".format(output.shape))
        if output.shape[0] <= 256 and output.shape[1] > output.shape[0]:
            rows = output.transpose()
        else:
            rows = output

        rows = rows.astype(np.float32, copy=False)
        candidate_boxes = {}
        candidate_scores = {}
        for row in rows:
            if row.shape[0] < 5:
                continue
            class_scores = row[4:]
            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id])
            if score < self.confidence:
                continue
            center_x, center_y, box_width, box_height = [float(value) for value in row[:4]]
            x1 = int(round((center_x - box_width / 2.0 - pad_x) / scale))
            y1 = int(round((center_y - box_height / 2.0 - pad_y) / scale))
            x2 = int(round((center_x + box_width / 2.0 - pad_x) / scale))
            y2 = int(round((center_y + box_height / 2.0 - pad_y) / scale))
            x1 = max(0, min(width - 1, x1))
            y1 = max(0, min(height - 1, y1))
            x2 = max(0, min(width - 1, x2))
            y2 = max(0, min(height - 1, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            candidate_boxes.setdefault(class_id, []).append([x1, y1, x2 - x1, y2 - y1])
            candidate_scores.setdefault(class_id, []).append(score)

        detections = []
        for class_id, boxes in candidate_boxes.items():
            scores = candidate_scores[class_id]
            kept = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, self.nms_threshold)
            if kept is None or len(kept) == 0:
                continue
            for index in np.asarray(kept).reshape(-1):
                index = int(index)
                x, y, box_width, box_height = boxes[index]
                label = self.class_names[class_id] if class_id < len(self.class_names) else "class_{}".format(class_id)
                is_defect = "undamaged" not in label.lower() and "perfect" not in label.lower()
                box_area = box_width * box_height
                area_ratio = float(box_area) / float(max(1, width * height))
                score = scores[index]
                if not is_defect:
                    severity, action = "LOW", "ACCEPTABLE"
                elif area_ratio > 0.15 or score > 0.75:
                    severity, action = "HIGH", "FLAG_FOR_REMOVAL"
                elif area_ratio > 0.05 or score > 0.50:
                    severity, action = "MEDIUM", "FLAG_FOR_REMOVAL"
                else:
                    severity, action = "LOW", "MONITOR"
                detections.append({
                    "label": label,
                    "confidence": round(score, 4),
                    "bbox": [x, y, x + box_width, y + box_height],
                    "is_defect": is_defect,
                    "severity": severity,
                    "action": action,
                })
        return detections


def damage_event(detections, source="camera"):
    is_damaged = any(detection["is_defect"] for detection in detections)
    if is_damaged:
        status = "DAMAGED_FLAGGED"
    elif detections:
        status = "VERIFIED_OK"
    else:
        status = "NO_DAMAGE_DETECTED"
    return {
        "timestamp": time.time(),
        "formatted_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "is_damaged": is_damaged,
        "status": status,
        "detections_count": len(detections),
        "detections": detections,
    }


def draw_damage_overlay(frame, detections, fps=None):
    canvas = frame.copy()
    height, width = canvas.shape[:2]
    is_damaged = any(detection["is_defect"] for detection in detections)
    color = (0, 0, 210) if is_damaged else (0, 145, 0)
    status = "DAMAGED PRODUCT" if is_damaged else ("UNDAMAGED PRODUCT" if detections else "SCANNING")
    cv2.rectangle(canvas, (0, 0), (width, 38), color, -1)
    title = "{} | detections: {}".format(status, len(detections))
    if fps is not None:
        title += " | {:.1f} FPS".format(fps)
    cv2.putText(canvas, title, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.64, (255, 255, 255), 2, cv2.LINE_AA)
    for detection in detections:
        x1, y1, x2, y2 = detection["bbox"]
        box_color = (0, 0, 230) if detection["is_defect"] else (0, 200, 0)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), box_color, 2)
        label = "{} {:.2f} [{}]".format(detection["label"], detection["confidence"], detection["severity"])
        cv2.putText(canvas, label, (x1, max(52, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2, cv2.LINE_AA)
    return canvas


def gstreamer_csi_pipeline(sensor_id=0, capture_width=1280, capture_height=720,
                           display_width=640, display_height=480, framerate=30, flip_method=0):
    return (
        "nvarguscamerasrc sensor-id={} ! ".format(sensor_id) +
        "video/x-raw(memory:NVMM), width=(int){}, height=(int){}, format=(string)NV12, framerate=(fraction){}/1 ! ".format(
            capture_width, capture_height, framerate
        ) +
        "nvvidconv flip-method={} ! ".format(flip_method) +
        "video/x-raw, width=(int){}, height=(int){}, format=(string)BGRx ! ".format(display_width, display_height) +
        "videoconvert ! video/x-raw, format=(string)BGR ! appsink drop=true max-buffers=1 sync=false"
    )


def open_camera(camera_index=0, use_csi=False, sensor_id=0, width=640, height=480, framerate=30):
    if use_csi:
        pipeline = gstreamer_csi_pipeline(
            sensor_id=sensor_id,
            display_width=width,
            display_height=height,
            framerate=framerate,
        )
        capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not capture.isOpened():
            raise RuntimeError("Could not open CSI camera. Check nvargus-daemon and OpenCV GStreamer support.")
        return capture

    try:
        capture = cv2.VideoCapture(camera_index, cv2.CAP_V4L2)
    except Exception:
        capture = cv2.VideoCapture(camera_index)
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if not capture.isOpened():
        raise RuntimeError("Could not open USB camera index {}.".format(camera_index))
    return capture


def append_jsonl(path, event):
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def save_image(path, image):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    if not cv2.imwrite(path, image):
        raise IOError("Could not save image: {}".format(path))


class InspectionOutbox:
    """Durably queue inspection events on the Nano and retry delivery in background."""

    def __init__(self, db_path, server_url="", server_token="", interval=2.0, timeout=3.0):
        self.db_path = db_path
        self.server_url = (server_url or "").rstrip("/")
        self.server_token = server_token or ""
        self.interval = float(interval)
        self.timeout = float(timeout)
        self._stop = threading.Event()
        self._thread = None
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with sqlite3.connect(self.db_path, timeout=10.0) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS inspection_outbox (
                    event_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    sent_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                )"""
            )

    def start(self):
        if not self.server_url or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._send_loop, name="inspection-uploader")
        self._thread.daemon = True
        self._thread.start()

    def enqueue(self, event):
        event_id = event.get("event_id") or uuid.uuid4().hex
        event["event_id"] = event_id
        payload = json.dumps(event, ensure_ascii=False)
        with sqlite3.connect(self.db_path, timeout=10.0) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO inspection_outbox (event_id, created_at, payload_json) VALUES (?, ?, ?)",
                (event_id, time.time(), payload),
            )
        return event_id

    def pending_count(self):
        with sqlite3.connect(self.db_path, timeout=10.0) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM inspection_outbox WHERE sent_at IS NULL"
            ).fetchone()
        return int(row[0])

    def _pending_batch(self, limit=5):
        with sqlite3.connect(self.db_path, timeout=10.0) as connection:
            rows = connection.execute(
                """SELECT event_id, payload_json FROM inspection_outbox
                   WHERE sent_at IS NULL AND next_attempt_at <= ?
                   ORDER BY created_at LIMIT ?""",
                (time.time(), int(limit)),
            ).fetchall()
        return rows

    def _set_sent(self, event_id):
        with sqlite3.connect(self.db_path, timeout=10.0) as connection:
            connection.execute(
                "UPDATE inspection_outbox SET sent_at = ?, attempts = attempts + 1, last_error = NULL WHERE event_id = ?",
                (time.time(), event_id),
            )

    def _set_failed(self, event_id, error):
        with sqlite3.connect(self.db_path, timeout=10.0) as connection:
            row = connection.execute(
                "SELECT attempts FROM inspection_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            attempts = int(row[0]) + 1 if row else 1
            backoff = min(300.0, float(2 ** min(attempts, 8)))
            connection.execute(
                """UPDATE inspection_outbox SET attempts = ?, next_attempt_at = ?, last_error = ?
                   WHERE event_id = ?""",
                (attempts, time.time() + backoff, str(error)[:500], event_id),
            )
            return attempts

    def _send_one(self, event_id, payload_json):
        url = self.server_url + "/api/inspection_event"
        request = urllib.request.Request(
            url,
            data=payload_json.encode("utf-8"),
            headers={"Content-Type": "application/json", "Idempotency-Key": event_id},
        )
        if self.server_token:
            request.add_header("Authorization", "Bearer " + self.server_token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = response.read().decode("utf-8")
                if response.getcode() < 200 or response.getcode() >= 300:
                    raise IOError("Server returned HTTP {}".format(response.getcode()))
            acknowledgement = json.loads(response_data)
            if not acknowledgement.get("accepted"):
                raise IOError("Server did not acknowledge the event")
            self._set_sent(event_id)
            print("[sync] Server stored event {} (database event {}).".format(
                event_id, acknowledgement.get("database_event_id", "already received")
            ))
        except Exception as exc:
            attempts = self._set_failed(event_id, exc)
            if attempts in (1, 5, 10):
                print("[sync] Server unavailable; {} event(s) remain queued: {}".format(
                    self.pending_count(), exc
                ))

    def _send_loop(self):
        while not self._stop.is_set():
            try:
                for event_id, payload_json in self._pending_batch():
                    if self._stop.is_set():
                        break
                    self._send_one(event_id, payload_json)
            except Exception as exc:
                print("[sync] Outbox error: {}".format(exc))
            self._stop.wait(self.interval)

    def close(self, wait_seconds=4.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(float(wait_seconds))
        if self.server_url:
            for event_id, payload_json in self._pending_batch(limit=5):
                self._send_one(event_id, payload_json)
