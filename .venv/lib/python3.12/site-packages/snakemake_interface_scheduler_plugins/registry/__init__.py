__author__ = "Johannes Köster"
__copyright__ = "Copyright 2025, Johannes Köster"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

import types
from typing import Mapping

from snakemake_interface_scheduler_plugins.base import SchedulerBase
from snakemake_interface_scheduler_plugins.settings import (
    SchedulerSettingsBase,
)
from snakemake_interface_common.plugin_registry.attribute_types import (
    AttributeKind,
    AttributeMode,
    AttributeType,
)
from snakemake_interface_scheduler_plugins.registry.plugin import Plugin
from snakemake_interface_common.plugin_registry import PluginRegistryBase
from snakemake_interface_scheduler_plugins import common


class SchedulerPluginRegistry(PluginRegistryBase):
    """This class is a singleton that holds all registered scheduler plugins."""

    @property
    def module_prefix(self) -> str:
        return common.scheduler_plugin_module_prefix

    def load_plugin(self, name: str, module: types.ModuleType) -> Plugin:
        """Load a plugin by name."""

        return Plugin(
            _name=name,
            scheduler=module.Scheduler,
            _scheduler_settings_cls=getattr(module, "SchedulerSettings", None),
        )

    def expected_attributes(self) -> Mapping[str, AttributeType]:
        return {
            "SchedulerSettings": AttributeType(
                cls=SchedulerSettingsBase,
                mode=AttributeMode.OPTIONAL,
                kind=AttributeKind.CLASS,
            ),
            "Scheduler": AttributeType(
                cls=SchedulerBase,
                mode=AttributeMode.REQUIRED,
                kind=AttributeKind.CLASS,
            ),
        }

    def collect_plugins(self):
        """Collect plugins and call register_plugin for each."""
        super().collect_plugins()

        try:
            from snakemake.scheduling import greedy
            from snakemake.scheduling import milp
        except ImportError:
            # snakemake not present, proceed without adding these plugins
            return

        self.register_plugin("greedy", greedy)
        self.register_plugin("ilp", milp)
