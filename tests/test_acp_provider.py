from src.acp.provider import ACPProvider, ToolRegistry
from src.acp.providers.coco import CocoProvider
from src.acp.providers.claude import ClaudeProvider


class MockProvider(ACPProvider):
    def __init__(self, name: str, available: bool = True):
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    def check_availability(self) -> bool:
        return self._available

    def get_serve_command(self, model_name=None) -> tuple[str, list[str]]:
        args = ["mock", "serve"]
        if model_name:
            args.extend(["-m", model_name])
        return self._name, args

    def get_fallback_command(self, model_name=None) -> tuple[str, list[str]] | None:
        if not self._available:
            return self._name, ["fallback"]
        return None


def test_registry_registration():
    registry = ToolRegistry()
    provider = MockProvider("test_tool")
    
    registry.register(provider)
    assert registry.get_provider("test_tool") is provider
    assert registry.get_provider("TEST_TOOL") is provider  # case insensitive
    assert registry.get_provider("non_existent") is None


def test_registry_get_serve_command_available():
    registry = ToolRegistry()
    provider = MockProvider("test_tool", available=True)
    registry.register(provider)
    
    cmd, args = registry.get_serve_command("test_tool", model_name="gpt-4")
    assert cmd == "test_tool"
    assert args == ["mock", "serve", "-m", "gpt-4"]


def test_registry_get_serve_command_fallback():
    registry = ToolRegistry()
    provider = MockProvider("test_tool", available=False)
    registry.register(provider)
    
    cmd, args = registry.get_serve_command("test_tool")
    assert cmd == "test_tool"
    assert args == ["fallback"]


def test_registry_rechecks_stale_negative_cache_for_hot_tool():
    registry = ToolRegistry()
    provider = MockProvider("coco", available=True)
    registry.register(provider)
    registry._set_availability_cache("coco", False)

    cmd, args = registry.get_serve_command("coco", model_name="gpt-4")
    assert cmd == "coco"
    assert args == ["mock", "serve", "-m", "gpt-4"]


def test_registry_get_serve_command_unregistered_fallback():
    registry = ToolRegistry()
    
    cmd, args = registry.get_serve_command("unknown_tool")
    assert cmd == "unknown_tool"
    assert args == ["acp", "serve"]


def test_coco_provider():
    provider = CocoProvider()
    assert provider.name == "coco"
    
    cmd, args = provider.get_serve_command(model_name="test-model")
    assert cmd == "coco"
    assert args == ["acp", "serve", "-c", "model.name=test-model"]
    
    cmd, args = provider.get_serve_command()
    assert cmd == "coco"
    assert args == ["acp", "serve"]


def test_claude_provider():
    provider = ClaudeProvider()
    assert provider.name == "claude"
    
    cmd, args = provider.get_serve_command(model_name="test-model")
    assert cmd == "claude"
    assert args == ["acp", "serve"]


def test_registry_rejects_empty_provider_name():
    class BadProvider:
        @property
        def name(self) -> str:  # type: ignore[override]
            return ""

        def check_availability(self) -> bool:
            return True

        def get_serve_command(self, model_name=None):
            return ("bad", ["acp", "serve"])

        def get_fallback_command(self, model_name=None):
            return None

    r = ToolRegistry()
    try:
        r.register(BadProvider())
        assert False, "should raise"
    except ValueError:
        pass
