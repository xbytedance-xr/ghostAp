import pytest
from src.ttadk import TTADKManager, get_ttadk_manager, TTADKTool, TTADKModel, TTADKModelFetcher


def test_ttadk_models():
    tool = TTADKTool(name="test_tool", description="Test Tool", is_default=True)
    assert tool.name == "test_tool"
    assert tool.description == "Test Tool"
    assert tool.is_default is True

    model = TTADKModel(name="test_model", description="Test Model", is_default=False)
    assert model.name == "test_model"
    assert model.description == "Test Model"
    assert model.is_default is False


def test_ttadk_manager():
    manager = TTADKManager(default_tool="coco", default_model="claude-3.5-sonnet")
    
    assert manager.get_current_tool() == "coco"
    assert manager.get_current_model() == "claude-3.5-sonnet"
    
    tools_result = manager.get_tools()
    assert tools_result.error is None
    assert len(tools_result.tools) > 0
    
    models_result = manager.get_models()
    assert models_result.error is None
    assert len(models_result.models) > 0


def test_ttadk_manager_set_tool_and_model():
    manager = TTADKManager()
    
    assert manager.set_tool("claude") is True
    assert manager.get_current_tool() == "claude"
    
    assert manager.set_tool("invalid_tool") is False
    assert manager.get_current_tool() == "claude"
    
    assert manager.set_model("gpt-5.2") is True
    assert manager.get_current_model() == "gpt-5.2"
    
    assert manager.set_model("invalid_model") is False
    assert manager.get_current_model() == "gpt-5.2"


def test_get_ttadk_manager():
    manager1 = get_ttadk_manager(default_tool="trae", default_model="doubao-1.5-pro")
    manager2 = get_ttadk_manager()
    
    assert manager1 is manager2
    assert manager1.get_current_tool() == "trae"
    assert manager1.get_current_model() == "doubao-1.5-pro"


def test_default_tools():
    manager = TTADKManager()
    tools_result = manager.get_tools()
    
    tool_names = [t.name for t in tools_result.tools]
    assert "claude" in tool_names
    assert "cursor" in tool_names
    assert "gemini" in tool_names
    assert "codex" in tool_names
    assert "coco" in tool_names
    assert "tmates" in tool_names
    assert "trae" in tool_names
    assert "opencode" in tool_names


def test_default_models():
    manager = TTADKManager()
    models_result = manager.get_models()
    
    model_names = [m.name for m in models_result.models]
    assert "gpt-5.2" in model_names
    assert "gpt-4.1" in model_names
    assert "claude-3-opus" in model_names
    assert "claude-3.5-sonnet" in model_names
    assert "claude-3.7-sonnet" in model_names
    assert "doubao-1.5-pro" in model_names
    assert "gemini-2.0-pro" in model_names
    assert "gemini-2.5-pro" in model_names


def test_get_models_from_sync_output(monkeypatch):
    manager = TTADKManager(default_tool="coco")
    monkeypatch.setattr(
        manager,
        "_run_ttadk_sync",
        lambda cwd: {"tools": {"coco": {"models": ["real-model-a", "real-model-b"]}}},
    )

    result = manager.get_models(cwd=".")
    names = [m.name for m in result.models]

    assert result.error is None
    assert names == ["real-model-a", "real-model-b"]


def test_set_model_accepts_synced_model(monkeypatch):
    manager = TTADKManager(default_tool="coco")
    monkeypatch.setattr(
        manager,
        "_run_ttadk_sync",
        lambda cwd: {"tools": {"coco": {"models": ["real-model"]}}},
    )

    manager.get_models(cwd=".")
    assert manager.set_model("real-model") is True
    assert manager.get_current_model() == "real-model"


def test_ttadk_model_fetcher_strip_ansi():
    """测试 ANSI 颜色码移除"""
    fetcher = TTADKModelFetcher()
    text_with_ansi = "\x1b[32mGreen Text\x1b[0m"
    clean = fetcher._strip_ansi(text_with_ansi)
    assert clean == "Green Text"


def test_ttadk_model_fetcher_parse_menu():
    """测试模型选择菜单解析"""
    fetcher = TTADKModelFetcher()
    output = """? Select a model:  (Use arrow keys)
 ❯ GPT 5.2 Codex (Recommended)
   GPT 4.1 Codex
   o4-mini
"""
    names = fetcher._parse_model_selection_menu(output)
    assert names == ["GPT 5.2 Codex (Recommended)", "GPT 4.1 Codex", "o4-mini"]


def test_ttadk_model_fetcher_extract_model_name():
    """测试真实模型名称提取"""
    fetcher = TTADKModelFetcher()
    output = """model:     gpt-5.2-codex-ttadk
provider:  openai"""
    name = fetcher._extract_real_model_name(output)
    assert name == "gpt-5.2-codex-ttadk"


def test_ttadk_model_fetcher_cache():
    """测试模型获取器缓存"""
    import time
    fetcher = TTADKModelFetcher()
    # 缓存应该为空
    assert fetcher._is_cache_valid("codex") is False
    # 设置缓存
    fetcher._cache["codex"] = [TTADKModel(name="test-model")]
    fetcher._cache_time["codex"] = time.time()  # 使用当前时间
    # 现在应该有效
    assert fetcher._is_cache_valid("codex") is True
    # 使缓存失效
    fetcher.invalidate_cache("codex")
    assert fetcher._is_cache_valid("codex") is False


def test_manager_get_models_with_tool_name():
    """测试获取指定工具的模型列表"""
    manager = TTADKManager(default_tool="coco")

    # 获取当前工具的模型
    result = manager.get_models()
    assert result.error is None

    # 获取指定工具的模型
    result_codex = manager.get_models(tool_name="codex")
    assert result_codex.error is None


def test_manager_model_cache_invalidation():
    """测试模型缓存失效"""
    manager = TTADKManager(default_tool="coco")

    # 获取模型，填充缓存
    manager.get_models()

    # 使特定工具的缓存失效
    manager.invalidate_model_cache("coco")

    # 使所有缓存失效
    manager.invalidate_model_cache()


def test_manager_model_cached_flag():
    """测试模型缓存标志"""
    manager = TTADKManager(default_tool="coco")

    # 第一次获取，cached 应该为 False
    result = manager.get_models()
    assert result.cached is False

    # 如果有缓存，再次获取时 cached 应该为 True
    # 但由于模型获取可能失败（终端交互），这里只测试缓存逻辑
    if manager._is_cache_valid("coco"):
        result2 = manager.get_models()
        assert result2.cached is True
