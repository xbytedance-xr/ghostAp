"""Worker - fixed isolated entrypoint for sandboxed execution.

This module is the ONLY file executed inside the sandbox.
It reads a task from stdin (JSON), executes it, and writes the result to stdout.

SECURITY: This module must NOT import anything from the autonomous domain,
journal, broker, or any other internal module. Only stdlib is allowed.
"""

from __future__ import annotations

import json
import sys
import traceback


# ---------------------------------------------------------------------------
# Approved imports (stdlib only)
# ---------------------------------------------------------------------------

APPROVED_MODULES = frozenset([
    "json",
    "os",
    "sys",
    "time",
    "hashlib",
    "math",
    "re",
    "collections",
    "itertools",
    "functools",
    "pathlib",
    "textwrap",
    "copy",
    "uuid",
    "base64",
    "datetime",
    "string",
    "io",
])


# ---------------------------------------------------------------------------
# Worker protocol
# ---------------------------------------------------------------------------


def execute_task(task: dict) -> dict:
    """Execute a task payload and return a result dict.

    Task format:
        {
            "task_type": "eval" | "shell" | "transform",
            "payload": { ... task-specific data ... },
            "timeout": <optional float>
        }

    Result format:
        {
            "success": bool,
            "output": <any JSON-serializable>,
            "error": <str or null>
        }
    """
    task_type = task.get("task_type", "")

    if task_type == "eval":
        return _execute_eval(task.get("payload", {}))
    elif task_type == "transform":
        return _execute_transform(task.get("payload", {}))
    else:
        return {
            "success": False,
            "output": None,
            "error": f"Unknown task_type: {task_type}",
        }


def _execute_eval(payload: dict) -> dict:
    """Evaluate a simple expression (no exec, no imports beyond approved)."""
    expr = payload.get("expression", "")
    if not expr:
        return {"success": False, "output": None, "error": "Empty expression"}

    # Security: no dunders, no import statements in expression
    if "__" in expr or "import" in expr:
        return {
            "success": False,
            "output": None,
            "error": "Forbidden: expression contains restricted patterns",
        }

    try:
        # Only allow basic math/string builtins
        safe_globals = {"__builtins__": {
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "round": round,
            "sorted": sorted,
            "enumerate": enumerate,
            "range": range,
            "zip": zip,
            "map": map,
            "filter": filter,
            "any": any,
            "all": all,
            "isinstance": isinstance,
            "type": type,
            "None": None,
            "True": True,
            "False": False,
        }}
        result = eval(expr, safe_globals, payload.get("context", {}))  # noqa: S307
        return {"success": True, "output": result, "error": None}
    except Exception as exc:
        return {"success": False, "output": None, "error": str(exc)}


def _execute_transform(payload: dict) -> dict:
    """Apply a data transformation (JSON-in, JSON-out)."""
    data = payload.get("data")
    operation = payload.get("operation", "")

    if operation == "keys":
        if isinstance(data, dict):
            return {"success": True, "output": list(data.keys()), "error": None}
        return {"success": False, "output": None, "error": "data is not a dict"}
    elif operation == "length":
        try:
            return {"success": True, "output": len(data), "error": None}
        except TypeError:
            return {"success": False, "output": None, "error": "data has no length"}
    elif operation == "flatten":
        if isinstance(data, list):
            flat = []
            for item in data:
                if isinstance(item, list):
                    flat.extend(item)
                else:
                    flat.append(item)
            return {"success": True, "output": flat, "error": None}
        return {"success": False, "output": None, "error": "data is not a list"}
    else:
        return {"success": False, "output": None, "error": f"Unknown operation: {operation}"}


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Read task from stdin, execute, write result to stdout."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            result = {"success": False, "output": None, "error": "Empty input"}
        else:
            task = json.loads(raw)
            result = execute_task(task)
    except json.JSONDecodeError as exc:
        result = {"success": False, "output": None, "error": f"Invalid JSON: {exc}"}
    except Exception as exc:
        result = {
            "success": False,
            "output": None,
            "error": f"Worker error: {exc}\n{traceback.format_exc()}",
        }

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
