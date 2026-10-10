"""Launch QR inspection and the gated Twist-to-ESP32 USB serial bridge."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    image_topic = LaunchConfiguration("image_topic")
    nav_cmd_topic = LaunchConfiguration("nav_cmd_topic")
    motor_cmd_topic = LaunchConfiguration("motor_cmd_topic")
    camera_startup_timeout_sec = LaunchConfiguration("camera_startup_timeout_sec")
    camera_timeout_sec = LaunchConfiguration("camera_timeout_sec")
    serial_port = LaunchConfiguration("serial_port")
    baud_rate = LaunchConfiguration("baud_rate")

    return LaunchDescription([
        DeclareLaunchArgument("image_topic", default_value="/camera/image_raw"),
        DeclareLaunchArgument("nav_cmd_topic", default_value="/cmd_vel_nav"),
        DeclareLaunchArgument("motor_cmd_topic", default_value="/cmd_vel_inspected"),
        DeclareLaunchArgument("camera_startup_timeout_sec", default_value="15.0"),
        DeclareLaunchArgument("camera_timeout_sec", default_value="1.0"),
        DeclareLaunchArgument("serial_port", default_value="/dev/ttyUSB0"),
        DeclareLaunchArgument("baud_rate", default_value="115200"),
        Node(
            package="warehouse_inspection",
            executable="qr_inspector",
            name="qr_stop_and_inspect",
            output="screen",
            parameters=[{
                "image_topic": image_topic,
                "nav_cmd_topic": nav_cmd_topic,
                "motor_cmd_topic": motor_cmd_topic,
                "camera_startup_timeout_sec": camera_startup_timeout_sec,
                "camera_timeout_sec": camera_timeout_sec,
            }],
        ),
        Node(
            package="warehouse_inspection",
            executable="esp32_serial_bridge",
            name="esp32_serial_bridge",
            output="screen",
            parameters=[{
                "command_topic": motor_cmd_topic,
                "serial_port": serial_port,
                "baud_rate": baud_rate,
            }],
        ),
    ])
