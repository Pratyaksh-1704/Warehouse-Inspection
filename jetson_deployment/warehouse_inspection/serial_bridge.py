"""Forward the inspection node's gated Twist commands to an ESP32 over USB serial."""

import math
import time

import rclpy
import serial
from geometry_msgs.msg import Twist
from rclpy.node import Node
from std_msgs.msg import String


class Esp32SerialBridge(Node):
    """Send versioned, newline-delimited velocity frames to the rover controller."""

    def __init__(self):
        super().__init__("esp32_serial_bridge")
        self.declare_parameter("command_topic", "/cmd_vel_inspected")
        self.declare_parameter("status_topic", "/rover/esp32_link_status")
        self.declare_parameter("serial_port", "/dev/ttyUSB0")
        self.declare_parameter("baud_rate", 115200)
        self.declare_parameter("publish_rate_hz", 20.0)
        self.declare_parameter("command_timeout_sec", 0.25)
        self.declare_parameter("reconnect_period_sec", 2.0)

        self.command_topic = str(self.get_parameter("command_topic").value)
        self.serial_port = str(self.get_parameter("serial_port").value)
        self.baud_rate = int(self.get_parameter("baud_rate").value)
        self.command_timeout_sec = float(
            self.get_parameter("command_timeout_sec").value
        )
        self.reconnect_period_sec = float(
            self.get_parameter("reconnect_period_sec").value
        )
        publish_rate_hz = float(self.get_parameter("publish_rate_hz").value)
        if (publish_rate_hz <= 0.0 or self.command_timeout_sec <= 0.0
                or self.reconnect_period_sec <= 0.0):
            raise ValueError("Serial bridge rates and timeouts must be greater than zero")

        self._status_pub = self.create_publisher(
            String, str(self.get_parameter("status_topic").value), 10
        )
        self.create_subscription(Twist, self.command_topic, self._command_callback, 10)
        self._last_command = None
        self._last_command_time = 0.0
        self._serial = None
        self._connected_at = 0.0
        self._next_connect_attempt = 0.0
        self._sequence = 0
        self.create_timer(1.0 / publish_rate_hz, self._send_command)
        self.get_logger().info(
            "ESP32 serial bridge: topic=%s port=%s baud=%d" %
            (self.command_topic, self.serial_port, self.baud_rate)
        )

    def _publish_status(self, text):
        message = String()
        message.data = text
        self._status_pub.publish(message)

    def _command_callback(self, message):
        vx = float(message.linear.x)
        wz = float(message.angular.z)
        if not math.isfinite(vx) or not math.isfinite(wz):
            self.get_logger().error("Discarding non-finite velocity command")
            self._last_command = None
            self._last_command_time = 0.0
            return
        self._last_command = (vx, wz)
        self._last_command_time = time.monotonic()

    def _connect(self, now):
        if now < self._next_connect_attempt:
            return False
        self._next_connect_attempt = now + self.reconnect_period_sec
        try:
            self._serial = serial.Serial(
                port=self.serial_port,
                baudrate=self.baud_rate,
                timeout=0.0,
                write_timeout=0.05,
            )
            self._connected_at = now
            self.get_logger().info("Connected to ESP32 on %s" % self.serial_port)
            self._publish_status("SERIAL_CONNECTED")
            return True
        except (serial.SerialException, OSError, ValueError) as exc:
            self._serial = None
            self.get_logger().warning(
                "ESP32 serial port unavailable (%s): %s" % (self.serial_port, exc)
            )
            self._publish_status("SERIAL_DISCONNECTED")
            return False

    def _send_command(self):
        now = time.monotonic()
        if self._serial is None and not self._connect(now):
            return

        command = (0.0, 0.0)
        if (self._last_command is not None
                and self._last_command_time >= self._connected_at
                and now - self._last_command_time <= self.command_timeout_sec):
            command = self._last_command

        linear_mm_s = int(round(command[0] * 1000.0))
        angular_mrad_s = int(round(command[1] * 1000.0))
        frame = "CMD1,%d,%d,%d\n" % (
            self._sequence, linear_mm_s, angular_mrad_s
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        try:
            self._serial.write(frame.encode("ascii"))
        except (serial.SerialException, OSError, ValueError) as exc:
            self.get_logger().error("ESP32 serial write failed; reconnecting: %s" % exc)
            self._close_serial()
            self._next_connect_attempt = now + self.reconnect_period_sec
            self._publish_status("SERIAL_DISCONNECTED")

    def _close_serial(self):
        if self._serial is None:
            return
        try:
            self._serial.close()
        except (serial.SerialException, OSError):
            pass
        self._serial = None

    def stop_and_close(self):
        if self._serial is not None:
            try:
                self._serial.write(
                    ("CMD1,%d,0,0\n" % self._sequence).encode("ascii")
                )
                self._serial.flush()
            except (serial.SerialException, OSError, ValueError):
                pass
        self._close_serial()


def main(args=None):
    rclpy.init(args=args)
    node = Esp32SerialBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_and_close()
        node.destroy_node()
        rclpy.shutdown()
