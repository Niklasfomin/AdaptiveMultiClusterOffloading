from setuptools import setup, find_packages

setup(
    name="snakemake-executor-plugin-offloader",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "kubernetes",
        "python-zunclient",
        "python-cinderclient",
        "setuptools",
        "snakemake",
        "snakemake-interface-common",
        "snakemake-interface-executor-plugins",
        "snakemake-executor-plugin-kubernetes",
        "snakemake-executor-plugin-openstack",
    ],
)
