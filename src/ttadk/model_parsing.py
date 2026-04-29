"""TTADK 模型解析纯函数。

从 `manager.py` 的 TTADKManager 方法提取为独立模块级函数，
保持向后兼容（manager.py 中的方法将委托到这些函数）。
"""

from typing import Optional

from .models import TTADKModel

# These keys are also defined in manager.py; imported here to avoid circular dependency.
MODEL_KEYS = ("models", "model_list", "available_models", "ai_models", "llm_models", "llms")
TOOL_KEYS = ("tools", "ai_tools", "providers", "toolkits")


def extract_models_from_sync(
    data: object,
    tool_name: Optional[str],
    current_model: Optional[str],
) -> list[TTADKModel]:
    # Tool-specific lookup
    if isinstance(data, dict) and tool_name:
        for key in TOOL_KEYS:
            container = data.get(key)
            models = extract_models_from_tool_container(container, tool_name, current_model)
            if models:
                return models

    # Fallback: search anywhere
    models = extract_models_from_container(data, current_model, under_model_key=False)
    return models


def extract_models_from_tool_container(
    container: object,
    tool_name: str,
    current_model: Optional[str],
) -> list[TTADKModel]:
    if isinstance(container, dict):
        if tool_name in container:
            return extract_models_from_container(
                container.get(tool_name),
                current_model,
                under_model_key=False,
            )
    elif isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                name = item.get("name") or item.get("tool") or item.get("id")
                if name == tool_name:
                    return extract_models_from_container(
                        item,
                        current_model,
                        under_model_key=False,
                    )
    return []


def extract_models_from_container(
    container: object,
    current_model: Optional[str],
    under_model_key: bool,
) -> list[TTADKModel]:
    if isinstance(container, dict):
        for key in MODEL_KEYS:
            if key in container:
                models = normalize_models(container.get(key), current_model)
                if models:
                    return models
        for value in container.values():
            models = extract_models_from_container(value, current_model, under_model_key=False)
            if models:
                return models
    elif isinstance(container, list):
        if under_model_key:
            models = normalize_models(container, current_model)
            if models:
                return models
        for item in container:
            models = extract_models_from_container(item, current_model, under_model_key=False)
            if models:
                return models
    return []


def normalize_models(raw: object, current_model: Optional[str]) -> list[TTADKModel]:
    if isinstance(raw, list):
        if raw and all(isinstance(x, str) for x in raw):
            return [TTADKModel(name=name, description=name, is_default=(name == current_model)) for name in raw]
        models: list[TTADKModel] = []
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("model") or item.get("model_name")
                if not name:
                    continue
                desc = item.get("description") or item.get("label") or str(name)
                models.append(
                    TTADKModel(
                        name=str(name),
                        description=str(desc),
                        is_default=(str(name) == current_model),
                    )
                )
        return models
    if isinstance(raw, dict):
        # Map of name -> details
        models: list[TTADKModel] = []
        for name, item in raw.items():
            if isinstance(item, dict):
                desc = item.get("description") or item.get("label") or str(name)
            else:
                desc = str(item)
            models.append(
                TTADKModel(
                    name=str(name),
                    description=str(desc),
                    is_default=(str(name) == current_model),
                )
            )
        return models
    return []
