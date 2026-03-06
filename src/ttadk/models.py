from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TTADKTool:
    name: str
    description: str = ""
    is_default: bool = False


@dataclass
class TTADKModel:
    name: str  # 真实模型名称（如 gpt-5.2-codex-ttadk）
    description: str = ""  # 描述
    is_default: bool = False
    friendly_name: str = ""  # 友好显示名称（如 GPT 5.2 Codex (Recommended)）


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
