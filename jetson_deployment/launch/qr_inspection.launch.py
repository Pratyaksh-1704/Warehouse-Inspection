from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    image_topic = LaunchConfiguration("image_topic")
    nav_cmd_topic = LaunchConfiguration("nav_cmd_topic")
    motor_cmd_topic = LaunchConfiguration("motor_cmd_topic")

    return LaunchDescription([
        DeclareLaunchArgument("image_topic", default_value="/camera/image_raw"),
        DeclareLaunchArgument("nav_cmd_topic", default_value="/cmd_vel_nav"),
        DeclareLaunchArgument("motor_cmd_topic", default_value="/cmd_vel"),
        Node(
            package="warehouse_inspection",
            executable="qr_inspector",
            name="qr_stop_and_inspect",
            output="screen",
            parameters=[{
                "image_topic": image_topic,
                "nav_cmd_topic": nav_cmd_topic,
                "motor_cmd_topic": motor_cmd_topic,
            }],
        ),
    ])
