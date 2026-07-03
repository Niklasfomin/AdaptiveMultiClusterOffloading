__author__ = "Johannes Köster"
__copyright__ = "Copyright 2025, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

from dataclasses import dataclass

import snakemake_interface_common.plugin_registry.plugin


@dataclass
class SchedulerSettingsBase(
    snakemake_interface_common.plugin_registry.plugin.SettingsBase
):
    """Base class for scheduler settings.

    Scheduler pluginscan define a subclass of this class,
    named 'SchedulerSettings'.
    """

    pass
