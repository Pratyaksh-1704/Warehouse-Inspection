#!/usr/bin/env python3
"""Single-camera Jetson Nano inspection with local storage and server relay."""

import argparse
import os
import time

import cv2

from jetson_runtime import (
    DamageDetector,
    InspectionOutbox,
    append_jsonl,
    decode_barcodes,
    draw_barcode_overlay,
    draw_damage_overlay,
    open_camera,
    save_image,
)


def inspect(frame, detector, engine):
    barcodes = decode_barcodes(frame, engine)
    detections = detector.predict(frame)
    is_damaged = any(item["is_defect"] for item in detections)
    if is_damaged:
        status = "DAMAGED_FLAGGED"
    elif detections:
        status = "VERIFIED_OK"
    else:
        status = "NO_DAMAGE_DETECTED"
    damage = {
        "timestamp": time.time(),
        "is_damaged": is_damaged,
        "status": status,
        "detections_count": len(detections),
        "detections": detections,
    }
    annotated = draw_damage_overlay(frame, detections)
    annotated = draw_barcode_overlay(annotated, barcodes)
    return barcodes, damage, annotated


def make_report(barcode, damage, mission_id, device_id, source):
    data = barcode["data"] if barcode else {}
    detections = damage["detections"]
    defects = [item for item in detections if item["is_defect"]]
    candidates = defects if defects else detections
    top = max(candidates, key=lambda item: item["confidence"]) if candidates else None
    if top:
        damage_label = top["label"]
        damage_confidence = top["confidence"]
        severity = top["severity"]
        action_required = top["action"]
    elif damage["status"] == "VERIFIED_OK":
        damage_label = "undamaged_product"
        damage_confidence = 0.0
        severity = "LOW"
        action_required = "ACCEPTABLE"
    else:
        damage_label = "no_detection"
        damage_confidence = 0.0
        severity = "NONE"
        action_required = "NONE"

    return {
        "device_id": device_id,
        "mission_id": mission_id,
        "timestamp": damage["timestamp"],
        "item": data.get("item", "Unidentified product"),
        "product_type": data.get("product_type", "Unknown"),
        "cost": data.get("cost", 0.0),
        "shelf_id": data.get("shelf_id", "SHELF-UNKNOWN"),
        "sku": data.get("sku"),
        "batch_id": data.get("batch_id"),
        "x": data.get("x", 0.0),
        "y": data.get("y", 0.0),
        "barcode_type": barcode["barcode_type"] if barcode else "UNKNOWN",
        "raw_barcode": barcode["raw_payload"] if barcode else "",
        "is_damaged": damage["is_damaged"],
        "damage_label": damage_label,
        "damage_confidence": damage_confidence,
        "severity": severity,
        "action_required": action_required,
        "detection_details": {
            "source": source,
            "device_id": device_id,
            "damage_status": damage["status"],
            "detection_count": damage["detections_count"],
            "detections": detections,
            "barcode_detected": barcode is not None,
        },
    }


def queue_report(outbox, log_path, report):
    event_id = outbox.enqueue(report)
    append_jsonl(log_path, report)
    print("[local] Stored event {} | {} | {}".format(
        event_id, report["item"], "DAMAGED" if report["is_damaged"] else "OK"
    ))


def main():
    deployment_dir = os.path.dirname(os.path.abspath(__file__))
    default_model = os.path.join(deployment_dir, "best.onnx")
    default_outbox = os.path.join(deployment_dir, "nano_inspections.sqlite3")
    parser = argparse.ArgumentParser(description="Jetson Nano barcode and damage inspector with store-and-forward")
    parser.add_argument("--model", default=default_model, help="Fixed-shape YOLO ONNX model")
    parser.add_argument("--image", help="Inspect one image instead of opening a camera")
    parser.add_argument("--csi", action="store_true", help="Use the Jetson CSI camera")
    parser.add_argument("--sensor-id", type=int, default=0)
    parser.add_argument("--camera", type=int, default=0, help="USB camera index")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--input-size", type=int, default=416)
    parser.add_argument("--class-names", help="Comma-separated names for a non-default model")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--backend", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--engine", choices=("auto", "zxing", "pyzbar", "opencv"), default="auto")
    parser.add_argument("--mission-id", default="MISSION_01")
    parser.add_argument("--device-id", default="jetson-nano-01")
    parser.add_argument("--server-url", default=os.environ.get("WAREHOUSE_SERVER_URL", ""),
                        help="Laptop server base URL, e.g. http://192.168.1.20:8080")
    parser.add_argument("--server-token", default=os.environ.get("WAREHOUSE_SERVER_TOKEN", ""),
                        help="Optional bearer token matching the server configuration")
    parser.add_argument("--outbox", default=default_outbox, help="Local SQLite durable queue")
    parser.add_argument("--log", default="inspection_events.jsonl", help="Local JSONL audit log")
    parser.add_argument("--cooldown", type=float, default=30.0,
                        help="Seconds before recording the same barcode/status again")
    parser.add_argument("--headless", action="store_true", help="Run without a display window")
    args = parser.parse_args()

    names = [name.strip() for name in args.class_names.split(",")] if args.class_names else None
    try:
        detector = DamageDetector(
            args.model,
            image_size=args.input_size,
            confidence=args.conf,
            nms_threshold=args.iou,
            backend=args.backend,
            class_names=names,
        )
    except Exception as exc:
        parser.error("Could not initialize damage model: {}".format(exc))

    outbox = InspectionOutbox(args.outbox, args.server_url, args.server_token)
    outbox.start()
    if args.server_url:
        print("[sync] Events will be sent to {} when reachable.".format(args.server_url))
    else:
        print("[sync] No server URL configured; events will remain in the local SQLite outbox.")

    try:
        if args.image:
            frame = cv2.imread(args.image)
            if frame is None:
                parser.error("Could not read image: {}".format(args.image))
            barcodes, damage, annotated = inspect(frame, detector, args.engine)
            if barcodes:
                for barcode in barcodes:
                    report = make_report(barcode, damage, args.mission_id, args.device_id, args.image)
                    queue_report(outbox, args.log, report)
            elif damage["detections"]:
                report = make_report(None, damage, args.mission_id, args.device_id, args.image)
                queue_report(outbox, args.log, report)
            else:
                print("[inspection] No barcode or damage detection; no report was stored.")
            output = os.path.splitext(args.image)[0] + "_inspection.jpg"
            save_image(output, annotated)
            print("[inspection] Annotated image saved to {}".format(output))
            return

        try:
            capture = open_camera(args.camera, args.csi, args.sensor_id, args.width, args.height, args.fps)
        except RuntimeError as exc:
            parser.error(str(exc))

        previous_time = time.time()
        last_scan_time = {}
        last_scan_status = {}
        last_unlabeled_time = 0.0
        last_unlabeled_status = None
        fps = 0.0
        print("[inspection] Running. Press q to quit; press s to save a snapshot.")
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    print("[inspection] Camera frame read failed; stopping.")
                    break
                now = time.time()
                instant_fps = 1.0 / max(now - previous_time, 0.00001)
                fps = instant_fps if fps == 0.0 else (0.85 * fps + 0.15 * instant_fps)
                previous_time = now

                barcodes, damage, annotated = inspect(frame, detector, args.engine)
                if barcodes:
                    for barcode in barcodes:
                        raw = barcode["raw_payload"]
                        status = damage["is_damaged"]
                        recently_seen = now - last_scan_time.get(raw, 0.0) < args.cooldown
                        status_changed = raw in last_scan_status and last_scan_status[raw] != status
                        if raw not in last_scan_status or status_changed or not recently_seen:
                            report = make_report(barcode, damage, args.mission_id, args.device_id, "camera")
                            queue_report(outbox, args.log, report)
                            last_scan_status[raw] = status
                            last_scan_time[raw] = now
                        else:
                            last_scan_time[raw] = now
                elif damage["detections"]:
                    signature = (damage["status"], tuple(item["label"] for item in damage["detections"]))
                    if signature != last_unlabeled_status or now - last_unlabeled_time >= args.cooldown:
                        report = make_report(None, damage, args.mission_id, args.device_id, "camera")
                        queue_report(outbox, args.log, report)
                        last_unlabeled_status = signature
                        last_unlabeled_time = now

                if not args.headless:
                    cv2.imshow("Jetson Nano Integrated Inspector", annotated)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        break
                    if key == ord("s"):
                        path = "inspection_snapshot_{}.jpg".format(int(now))
                        save_image(path, annotated)
                        print("[inspection] Snapshot saved: {}".format(path))
        except KeyboardInterrupt:
            pass
        finally:
            capture.release()
            if not args.headless:
                cv2.destroyAllWindows()
            print("[inspection] Camera closed.")
    except KeyboardInterrupt:
        pass
    finally:
        outbox.close()
        print("[local] {} event(s) still pending upload.".format(outbox.pending_count()))


if __name__ == "__main__":
    main()
