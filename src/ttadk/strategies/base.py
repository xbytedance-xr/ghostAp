import abc
import logging
import os
from typing import Optional

from ..models import TTADKModel

logger = logging.getLogger(__name__)


def _env_truthy(name: str) -> bool:
    v = os.getenv(name)
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "0", "false", "no", "off")


def _in_ci_environment() -> bool:
    # Common CI markers. Keep this list conservative: false positives are worse
    # than missing a niche CI.
    for k in (
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "BUILDKITE",
        "CIRCLECI",
        "TRAVIS",
        "TF_BUILD",
        "TEAMCITY_VERSION",
        "JENKINS_URL",
        "BUILD_NUMBER",
    ):
        if _env_truthy(k):
            return True
    return False


class ModelFetchStrategy(abc.ABC):
    """模型获取策略基类"""

    @abc.abstractmethod
    def fetch(self, tool_name: str, cwd: Optional[str] = None) -> list[TTADKModel]:
        """获取指定工具的模型列表"""
        pass

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """策略名称"""
        pass
