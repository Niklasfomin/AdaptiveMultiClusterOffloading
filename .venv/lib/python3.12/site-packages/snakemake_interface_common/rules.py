from abc import ABC, abstractmethod


class RuleInterface(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def lineno(self) -> int: ...

    @property
    @abstractmethod
    def snakefile(self) -> str: ...
