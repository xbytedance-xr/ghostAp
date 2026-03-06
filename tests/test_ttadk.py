import pytest
from src.ttadk import TTADKManager, get_ttadk_manager, TTADKTool, TTADKModel


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
