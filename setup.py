import re
from pathlib import Path

from setuptools import find_packages, setup

VERSION = re.search(r'__version__ = "([^"]+)"', Path("streamdl/__init__.py").read_text(encoding="utf-8")).group(1)

setup(
    name="streamdl",
    version=VERSION,
    description="HLS / DASH 串流下載器（直播、回溯、CENC 解密、網站適配器）",
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    packages=find_packages(include=["streamdl", "streamdl.*"]),
    python_requires=">=3.11",
    install_requires=[
        "pydantic>=2.0",
        "httpx>=0.28",
        "rich>=13",
        "pycryptodome>=3.19",
        "beautifulsoup4>=4.12",
    ],
    extras_require={
        # ZAN-LIVE、SINGULAR LIVE、瀏覽器監控、--fetcher browser 需要
        "browser": ["undetected-chromedriver>=3.5"],
        "dev": ["pytest>=8", "pyinstaller>=6"],
    },
    entry_points={"console_scripts": ["streamdl=streamdl.cli:main"]},
)
