"""
策略包：包含所有与获取和验证模型相关的策略实现。
按照策略模式拆分以避免单一文件职责过重。
"""

from .base import ModelFetchStrategy
from .interactive import InteractiveStrategy
from .local_config import LocalConfigModelsStrategy, TTADKLocalConfigError
from .official_cli import (
    OfficialCLIModelsStrategy,
    TTADKModelsListStrategy,
    TTADKOfficialCLIError,
)
from .probe import ProbeStrategy, TTADKProbeError
from .project_meta import ProjectMetaModelsStrategy, TTADKProjectMetaError

__all__ = [
    "ModelFetchStrategy",
    "ProbeStrategy",
    "TTADKProbeError",
    "InteractiveStrategy",
    "TTADKModelsListStrategy",
    "OfficialCLIModelsStrategy",
    "TTADKOfficialCLIError",
    "ProjectMetaModelsStrategy",
    "TTADKProjectMetaError",
    "LocalConfigModelsStrategy",
    "TTADKLocalConfigError",
]
