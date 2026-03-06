from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TTADKTool:
    name: str
    description: str = ""
    is_default: bool = False


@dataclass
class TTADKModel:
    name: str
    description: str = ""
    is_default: bool = False


@dataclass
class ToolListResult:
    tools: list[TTADKTool] = field(default_factory=list)
    cached: bool = False
    error: Optional[str] = None


@dataclass
class ModelListResult:
    models: list[TTADKModel] = field(default_factory=list)
    cached: bool = False
    error: Optional[str] = None
