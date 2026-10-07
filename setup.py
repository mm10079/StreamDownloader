from setuptools import setup, find_packages

setup(
    name="StreamDownloader",
    version="0.1.0",
    packages=find_packages(include=["src", "src.*"]),
    python_requires=">=3.11",
    install_requires=[
        "pydantic>=2.0",
        "httpx>=0.28",
        "rich>=13",
        "pycryptodome>=3.19",
        "undetected-chromedriver>=3.5",
        "beautifulsoup4>=4.12",
    ],
    entry_points={"console_scripts": ["streamdl=src.cli:main"]},
)
