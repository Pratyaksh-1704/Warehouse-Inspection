#!/usr/bin/env python3
"""Warehouse QR/barcode scanner for Jetson Nano cameras."""

import argparse
import os
import time

import cv2

from jetson_runtime import (
    append_jsonl,
    barcode_event,
    decode_barcodes,
    draw_barcode_overlay,
    open_camera,
    save_image,
)


def main():
    parser = argparse.ArgumentParser(description="Jetson Nano barcode and QR scanner")
    parser.add_argument("--image", help="Decode one image instead of opening a camera")
    parser.add_argument("--csi", action="store_true", help="Use the Jetson CSI camera")
    parser.add_argument("--sensor-id", type=int, default=0, help="CSI sensor index")
    parser.add_argument("--camera", type=int, default=0, help="USB camera index (usually 0)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--engine", choices=("auto", "zxing", "pyzbar", "opencv"), default="auto")
    parser.add_argument("--mission-id", default="MISSION_01")
    parser.add_argument("--log", default="scanned_inventory.jsonl", help="JSONL output; use an empty string to disable")
    parser.add_argument("--cooldown", type=float, default=3.0, help="Seconds between repeated records of the same barcode")
    parser.add_argument("--headless", action="store_true", help="Run without an OpenCV display window")
    args = parser.parse_args()

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            parser.error("Could not read image: {}".format(args.image))
        detections = decode_barcodes(frame, args.engine)
        for detection in detections:
            event = barcode_event(detection, args.mission_id)
            append_jsonl(args.log, event)
            print("[scan] {} | {} | Rs.{:.2f} | {}".format(
                detection["data"]["item"], detection["data"]["product_type"],
                detection["data"]["cost"], detection["data"]["shelf_id"]
            ))
        if not detections:
            print("[scan] No supported code found.")
        annotated = draw_barcode_overlay(frame, detections)
        output = os.path.splitext(args.image)[0] + "_annotated.jpg"
        save_image(output, annotated)
        print("[scan] Annotated image: {}".format(output))
        return

    try:
        capture = open_camera(args.camera, args.csi, args.sensor_id, args.width, args.height, args.fps)
    except RuntimeError as exc:
        parser.error(str(exc))

    last_scan = {}
    previous_time = time.time()
    fps = 0.0
    print("[scan] Running. Press q to quit; press s to save a snapshot.")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                print("[scan] Camera frame read failed; stopping.")
                break
            now = time.time()
            instant_fps = 1.0 / max(now - previous_time, 0.00001)
            fps = instant_fps if fps == 0.0 else (0.85 * fps + 0.15 * instant_fps)
            previous_time = now

            detections = decode_barcodes(frame, args.engine)
            for detection in detections:
                key = detection["raw_payload"]
                if now - last_scan.get(key, 0.0) >= args.cooldown:
                    last_scan[key] = now
                    event = barcode_event(detection, args.mission_id)
                    append_jsonl(args.log, event)
                    print("[scan] {} | {} | Rs.{:.2f} | {}".format(
                        detection["data"]["item"], detection["data"]["product_type"],
                        detection["data"]["cost"], detection["data"]["shelf_id"]
                    ))

            annotated = draw_barcode_overlay(frame, detections, fps)
            if not args.headless:
                cv2.imshow("Jetson Nano Barcode Scanner", annotated)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("s"):
                    path = "barcode_snapshot_{}.jpg".format(int(now))
                    save_image(path, annotated)
                    print("[scan] Snapshot saved: {}".format(path))
    except KeyboardInterrupt:
        pass
    finally:
        capture.release()
        if not args.headless:
            cv2.destroyAllWindows()
        print("[scan] Camera closed.")


if __name__ == "__main__":
    main()
