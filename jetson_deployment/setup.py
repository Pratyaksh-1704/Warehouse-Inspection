from glob import glob
import os
from setuptools import setup

package_name = "warehouse_inspection"

setup(
    name=package_name,
    version="0.2.0",
    packages=[package_name],
    py_modules=["jetson_runtime"],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/warehouse_inspection"]),
        (os.path.join("share", package_name), ["package.xml"]),
        (os.path.join("share", package_name, "models"), [
            "best.onnx",
            "product_types.onnx",
            "product_classes.json",
        ]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Pratyaksh",
    maintainer_email="prat170404@gmail.com",
    description="QR-triggered product identity and condition inspection for a ROS 2 Jetson rover.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "qr_inspector = warehouse_inspection.ros_node:main",
            "esp32_serial_bridge = warehouse_inspection.serial_bridge:main",
        ],
    },
)
