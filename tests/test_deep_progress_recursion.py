from src.acp.models import ToolCallInfo
from src.deep_engine.progress import DeepProgress, _truncate_nested_data


def test_truncate_nested_data():
    # Construct a 15-level nested dict
    nested = {}
    current = nested
    for _ in range(15):
        current["child"] = {}
        current = current["child"]

    current["value"] = 42

    truncated = _truncate_nested_data(nested, max_depth=10)

    # Verify depth 10
    curr = truncated
    for _i in range(10):
        assert "child" in curr
        curr = curr["child"]

    assert curr == "[TRUNCATED: MAX DEPTH EXCEEDED]"


def test_deep_progress_record_tool_truncates():
    nested = {
        "level1": {
            "level2": {
                "level3": {
                    "level4": {
                        "level5": {"level6": {"level7": {"level8": {"level9": {"level10": {"level11": "too deep"}}}}}}
                    }
                }
            }
        }
    }
    tool_info = ToolCallInfo(id="t1", title="test", kind="read", status="completed", result=nested)

    progress = DeepProgress()
    progress.record_tool(tool_info)

    # Ensure no exception occurred and the result was truncated
    assert (
        progress.tool_calls[0].result["level1"]["level2"]["level3"]["level4"]["level5"]["level6"]["level7"]["level8"][
            "level9"
        ]["level10"]
        == "[TRUNCATED: MAX DEPTH EXCEEDED]"
    )
