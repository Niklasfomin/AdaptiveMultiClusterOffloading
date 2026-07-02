from setuptools import setup, find_packages

setup(
    name="snakemake-executor-plugin-kubernetes",
    description="A snakemake executor plugin for Kubernetes, adapted for offloading.",
    version="1.0.0",
    url="https://github.com/snakemake/snakemake-executor-plugin-kubernetes",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "snakemake-interface-common>=1.17.3",
        "snakemake-interface-executor-plugins>=9.0.0,<10.0.0",
        "kubernetes>=27.2.0,<31"
    ],
    include_package_data=True,
    zip_safe=False,
)
