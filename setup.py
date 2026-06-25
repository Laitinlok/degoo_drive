from setuptools import setup, find_packages

setup(
    name="degoo-drive-gui",
    version="0.1.0",
    packages=find_packages(),
    install_requires=["PyQt6>=6.6.0"],
    entry_points={
        "console_scripts": [
            "degoo-drive-gui=degoo_gui.main:main",
        ],
    },
    python_requires=">=3.11",
)
