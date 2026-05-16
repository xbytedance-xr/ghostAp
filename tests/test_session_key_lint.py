from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

import pytest

from src.acp.helper import SessionKeyCodec
from src.utils.text import render_violation_report

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


RECOMMENDED_FIX_NOTE = (
    "检测到对 session_key 的手工解析，请改用 ACPSessionManager._parse_session_key(...) "
    "或项目中既有的解析辅助函数，避免手写字符串拆分逻辑。"
)


@dataclass
class Violation:
    file: Path
    lineno: int
    message: str


def _is_session_key_attr(node: ast.AST) -> bool:
    """Return True if node ultimately resolves to a name/attr called 'session_key'."""

    # Name("session_key")
    if isinstance(node, ast.Name) and node.id == "session_key":
        return True

    # obj.session_key or obj.foo.session_key
    if isinstance(node, ast.Attribute):
        attr = node.attr
        if attr == "session_key":
            return True
        # walk down attribute chain: obj.foo.session_key
        current = node
        while isinstance(current, ast.Attribute):
            if current.attr == "session_key":
                return True
            current = current.value

    return False


def _call_uses_colon_separator(call: ast.Call) -> bool:
    """Return True if first argument looks like a ':' separator.

    This keeps the rule非常狭窄，避免误伤其他 split 用法。
    """

    if not call.args:
        return False

    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return ":" in first.value

    return False


def detect_manual_session_key_parsing(source: str) -> List[Tuple[int, str]]:
    """在给定源码字符串中检测 session_key 的手工解析反模式。

    返回 (lineno, message) 列表；规则尽量保持"窄":
    - 仅匹配 `session_key.split(":")` / `session_key.partition(":")` 及其属性变体；
    - 其它字符串的 split/partition 不在本规则覆盖范围。
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 非法代码直接忽略，不在静态检查范围
        return []

    violations: List[Tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if not isinstance(func, ast.Attribute):
            continue

        if func.attr not in {"split", "partition"}:
            continue

        target = func.value
        if not _is_session_key_attr(target):
            continue

        if not _call_uses_colon_separator(node):
            continue

        # 短原因：仅说明“哪种方式解析 session_key 被禁止”，修复建议统一由
        # `_format_violations` 在头部集中输出，避免每行重复噪音。
        msg = "manual parsing of session_key via %s(':') is forbidden" % func.attr
        violations.append((node.lineno, msg))

    return violations


def scan_session_key_anti_patterns(paths: Iterable[Path]) -> List[Violation]:
    """扫描给定文件/目录中的 Python 源码，收集所有 session_key 手工解析反模式。"""

    results: List[Violation] = []

    def iter_py_files(path: Path) -> Iterable[Path]:
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            for p in path.rglob("*.py"):
                # 粗略排除虚拟环境或隐藏目录
                parts = set(p.parts)
                if any(x in parts for x in {".venv", "venv", "__pycache__"}):
                    continue
                yield p

    for path in paths:
        for py_file in iter_py_files(path):
            try:
                source = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            for lineno, message in detect_manual_session_key_parsing(source):
                results.append(Violation(file=py_file, lineno=lineno, message=message))

    return results


def _format_violations(violations: List[Violation]) -> str:
    body_lines: List[str] = []
    for v in violations:
        try:
            rel = v.file.relative_to(ROOT_DIR)
        except ValueError:
            rel = v.file
        body_lines.append(f"- {rel}:{v.lineno}: {v.message}")

    return render_violation_report(
        title="发现针对 session_key 的手工字符串解析反模式:",
        recommended_fix=RECOMMENDED_FIX_NOTE,
        violation_lines=body_lines,
    )


def assert_no_manual_session_key_parsing(paths: Iterable[Path]) -> None:
    """针对给定路径集合做 Lint 断言：一旦发现违规即抛出 pytest 失败。"""

    violations = scan_session_key_anti_patterns(paths)
    if violations:
        pytest.fail(_format_violations(violations))


def test_detect_manual_session_key_parsing_positive_cases() -> None:
    """正向用例：典型反模式必须被检测到。"""

    src = """
session_key = "chat:proj"
user = session_key.split(":")
other = obj.session_key.partition(":")
"""
    violations = detect_manual_session_key_parsing(src)
    assert violations, "expected to detect manual parsing of session_key"
    # 至少命中两处
    assert len(violations) >= 2

    # 每条违规信息只给出“短原因”，修复建议由 _format_violations 统一在头部输出
    for _lineno, message in violations:
        assert "manual parsing of session_key" in message
        assert RECOMMENDED_FIX_NOTE not in message


def test_format_violations_header_and_deduplicated_fix_note() -> None:
    """_format_violations 只在头部输出一次修复建议，后续每行仅含位置与短原因。"""

    violations = [
        Violation(file=ROOT_DIR / "src/foo.py", lineno=10, message="reason-one"),
        Violation(file=ROOT_DIR / "src/bar.py", lineno=20, message="reason-two"),
    ]

    formatted = _format_violations(violations)
    lines = formatted.splitlines()

    # 头部结构：标题 → 空行 → 【推荐修复方式】 → 修复说明 → 空行
    assert lines[0].startswith("发现针对 session_key 的手工字符串解析反模式")
    assert lines[1] == ""
    assert "【推荐修复方式】" == lines[2]
    assert RECOMMENDED_FIX_NOTE in lines[3]
    assert lines[4] == ""
    assert formatted.count(RECOMMENDED_FIX_NOTE) == 1

    # 违规列表行：以 "- " 开头，且只包含位置 + 短原因，不重复修复建议
    body_lines = [line for line in lines if line.startswith("- ")]
    assert body_lines, "expected formatted violations to contain body lines"
    for line in body_lines:
        assert line.startswith("- ")
        assert RECOMMENDED_FIX_NOTE not in line
    # 短原因必须出现在对应行中
    assert any("reason-one" in line for line in body_lines)
    assert any("reason-two" in line for line in body_lines)


def test_detect_manual_session_key_parsing_negative_cases() -> None:
    """反向用例：合法解析方式不得被误报。"""

    src = """
from src.acp.manager import ACPSessionManager

def handle(key: str) -> None:
    chat_id, project_id, thread_id = ACPSessionManager._parse_session_key(key)
    parts = "not_session_key".split(":")
"""
    violations = detect_manual_session_key_parsing(src)
    assert violations == []


def test_assert_no_manual_session_key_parsing_on_tmp_file(tmp_path: Path) -> None:
    """集成级测试：当存在违规样本文件时，断言函数会触发 pytest 失败。

    注意：这里只在临时目录下构造示例，不影响真实 src/ 代码。
    """

    bad_code = """
session_key = "chat:proj"
parts = session_key.split(":")
"""
    bad_file = tmp_path / "bad_session_key_usage.py"
    bad_file.write_text(bad_code, encoding="utf-8")

    # pytest.fail(...) 实际抛出的异常类型是 Failed，位于 pytest 子模块中
    from _pytest.outcomes import Failed

    with pytest.raises(Failed) as excinfo:
        assert_no_manual_session_key_parsing([bad_file])

    # 报错信息中需要包含一次清晰的修复建议，且不在每条违规行中重复
    failure_message = str(excinfo.value)
    assert RECOMMENDED_FIX_NOTE in failure_message
    assert failure_message.count(RECOMMENDED_FIX_NOTE) == 1

    lines = failure_message.splitlines()
    # 仅针对真正的违规列表行做断言（以 "- " 开头），头部结构允许包含修复说明
    for line in lines:
        if line.startswith("- "):
            assert RECOMMENDED_FIX_NOTE not in line


def test_no_manual_session_key_parsing_in_src() -> None:
    """对真实业务代码做静态检查：禁止手工解析 session_key。

    该用例将作为 CI 层的“协议收口 Lint Gate”，一旦有人在 src/ 中用
    session_key.split(":") 或 session_key.partition(":") 解析协议，即会在
    本地/CI pytest 中立刻红灯。
    """

    assert_no_manual_session_key_parsing([SRC_DIR])


class TestSessionKeyCodec:
    def test_encode_decode_roundtrip_default_project(self) -> None:
        """SessionKeyCodec 在默认 project 场景下应与现有协议语义一致。"""

        key = SessionKeyCodec.encode("chat-default")

        chat_id, project_id, thread_id = SessionKeyCodec.decode(key)
        assert chat_id == "chat-default"
        # 默认项目应当被折叠为 None，而不是暴露占位符
        assert project_id is None
        assert thread_id is None

    def test_encode_decode_roundtrip_with_project_and_thread(self) -> None:
        key = SessionKeyCodec.encode("chat-ctx", project_id="proj-ctx", thread_id="thread-ctx")

        chat_id, project_id, thread_id = SessionKeyCodec.decode(key)

        assert chat_id == "chat-ctx"
        assert project_id == "proj-ctx"
        assert thread_id == "thread-ctx"

    def test_decode_handles_empty_and_non_string_input(self) -> None:
        chat_id, project_id, thread_id = SessionKeyCodec.decode("")
        assert chat_id == ""
        assert project_id is None
        assert thread_id is None

        chat_id2, project_id2, thread_id2 = SessionKeyCodec.decode(12345)  # type: ignore[arg-type]
        assert isinstance(chat_id2, str)
        assert project_id2 is None
        assert thread_id2 is None

    def test_decode_treats_default_placeholder_as_none(self) -> None:
        """使用占位符 `_default_` 的旧 key 应折叠为 `project_id=None`。"""

        key = f"chat-x:{SessionKeyCodec.DEFAULT_PROJECT_PLACEHOLDER}"
        chat_id, project_id, thread_id = SessionKeyCodec.decode(key)
        assert chat_id == "chat-x"
        assert project_id is None
        assert thread_id is None

    def test_decode_handles_minimal_legacy_key(self) -> None:
        """只有 chat_id 一段的历史 key 应被视为「无 project/thread」。"""

        chat_id, project_id, thread_id = SessionKeyCodec.decode("legacy-chat-only")
        assert chat_id == "legacy-chat-only"
        assert project_id is None
        assert thread_id is None
