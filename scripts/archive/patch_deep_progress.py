
with open("src/deep_engine/progress.py", "r") as f:
    content = f.read()

helper_func = """def _truncate_nested_data(data: Any, max_depth: int = 10, current_depth: int = 0) -> Any:
    \"\"\"Truncate nested data structures to prevent recursion explosion.\"\"\"
    if current_depth >= max_depth:
        return "[TRUNCATED: MAX DEPTH EXCEEDED]"
        
    if isinstance(data, dict):
        return {k: _truncate_nested_data(v, max_depth, current_depth + 1) for k, v in data.items()}
    elif isinstance(data, list):
        return [_truncate_nested_data(item, max_depth, current_depth + 1) for item in data]
    elif isinstance(data, tuple):
        return tuple(_truncate_nested_data(item, max_depth, current_depth + 1) for item in data)
    return data
"""

if "_truncate_nested_data" not in content:
    content = content.replace("from ..utils.text import make_progress_bar", "from ..utils.text import make_progress_bar\nfrom typing import Any\n\n" + helper_func)

record_tool_str = """    def record_tool(self, tool: ToolCallInfo) -> None:
        self.tool_calls.append(tool)
        for loc in tool.locations:
            self.modified_files.add(loc)"""

record_tool_repl = """    def record_tool(self, tool: ToolCallInfo) -> None:
        # Prevent stack explosion on highly nested tool results
        if tool.result:
            tool.result = _truncate_nested_data(tool.result)
            
        self.tool_calls.append(tool)
        for loc in tool.locations:
            self.modified_files.add(loc)"""

content = content.replace(record_tool_str, record_tool_repl)

with open("src/deep_engine/progress.py", "w") as f:
    f.write(content)
