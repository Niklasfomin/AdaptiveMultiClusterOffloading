__author__ = "Johannes Köster"
__copyright__ = "Copyright 2022, Johannes Köster, Vanessa Sochat"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

from abc import ABC, abstractmethod
import re
import types
import pkgutil
import importlib
from typing import Dict, List, Mapping, TYPE_CHECKING, Type, TypeVar, Generic

from snakemake_interface_common.exceptions import InvalidPluginException
from snakemake_interface_common.plugin_registry.plugin import PluginBase
from snakemake_interface_common.plugin_registry.attribute_types import AttributeType

if TYPE_CHECKING:
    from argparse import ArgumentParser

TPlugin = TypeVar("TPlugin", bound=PluginBase, covariant=True)


class PluginRegistryBase(ABC, Generic[TPlugin]):
    """This class is a singleton that holds all registered executor plugins."""

    _instance = None
    plugins: Dict[str, TPlugin]

    def __new__(
        cls: Type["PluginRegistryBase[TPlugin]"],
    ) -> "PluginRegistryBase[TPlugin]":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "plugins"):
            # init has been called before
            return
        self.collect_plugins()

    def get_registered_plugins(self) -> List[str]:
        """Return a list of registered plugin names."""
        return [name for name in self.plugins.keys()]

    def is_installed(self, plugin_name: str) -> bool:
        """Return True if the plugin is registered."""
        return plugin_name in self.plugins

    def get_plugin(self, plugin_name: str) -> PluginBase:
        """Get a plugin by name."""
        try:
            return self.plugins[plugin_name]
        except KeyError:
            raise InvalidPluginException(
                plugin_name,
                f"The package {self.module_prefix.replace('_', '-')}{plugin_name} is "
                "not installed.",
            )

    def get_plugin_package_name(self, plugin_name: str) -> str:
        """Get the package name of a plugin by name."""
        return f"{self.module_prefix.replace('_', '-')}{plugin_name}"

    def register_cli_args(self, argparser: "ArgumentParser") -> None:
        """Add arguments derived from self.executor_settings to given
        argparser."""
        plugin_type = self.get_plugin_type()
        for _, plugin in self.plugins.items():
            plugin.register_cli_args(argparser, plugin_type)

    def get_plugin_type(self) -> str:
        m = re.match(r"(?P<type>.+)PluginRegistry", self.__class__.__name__)
        if m is not None:
            return m.group("type").lower()
        raise ValueError(
            "Unable to infer plugin type name from registry class "
            f"name: {self.__class__.__name__}. The name is expected to follow the "
            "pattern <type>PluginRegistry, e.g. ExecutorPluginRegistry."
        )

    def collect_plugins(self) -> None:
        """Collect plugins and call register_plugin for each."""
        self.plugins = dict()

        # Executor plugins are externally installed plugins named
        # "snakemake_executor_<name>".
        # They should follow the same convention if on pip,
        # snakemake-executor-<name>.
        # Note that these will not be detected installed in editable
        # mode (pip install -e .).
        for moduleinfo in pkgutil.iter_modules():
            if not moduleinfo.ispkg or not moduleinfo.name.startswith(
                self.module_prefix
            ):
                continue
            module = importlib.import_module(moduleinfo.name)
            self.register_plugin(moduleinfo.name, module)

    def register_plugin(self, name: str, plugin: types.ModuleType) -> None:
        """Validate and register a plugin.

        Does nothing if the plugin is already registered.
        """
        if name in self.plugins:
            return

        self.validate_plugin(name, plugin)

        # Derive the shortened name for future access
        plugin_name = name.removeprefix(self.module_prefix).replace("_", "-")

        self.plugins[plugin_name] = self.load_plugin(plugin_name, plugin)

    def is_valid_plugin_package_name(self, name: str) -> bool:
        return True

    def validate_plugin(self, name: str, module: types.ModuleType) -> None:
        """Validate a plugin for attributes and naming"""
        expected_attributes = self.expected_attributes()
        for attr, attr_type in expected_attributes.items():
            # check if attr is missing and fail if it is not optional
            if not hasattr(module, attr):
                if attr_type.is_optional:
                    continue
                raise InvalidPluginException(name, f"plugin does not define {attr}.")

            attr_value = getattr(module, attr)
            if attr_type.is_class:
                # check for class type
                if not issubclass(attr_value, attr_type.cls):
                    raise InvalidPluginException(
                        name,
                        f"{attr} must be a subclass of "
                        f"{attr_type.cls.__module__}.{attr_type.cls.__name__}.",
                    )
            else:
                # check for instance type
                if not isinstance(attr_value, attr_type.cls):
                    raise InvalidPluginException(
                        name, f"{attr} must be of type {attr_type.cls.__name__}."
                    )

    @property
    @abstractmethod
    def module_prefix(self) -> str: ...

    @abstractmethod
    def load_plugin(self, name: str, module: types.ModuleType) -> TPlugin:
        """Load a plugin by name."""
        ...

    @abstractmethod
    def expected_attributes(self) -> Mapping[str, AttributeType]: ...
