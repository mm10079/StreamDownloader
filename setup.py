from setuptools import setup, find_packages

setup(
    name="StreamDownloader",
    version="0.0.0",
    packages=find_packages(),
    install_requires=[
        "pydantic>=2.0.0",
    ],
)