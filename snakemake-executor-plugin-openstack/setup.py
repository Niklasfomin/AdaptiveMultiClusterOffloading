from setuptools import setup, find_packages

setup(
    name='snakemake-executor-plugin-openstack',
    version='1.0.0',
    packages=find_packages(),
    install_requires=[
        'python-zunclient',
        'setuptools',
        'snakemake',
        'snakemake-interface-common',
        'snakemake-interface-executor-plugins',
    ],
)