from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CocoModel:
    name: str
    description: str = ""
    is_default: bool = False


@dataclass
class ModelListResult:
    models: list[CocoModel] = field(default_factory=list)
    cached: bool = False
    error: Optional[str] = None
