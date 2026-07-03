def at_least_snakemake_version(version: str) -> bool:
    """Check if the current Snakemake version is at least the specified version.

    There is no need to check for a maximum version as Snakemake itself will ensure
    that only compatible interfaces can be installed along with it.
    """
    from snakemake import __version__ as snakemake_version
    from packaging.version import parse

    snakemake_version = parse(snakemake_version)
    return snakemake_version >= parse(version)
