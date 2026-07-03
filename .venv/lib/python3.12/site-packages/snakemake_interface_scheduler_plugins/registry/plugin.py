__author__ = "Johannes Köster"
__copyright__ = "Copyright 2025, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

from dataclasses import dataclass
from typing import Optional, Type
from snakemake_interface_scheduler_plugins.base import SchedulerBase
from snakemake_interface_scheduler_plugins.settings import SchedulerSettingsBase
from snakemake_interface_scheduler_plugins import common

from snakemake_interface_common.plugin_registry.plugin import PluginBase


@dataclass
class Plugin(PluginBase):
    scheduler: SchedulerBase
    _scheduler_settings_cls: Optional[Type[SchedulerSettingsBase]]
    _name: str

    @property
    def name(self):
        return self._name

    @property
    def cli_prefix(self):
        return "scheduler-" + self.name.replace(
            common.scheduler_plugin_module_prefix, ""
        )

    @property
    def settings_cls(self):
        return self._scheduler_settings_cls
