from typing import Optional
from dataclasses import dataclass


@dataclass
class SoftwareReport:
    name: str
    version: Optional[str] = None
    is_secondary: bool = False
