from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="sou",
    version="0.1",
    packages=find_packages(),
    include_package_data=True,
    package_data={"sou": ["*.tcss"]},
    install_requires=requirements,
    entry_points={
        'console_scripts': [
            'sou = sou.tui:main',
        ],
    },
)
