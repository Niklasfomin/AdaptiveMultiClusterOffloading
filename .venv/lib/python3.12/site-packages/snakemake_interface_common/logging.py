__author__ = "Johannes Köster"
__copyright__ = "Copyright 2023, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging


def get_logger() -> "logging.Logger":
    """Retrieve the logger singleton from snakemake."""
    from snakemake.logging import logger  # type: ignore

    return logger
