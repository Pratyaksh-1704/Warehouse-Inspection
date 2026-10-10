#!/usr/bin/env python3
"""YOLOv8 ONNX product damage detector for Jetson Nano (OpenCV DNN)."""

import argparse
import os
import time

import cv2

from jetson_runtime import (
    DamageDetector,
    append_jsonl,
    damage_event,
    draw_damage_overlay,
    open_camera,
    save_image,
)


def init_gpio(board_pin):
    if board_pin is None:
        return None
    try:
        import Jetson.GPIO as gpio
    except Exception as exc:
        raise RuntimeError("Jetson.GPIO is required when --gpio-pin is set: {}".format(exc))
    gpio.setmode(gpio.BOARD)
    gpio.setup(board_pin, gpio.OUT, initial=gpio.LOW)
    print("[damage] GPIO BOARD pin {} enabled.".format(board_pin))
    return gpio


def main():
    default_model = os.path.join(os.path.dirname(os.path.abspath(__file__)), "best.onnx")
    parser = argparse.ArgumentParser(description="Jetson Nano product damage detection")
    parser.add_argument("--model", default=default_model, help="Fixed-shape YOLO ONNX model")
    parser.add_argument("--image", help="Inspect a single image instead of opening a camera")
    parser.add_argument("--csi", action="store_true", help="Use the Jetson CSI camera")
    parser.add_argument("--sensor-id", type=int, default=0)
    parser.add_argument("--camera", type=int, default=0, help="USB camera index")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--input-size", type=int, default=416, help="ONNX input side; current best.onnx uses 416")
    parser.add_argument("--class-names", help="Comma-separated class names for a non-default model")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--backend", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--gpio-pin", type=int, help="Optional physical BOARD pin; HIGH while damage is detected")
    parser.add_argument("--log", default="damage_inspections.jsonl", help="JSONL output; empty string disables logging")
    parser.add_argument("--output", help="Annotated image output path for --image")
    parser.add_argument("--headless", action="store_true", help="Run without an OpenCV display window")
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

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            parser.error("Could not read image: {}".format(args.image))
        detections = detector.predict(frame)
        event = damage_event(detections, args.image)
        append_jsonl(args.log, event)
        output = args.output or os.path.splitext(args.image)[0] + "_damage.jpg"
        save_image(output, draw_damage_overlay(frame, detections))
        print("[damage] {} ({} detection(s)); annotated image: {}".format(
            event["status"], event["detections_count"], output
        ))
        return

    gpio = None
    capture = None
    try:
        gpio = init_gpio(args.gpio_pin)
        try:
            capture = open_camera(args.camera, args.csi, args.sensor_id, args.width, args.height, args.fps)
        except RuntimeError as exc:
            parser.error(str(exc))

        print("[damage] Running. Press q to quit; press s to save a snapshot.")
        previous_time = time.time()
        last_record_time = 0.0
        last_status = None
        fps = 0.0
        while True:
            ok, frame = capture.read()
            if not ok:
                print("[damage] Camera frame read failed; stopping.")
                break
            now = time.time()
            instant_fps = 1.0 / max(now - previous_time, 0.00001)
            fps = instant_fps if fps == 0.0 else (0.85 * fps + 0.15 * instant_fps)
            previous_time = now

            detections = detector.predict(frame)
            event = damage_event(detections)
            if gpio is not None:
                gpio.output(args.gpio_pin, gpio.HIGH if event["is_damaged"] else gpio.LOW)
            if event["status"] != last_status or now - last_record_time >= 2.5:
                append_jsonl(args.log, event)
                print("[damage] {} | {} detection(s)".format(event["status"], event["detections_count"]))
                last_status = event["status"]
                last_record_time = now

            annotated = draw_damage_overlay(frame, detections, fps)
            if not args.headless:
                cv2.imshow("Jetson Nano Damage Detector", annotated)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("s"):
                    path = "damage_snapshot_{}.jpg".format(int(now))
                    save_image(path, annotated)
                    print("[damage] Snapshot saved: {}".format(path))
    except KeyboardInterrupt:
        pass
    finally:
        if capture is not None:
            capture.release()
        if not args.headless:
            cv2.destroyAllWindows()
        if gpio is not None:
            try:
                gpio.output(args.gpio_pin, gpio.LOW)
                gpio.cleanup()
            except Exception:
                pass
        print("[damage] Camera closed.")


if __name__ == "__main__":
    main()
