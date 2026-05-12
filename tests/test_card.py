import json
from unittest.mock import MagicMock, patch

import pytest

from src.card.builder import CardBuilder
from src.card.models import EngineCardState
from src.card.shared import THEMES, get_theme
from src.config import Settings
from src.project.context import CocoSessionSnapshot, ProjectContext
from src.ttadk.models import TTADKModel, TTADKTool


class TestProjectTheme:
    def test_get_theme(self):
        theme = get_theme("green")

        assert theme.name == "green"
        assert theme.emoji == "🟢"
        assert theme.header_template == "green"

    def test_get_unknown_theme_returns_default(self):
        theme = get_theme("unknown_color")

        assert theme.name == "green"

    def test_all_themes_exist(self):
        expected_colors = ["green", "blue", "purple", "orange", "red", "turquoise"]

        for color in expected_colors:
            assert color in THEMES


class TestCardBuilder:
    @pytest.fixture
    def sample_project(self):
        return ProjectContext(
            project_id="test_project",
            project_name="Test Project",
            root_path="/tmp/test",
            working_dir="/tmp/test/src",
            theme_color="green",
            emoji_prefix="🟢",
        )

    def test_build_project_response_card(self, sample_project):
        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Test Title",
            content="Test content here",
            show_buttons=True,
        )

        assert msg_type == "interactive"

        card = json.loads(content)
        assert card["config"]["wide_screen_mode"] is True
        assert "Test Project" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "green"

        elements = card["body"]["elements"]
        assert any("Test Title" in str(e) for e in elements)
        assert any("Test content here" in str(e) for e in elements)
        # Schema 2.0: 按钮统一使用 column_set 布局
        column_set_elements = [e for e in elements if e.get("tag") == "column_set"]
        assert len(column_set_elements) >= 1
        columns = column_set_elements[0]["columns"]
        assert len(columns) >= 1
        for col in columns:
            if col["elements"]:
                btn = col["elements"][0]
                assert "behaviors" in btn
                assert btn["behaviors"][0]["type"] == "callback"
                assert isinstance(btn["behaviors"][0]["value"], dict)
                assert btn.get("size") == "medium"

    def test_build_project_response_card_mobile_layout_forces_grid(self, sample_project):
        # 验证 mobile 布局：<=2 个按钮也应使用 column_set（避免小屏自动换行堆叠）
        from src.card.shared import build_responsive_layout

        buttons = [
            {"tag": "button", "text": {"tag": "plain_text", "content": "Btn1"}, "type": "primary"},
            {"tag": "button", "text": {"tag": "plain_text", "content": "Btn2"}, "type": "default"},
        ]
        result = build_responsive_layout(buttons, layout="mobile")
        assert len(result) > 0
        assert all(e.get("tag") == "column_set" for e in result)
        # Mobile layout forces flex_mode=none (vertical stack)
        assert all(e.get("flex_mode") == "none" for e in result)

    def test_build_ttadk_model_select_card_includes_refresh_button(self):
        """TTADK 模型选择卡：包含『刷新模型列表』按钮，便于 Invalid model 自助修复。"""
        models = [
            TTADKModel(name="gpt-5.2-codex-ttadk", description="", is_default=True),
            TTADKModel(name="gpt-5.2-ttadk", description="", is_default=False),
        ]
        msg_type, content = CardBuilder.build_ttadk_model_select_card(models, tool_name="codex", project_id="p1")
        assert msg_type == "interactive"

        card = json.loads(content)
        elements = card["body"]["elements"]

        # 收集所有按钮（column_set -> columns -> elements）
        buttons: list[dict] = []
        for e in elements:
            if e.get("tag") != "column_set":
                continue
            for col in e.get("columns", []) or []:
                for el in col.get("elements", []) or []:
                    if isinstance(el, dict) and el.get("tag") == "button":
                        buttons.append(el)

        refresh = next(
            (b for b in buttons if (b.get("text") or {}).get("content") == "🔄 刷新模型列表"),
            None,
        )
        assert refresh is not None
        assert (refresh.get("value") or {}).get("action") == "refresh_ttadk_models"
        assert (refresh.get("value") or {}).get("tool_name") == "codex"
        assert (refresh.get("value") or {}).get("project_id") == "p1"

        yolo_toggle = next((b for b in buttons if (b.get("value") or {}).get("action") == "toggle_ttadk_yolo"), None)
        assert yolo_toggle is not None
        assert (yolo_toggle.get("value") or {}).get("enabled") is True

    def test_build_info_card_progress_bar_not_duplicated_when_content_contains_bar(self):
        # 兜底：即使调用方把 progress_bar 文本拼到 content，build_info_card 也不应重复渲染
        progress_bar = "[████░░░░░░] 40% (2/5)"
        content = f"任务中...\n\n{progress_bar}\n"
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="X",
                content=content,
                progress_bar=progress_bar,
                engine_name="Coco",
                show_buttons=False,
            ),
        )
        card = json.loads(card_content)
        progress_elems = [
            e
            for e in card.get("body", {}).get("elements", [])
            if e.get("tag") == "markdown" and str(e.get("content", "")).startswith("📊 ")
        ]
        assert len(progress_elems) == 0

    def test_build_project_response_card_coco_mode(self, sample_project):
        sample_project.coco_mode = True

        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Coco Response",
            content="AI response",
        )

        card = json.loads(content)
        assert "🤖" in card["header"]["title"]["content"]

    def test_build_project_response_card_claude_mode(self, sample_project):
        sample_project.claude_mode = True

        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Claude Response",
            content="AI response",
        )

        assert msg_type == "interactive"
        card = json.loads(content)
        assert "🔮" in card["header"]["title"]["content"]
        assert "Claude" in card["header"]["title"]["content"]

    def test_build_project_response_card_ttadk_status_bar(self, sample_project):
        sample_project.ttadk_mode = True
        sample_project.ttadk_tool_name = "codex"
        sample_project.ttadk_model_name = "gpt-5.2"
        sample_project.ttadk_yolo_enabled = True

        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="TTADK",
            content="执行中",
        )

        assert msg_type == "interactive"
        content_str = json.dumps(json.loads(content), ensure_ascii=False)
        assert "TTADK 状态" in content_str
        assert "工具: `codex`" in content_str
        assert "模型: `gpt-5.2`" in content_str
        assert "自动执行: `开启`" in content_str

    def test_build_project_response_card_no_buttons(self, sample_project):
        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Title",
            content="Content",
            show_buttons=False,
        )

        card = json.loads(content)
        column_set_elements = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
        assert len(column_set_elements) == 0

    def test_build_status_board_card_empty(self):
        msg_type, content = CardBuilder.build_status_board_card([], None)

        assert msg_type == "interactive"

        card = json.loads(content)
        assert "项目看板" in card["header"]["title"]["content"]
        assert any("暂无项目" in str(e) for e in card["body"]["elements"])

    def test_build_status_board_card_with_projects(self, sample_project):
        project2 = ProjectContext(
            project_id="proj2",
            project_name="Project 2",
            root_path="/tmp/proj2",
            theme_color="blue",
            emoji_prefix="🔵",
        )

        msg_type, content = CardBuilder.build_status_board_card(
            [sample_project, project2],
            current_project_id="test_project",
        )

        card = json.loads(content)

        content_str = json.dumps(card, ensure_ascii=False)
        assert "Test Project" in content_str
        assert "Project 2" in content_str
        assert "(当前)" in content_str

    def test_build_notification_card(self, sample_project):
        msg_type, content = CardBuilder.build_notification_card(
            project=sample_project,
            notification_type="success",
            title="Task Complete",
            content="The task has been completed successfully.",
            suggestions=["Run tests", "Deploy to staging"],
        )

        assert msg_type == "interactive"

        card = json.loads(content)
        assert "✅" in card["header"]["title"]["content"]
        assert any("建议下一步" in str(e) for e in card["body"]["elements"])

    def test_build_coco_resume_card(self, sample_project):
        sample_project.coco_session_snapshot = CocoSessionSnapshot(
            session_id="session_123",
            query_count=10,
            last_query="帮我写一个函数",
            is_resumable=True,
        )

        msg_type, content = CardBuilder.build_coco_resume_card(sample_project)

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)

        assert "session_123" in content_str
        assert "恢复会话" in content_str

    def test_build_ttadk_resume_card_includes_status_bar(self, sample_project):
        sample_project.ttadk_tool_name = "claude"
        sample_project.ttadk_model_name = "gpt-5.2-ttadk"
        sample_project.ttadk_yolo_enabled = False
        sample_project.ttadk_session_snapshot = CocoSessionSnapshot(
            session_id="session_ttadk",
            query_count=3,
            last_query="继续重构",
            is_resumable=True,
        )

        msg_type, content = CardBuilder.build_ttadk_resume_card(sample_project)

        assert msg_type == "interactive"
        content_str = json.dumps(json.loads(content), ensure_ascii=False)
        assert "TTADK 状态" in content_str
        assert "工具: `claude`" in content_str
        assert "模型: `gpt-5.2-ttadk`" in content_str
        assert "自动执行: `关闭`" in content_str

    def test_build_project_created_card(self, sample_project):
        msg_type, content = CardBuilder.build_project_created_card(sample_project)

        card = json.loads(content)

        assert "新项目已创建" in card["header"]["title"]["content"]
        content_str = json.dumps(card)
        assert "Test Project" in content_str
        assert "test_project" in content_str

    def test_build_error_card_without_project(self):
        msg_type, content = CardBuilder.build_error_card("Something went wrong")

        card = json.loads(content)

        assert card["header"]["template"] == "red"
        assert any("Something went wrong" in str(e) for e in card["body"]["elements"])

    def test_build_error_card_with_project(self, sample_project):
        msg_type, content = CardBuilder.build_error_card(
            "Error message",
            project=sample_project,
        )

        card = json.loads(content)
        content_str = json.dumps(card)

        assert "/tmp/test" in content_str
        assert "Error message" in content_str

    def test_build_error_card_with_ghost_ap_error(self):
        from src.utils.errors import GhostAPError

        err = GhostAPError("Business error", quick_actions=["retry", "cancel"])
        msg_type, content = CardBuilder.build_error_card(err)

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)
        assert "Business error" in content_str
        assert "重试" in content_str
        assert "取消" in content_str

    def test_build_project_response_card_with_images(self, sample_project):
        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Claude",
            content="分析结果",
            image_keys=["img_v2_abc", "img_v2_def"],
        )

        card = json.loads(content)
        elements = card["body"]["elements"]
        img_elements = [e for e in elements if e.get("tag") == "img"]
        assert len(img_elements) == 2
        assert img_elements[0]["img_key"] == "img_v2_abc"
        assert img_elements[1]["img_key"] == "img_v2_def"

    def test_build_project_response_card_no_images(self, sample_project):
        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Claude",
            content="普通回复",
        )

        card = json.loads(content)
        elements = card["body"]["elements"]
        img_elements = [e for e in elements if e.get("tag") == "img"]
        assert len(img_elements) == 0

    def test_build_project_response_card_ttadk_entry_ui(self, sample_project):
        sample_project.ttadk_mode = True
        sample_project.ttadk_tool_name = "codex"
        sample_project.ttadk_model_name = "gpt-5.2"
        sample_project.ttadk_yolo_enabled = True

        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="TTADK编程模式",
            content="已进入TTADK编程模式",
        )

        assert msg_type == "interactive"
        content_str = json.dumps(json.loads(content), ensure_ascii=False)
        assert "TTADK编程模式" in content_str
        assert "已进入TTADK编程模式" in content_str
        assert "TTADK 状态" in content_str
        assert "show_ttadk_menu" in content_str

    def test_build_image_elements(self):
        elements = CardBuilder._build_image_elements(["key1", "key2", "key3"])
        assert len(elements) == 3
        assert elements[0]["tag"] == "img"
        assert elements[0]["img_key"] == "key1"
        assert elements[2]["alt"]["content"] == "图片 3"

    def test_build_image_elements_empty(self):
        elements = CardBuilder._build_image_elements([])
        assert elements == []

    def test_format_time_ago(self):
        import time

        now = time.time()

        assert CardBuilder._format_time_ago(now) == "刚刚"
        assert "分钟前" in CardBuilder._format_time_ago(now - 120)
        assert "小时前" in CardBuilder._format_time_ago(now - 7200)
        assert "天前" in CardBuilder._format_time_ago(now - 172800)


class TestDeepCard:
    @pytest.fixture
    def sample_project(self):
        return ProjectContext(
            project_id="test_project",
            project_name="Test Project",
            root_path="/tmp/test",
            working_dir="/tmp/test/src",
            theme_color="green",
            emoji_prefix="🟢",
        )

    def test_build_info_card_basic(self, sample_project):
        msg_type, content = CardBuilder.build_info_card(
            project=sample_project,
            state=EngineCardState(
                title="Test Title",
                content="Test content",
                engine_name="Coco",
            ),
        )

        assert msg_type == "interactive"
        card = json.loads(content)
        header_title = card["header"]["title"]["content"]
        assert header_title == "🧠 Test Title"

        # Check elements
        elements = card["body"]["elements"]
        assert len(elements) >= 3  # Directory + HR + Content

    def test_build_info_card_with_progress_bar(self, sample_project):
        msg_type, content = CardBuilder.build_info_card(
            project=sample_project,
            state=EngineCardState(
                title="Executing",
                content="Task in progress",
                progress_bar="[█████░░░░░] 50% (2/4)",
                is_executing=True,
                engine_project_id="proj123",
            ),
        )

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)
        assert "50%" in content_str
        assert "deep_pause" in content_str
        assert "deep_stop" in content_str

    def test_build_info_card_paused(self, sample_project):
        msg_type, content = CardBuilder.build_info_card(
            project=sample_project,
            state=EngineCardState(
                title="Paused",
                content="Execution paused",
                is_paused=True,
                engine_project_id="proj123",
            ),
        )

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)
        assert "deep_resume" in content_str
        assert "deep_stop" in content_str

    def test_build_info_card_no_project(self):
        msg_type, content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="No project",
                engine_name="Coco",
            ),
        )

        card = json.loads(content)
        header_title = card["header"]["title"]["content"]
        assert header_title == "🧠 Test"
        # Deep 卡片按引擎区分颜色：Coco=turquoise (running default) / Claude=purple
        assert card["header"]["template"] == "turquoise"

    def test_build_info_card_claude(self):
        msg_type, content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="No project",
                engine_name="Claude",
            ),
        )
        card = json.loads(content)
        assert card["header"]["template"] == "violet"

    def test_build_info_card_with_progress(self):
        msg_type, content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="Doing stuff",
                progress_bar="[===>   ]",
                engine_name="Coco",
            ),
        )
        card = json.loads(content)
        found_bar = False
        for el in card["body"]["elements"]:
            if "content" in el and "[===>   ]" in el["content"]:
                found_bar = True
                break
        assert found_bar

    # ------------------------------------------------------------------
    # _build_deep_buttons tests
    # ------------------------------------------------------------------
    def test_build_deep_buttons_executing(self):
        # Need content > FULL_LINE_THRESHOLD (50) lines to show Expand button (compact=False default)
        long_content = "\n".join([f"Line {i}" for i in range(55)])
        buttons = CardBuilder._build_deep_buttons(
            EngineCardState(engine_project_id="proj123", is_executing=True, content=long_content)
        )
        # Pause, Stop, Expand, Mode -> 4 buttons
        assert len(buttons) == 4
        texts = [b["text"]["content"] for b in buttons]
        assert "⏸️ 暂停" in texts
        assert "⏹️ 停止" in texts

    def test_build_deep_buttons_paused(self):
        # Need content > FULL_LINE_THRESHOLD (50) lines to show Expand button (compact=False default)
        long_content = "\n".join([f"Line {i}" for i in range(55)])
        buttons = CardBuilder._build_deep_buttons(
            EngineCardState(engine_project_id="proj123", is_paused=True, content=long_content)
        )
        # Resume, Stop, Expand, Mode -> 4 buttons
        assert len(buttons) == 4
        texts = [b["text"]["content"] for b in buttons]
        assert "▶️ 继续" in texts
        assert "⏹️ 停止" in texts

    def test_build_deep_buttons_neither(self):
        # Need content > FULL_LINE_THRESHOLD (50) lines to show Expand button (compact=False default)
        long_content = "\n".join([f"Line {i}" for i in range(55)])
        buttons = CardBuilder._build_deep_buttons(EngineCardState(engine_project_id="proj123", content=long_content))
        # Expand, Mode -> 2 buttons
        assert len(buttons) == 2


# ---------------------------------------------------------------------------
# 卡片结构验证 —— 所有卡片使用 v2 格式（schema 2.0，body.elements）
# ---------------------------------------------------------------------------


class TestCardSchema20Structure:
    """验证所有卡片均使用正确的 v2 卡片结构（schema 2.0，body.elements）"""

    @pytest.fixture
    def project(self):
        return ProjectContext(
            project_id="md_test",
            project_name="MD Test",
            root_path="/tmp/md_test",
            working_dir="/tmp/md_test",
            theme_color="blue",
            emoji_prefix="🔵",
        )

    def _parse_card(self, card_tuple: tuple[str, str]) -> dict:
        msg_type, content = card_tuple
        assert msg_type == "interactive"
        return json.loads(content)

    def _assert_v2_structure(self, card: dict, label: str):
        """断言卡片使用 v2 结构（schema 2.0，body.elements）"""
        assert "schema" in card, f"{label}: missing schema field"
        assert card["schema"] == "2.0", f"{label}: schema should be '2.0'"
        assert "body" in card, f"{label}: missing body field"
        assert "elements" in card["body"], f"{label}: missing elements in body"

    def _assert_no_lark_md(self, card: dict, label: str):
        """断言卡片中没有任何 lark_md 标签"""
        card_str = json.dumps(card, ensure_ascii=False)
        assert "lark_md" not in card_str, f"{label}: contains lark_md"

    def _assert_all_text_use_markdown_tag(self, elements: list[dict], label: str):
        """断言所有文本内容元素使用 markdown 标签（而非 div + lark_md）"""
        for i, elem in enumerate(elements):
            tag = elem.get("tag")
            if tag == "div":
                div_str = json.dumps(elem)
                assert "lark_md" not in div_str, f"{label}: element[{i}] is div with lark_md"

    # ---- 各卡片类型的结构验证 ----

    def test_coco_response_card_schema(self, project):
        card = self._parse_card(CardBuilder.build_coco_response_card(project, "Title", "Content"))
        self._assert_v2_structure(card, "coco_response")
        self._assert_no_lark_md(card, "coco_response")
        self._assert_all_text_use_markdown_tag(card["body"]["elements"], "coco_response")

    def test_project_response_card_schema(self, project):
        card = self._parse_card(CardBuilder.build_project_response_card(project, "Title", "Content"))
        self._assert_v2_structure(card, "project_response")
        self._assert_no_lark_md(card, "project_response")

    def test_status_board_card_with_projects_schema(self, project):
        card = self._parse_card(CardBuilder.build_status_board_card([project], project.project_id))
        self._assert_v2_structure(card, "status_board")
        self._assert_no_lark_md(card, "status_board")

    def test_coco_resume_card_schema(self, project):
        project.coco_session_snapshot = CocoSessionSnapshot(
            session_id="s1",
            query_count=3,
            last_query="test",
            is_resumable=True,
        )
        card = self._parse_card(CardBuilder.build_coco_resume_card(project))
        self._assert_v2_structure(card, "coco_resume")
        self._assert_no_lark_md(card, "coco_resume")

    def test_error_card_schema(self):
        card = self._parse_card(CardBuilder.build_error_card("Error msg"))
        self._assert_v2_structure(card, "error")
        self._assert_no_lark_md(card, "error")

    def test_deep_card_schema(self, project):
        card = self._parse_card(
            CardBuilder.build_info_card(project, EngineCardState(title="Title", content="Content", engine_name="Coco"))
        )
        self._assert_v2_structure(card, "deep")
        self._assert_no_lark_md(card, "deep")


# ---------------------------------------------------------------------------
# Markdown 内容元素的渲染验证
# ---------------------------------------------------------------------------


class TestMarkdownContentRendering:
    """
    验证 _build_content_element 始终使用 {"tag": "markdown"} 标签，
    并且各种 Markdown 语法能够正确传递到卡片 JSON 中。

    注意：这里不测试飞书客户端的实际渲染效果（那是飞书 SDK 的责任），
    而是验证 GhostAP 输出的卡片 JSON 结构满足 Markdown 渲染要求：
    1. 内容元素使用 {"tag": "markdown"} 而非 {"tag": "div", "text": {"tag": "lark_md"}}
    2. Markdown 文本被完整传递，不被截断或转义
    """

    @pytest.fixture
    def project(self):
        return ProjectContext(
            project_id="md_project",
            project_name="Markdown Test",
            root_path="/tmp/md",
            theme_color="green",
            emoji_prefix="🟢",
        )

    def _get_content_element(self, card_tuple: tuple[str, str]) -> dict:
        """从卡片中提取主要内容 markdown 元素"""
        card = json.loads(card_tuple[1])
        elements = card["body"]["elements"]
        # 找到内容 markdown 元素（排除路径元素和进度条）
        md_elements = [
            e
            for e in elements
            if e.get("tag") == "markdown"
            and not str(e.get("content", "")).startswith("📁")
            and not str(e.get("content", "")).startswith("📊")
            and not str(e.get("content", "")).startswith("💡")
        ]
        assert len(md_elements) >= 1, "No content markdown element found"
        return md_elements[0]

    # ---- 1. 常见 Markdown 语法 ----

    def test_heading_syntax(self, project):
        content = "# 一级标题\n## 二级标题\n### 三级标题"
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "Test", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "# 一级标题" in elem["content"]
        assert "## 二级标题" in elem["content"]
        assert "### 三级标题" in elem["content"]

    def test_ordered_list(self, project):
        content = "步骤：\n1. 安装依赖\n2. 运行测试\n3. 部署"
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "Test", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "1. 安装依赖" in elem["content"]
        assert "3. 部署" in elem["content"]

    def test_inline_code(self, project):
        content = "使用 `pip install flask` 安装"
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "Test", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "`pip install flask`" in elem["content"]

    def test_link(self, project):
        content = "参考 [飞书文档](https://open.feishu.cn/) 获取更多信息"
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "Test", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "[飞书文档](https://open.feishu.cn/)" in elem["content"]

    def test_horizontal_rule_in_content(self, project):
        content = "上半部分\n\n---\n\n下半部分"
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "Test", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "---" in elem["content"]

    def test_mixed_markdown_ai_response(self, project):
        """模拟 AI 回复的典型复杂 Markdown"""
        content = (
            "# 分析结果\n\n"
            "## 问题描述\n"
            "代码中存在以下问题：\n\n"
            "1. **内存泄漏**: `session` 对象未正确关闭\n"
            "2. **SQL注入**: 使用了字符串拼接\n"
            "3. **竞态条件**: 缺少锁保护\n\n"
            "## 修复方案\n\n"
            "```python\n"
            "class SessionManager:\n"
            "    def __init__(self):\n"
            "        self._lock = threading.Lock()\n"
            "    \n"
            "    def close_session(self, session_id: str):\n"
            "        with self._lock:\n"
            "            if session_id in self._sessions:\n"
            "                self._sessions[session_id].close()\n"
            "                del self._sessions[session_id]\n"
            "```\n\n"
            "> 注意：修复后需要运行完整测试套件验证\n\n"
            "相关文档：[Python线程安全](https://docs.python.org/3/library/threading.html)"
        )
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "代码审查", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        # 验证所有 Markdown 语法都完整传递
        assert "# 分析结果" in elem["content"]
        assert "**内存泄漏**" in elem["content"]
        assert "```python" in elem["content"]
        assert "class SessionManager:" in elem["content"]
        assert "> 注意" in elem["content"]
        assert "[Python线程安全]" in elem["content"]

    def test_nested_list_with_code(self, project):
        content = (
            "安装步骤：\n"
            "1. 克隆仓库\n"
            "   ```bash\n"
            "   git clone https://github.com/example/repo.git\n"
            "   ```\n"
            "2. 安装依赖\n"
            "   - Python: `pip install -r requirements.txt`\n"
            "   - Node: `npm install`\n"
            "3. 启动服务\n"
            "   ```bash\n"
            "   python -m src.main\n"
            "   ```"
        )
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "安装指南", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "git clone" in elem["content"]
        assert "`pip install -r requirements.txt`" in elem["content"]

    def test_multiple_code_blocks_different_languages(self, project):
        content = (
            "## Python\n"
            "```python\n"
            "print('hello')\n"
            "```\n\n"
            "## JavaScript\n"
            "```javascript\n"
            "console.log('hello');\n"
            "```\n\n"
            "## Shell\n"
            "```bash\n"
            "echo 'hello'\n"
            "```"
        )
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "多语言示例", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "```python" in elem["content"]
        assert "```javascript" in elem["content"]
        assert "```bash" in elem["content"]

    def test_table_syntax(self, project):
        """飞书 Card JSON 2.0 markdown 标签支持表格"""
        content = (
            "| 方法 | 耗时 | 结果 |\n"
            "|------|------|------|\n"
            "| GET  | 12ms | 200  |\n"
            "| POST | 45ms | 201  |\n"
            "| PUT  | 30ms | 200  |"
        )
        elem = self._get_content_element(
            CardBuilder.build_project_response_card(project, "API测试", content, show_buttons=False)
        )
        assert elem["tag"] == "markdown"
        assert "| 方法 |" in elem["content"]
        assert "| GET  |" in elem["content"]

    def test_content_with_title_prepended(self, project):
        """with_title 参数时，标题以 **title** 格式前置"""
        content = "这是正文内容"
        elem = CardBuilder._build_content_element(content, with_title="测试标题")
        assert elem["tag"] == "markdown"
        assert elem["content"].startswith("**测试标题**")
        assert "这是正文内容" in elem["content"]

    def test_content_without_title(self, project):
        """无标题时内容直接传递"""
        content = "纯内容"
        elem = CardBuilder._build_content_element(content, with_title=None)
        assert elem["tag"] == "markdown"
        assert elem["content"] == "纯内容"

    def test_deep_card_markdown_rendering(self, project):
        """Deep 卡片也使用 markdown 标签"""
        content = "## 任务进度\n- [x] 步骤一\n- [ ] 步骤二\n\n```python\nresult = run()\n```"
        card = json.loads(
            CardBuilder.build_info_card(
                project,
                state=EngineCardState(title="Deep任务", content=content, engine_name="Claude", show_buttons=False),
            )[1]
        )
        elements = card["body"]["elements"]
        md_content = [e for e in elements if e.get("tag") == "markdown" and "任务进度" in str(e.get("content", ""))]
        assert len(md_content) >= 1
        assert "```python" in md_content[0]["content"]

    def test_notification_card_suggestions_use_markdown(self, project):
        """通知卡片的建议部分也使用 markdown 标签"""
        card = json.loads(
            CardBuilder.build_notification_card(
                project,
                "success",
                "Done",
                "全部完成",
                suggestions=["运行 `pytest`", "执行 **部署**"],
            )[1]
        )
        elements = card["body"]["elements"]
        suggestion_md = [
            e for e in elements if e.get("tag") == "markdown" and "建议下一步" in str(e.get("content", ""))
        ]
        assert len(suggestion_md) == 1
        assert "运行 `pytest`" in suggestion_md[0]["content"]
        assert "执行 **部署**" in suggestion_md[0]["content"]

    def test_status_board_project_info_uses_markdown(self, project):
        """状态看板中项目信息使用 markdown 标签"""
        card = json.loads(CardBuilder.build_status_board_card([project], project.project_id)[1])
        elements = card["body"]["elements"]
        # 项目信息元素应为 markdown 标签（项目名为 "Markdown Test"）
        project_md = [
            e for e in elements if e.get("tag") == "markdown" and "Markdown Test" in str(e.get("content", ""))
        ]
        assert len(project_md) >= 1

    def test_error_card_markdown_rendering(self):
        """错误卡片使用 markdown 标签渲染错误信息"""
        card = json.loads(CardBuilder.build_error_card("找不到文件 `config.yaml`")[1])
        elements = card["body"]["elements"]
        error_md = [e for e in elements if e.get("tag") == "markdown" and "config.yaml" in str(e.get("content", ""))]
        assert len(error_md) == 1
        assert "`config.yaml`" in error_md[0]["content"]

    # ---- 3. 各卡片类型通过完整 pipeline 的 Markdown 传递 ----

    def test_coco_card_passes_markdown_through(self, project):
        """Coco 卡片完整传递 Markdown"""
        content = "**步骤一**: `git pull`\n\n```bash\ngit pull origin main\n```"
        card = json.loads(CardBuilder.build_coco_response_card(project, "Git操作", content, show_buttons=False)[1])
        card_text = json.dumps(card, ensure_ascii=False)
        assert "**步骤一**" in card_text
        assert "```bash" in card_text

    def test_smart_card_passes_markdown_through(self, project):
        """Smart 卡片完整传递 Markdown"""
        content = "- 选项A: *推荐*\n- 选项B: ~~不推荐~~"
        card = json.loads(CardBuilder.build_smart_response_card(project, "建议", content, show_buttons=False)[1])
        card_text = json.dumps(card, ensure_ascii=False)
        assert "*推荐*" in card_text
        assert "~~不推荐~~" in card_text


# ---------------------------------------------------------------------------
# Markdown 渲染边界情况
# ---------------------------------------------------------------------------


class TestMarkdownEdgeCases:
    """Markdown 渲染的边界情况测试"""

    @pytest.fixture
    def project(self):
        return ProjectContext(
            project_id="edge_test",
            project_name="Edge Test",
            root_path="/tmp/edge",
            theme_color="orange",
            emoji_prefix="🟠",
        )

    # ---- 空内容和空白 ----

    def test_empty_content(self, project):
        """空字符串内容"""
        elem = CardBuilder._build_content_element("")
        assert elem["tag"] == "markdown"
        assert elem["content"] == ""

    def test_empty_content_with_title(self, project):
        """有标题但内容为空"""
        elem = CardBuilder._build_content_element("", with_title="标题")
        assert elem["tag"] == "markdown"
        assert "**标题**" in elem["content"]

    # ---- 特殊字符 ----

    def test_html_tags_in_content(self, project):
        """HTML 标签在内容中（不应被解析为 HTML）"""
        content = "<script>alert('xss')</script>\n<b>bold</b>\n<img src='x'>"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        # 内容应原样传递，由飞书客户端决定如何处理
        assert "<script>" in elem["content"]
        assert "<b>bold</b>" in elem["content"]

    def test_unicode_emoji_content(self, project):
        """Unicode 和 emoji"""
        content = "✅ 成功 | ❌ 失败 | ⚠️ 警告\n🇨🇳 中文 | 🇺🇸 English | 🇯🇵 日本語"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        assert "✅ 成功" in elem["content"]
        assert "🇨🇳 中文" in elem["content"]

    def test_markdown_special_chars_unescaped(self, project):
        """Markdown 特殊字符不做额外转义（由飞书渲染器处理）"""
        content = "价格 $100 | 100% 完成 | a_b_c | [text] | {code}"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        assert elem["content"] == content

    # ---- 超长内容 ----

    def test_very_long_content(self, project):
        """超长内容被截断（防止 API 报错）"""
        long_line = "A" * 5000
        content = f"开始\n{long_line}\n结束"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        # 新逻辑会截断到约 4000 字符
        assert len(elem["content"]) < len(content)
        assert "日志内容过长，已被截断" in elem["content"]
        assert elem["content"].endswith("结束")

    def test_markdown_truncation_smart_closing(self):
        """测试 Markdown 智能截断闭合逻辑"""
        # 1. 正常截断，无未闭合标记
        content = "a" * 5000
        truncated = CardBuilder._truncate_markdown(content, 1000)
        assert len(truncated) <= 1000
        assert "日志内容过长" in truncated

        # 2. 截断点在代码块内部 -> 应补全 ```
        # 构造：前缀 + 代码块开始 + 长内容 + 代码块结束
        # 截断后应该保留 尾部，如果尾部在代码块内，应在尾部前加 ```
        # 这里的逻辑是保留 TAIL。
        # 原文: [PRE] ``` [LONG CODE] ```
        # 截断: 保留 [...CODE] ```
        # 因为 Cut Point 在 ``` 之后，意味着 Head 里有一个 ```。
        # 所以 Tail 开始时处于 inside 状态。
        # _truncate_markdown 应该在 Tail 前加 ```。

        prefix = "a" * 3000
        code_content = "code" * 1000  # 4000 chars
        full = f"{prefix}```\n{code_content}\n```"
        # total > 7000
        # max 1000. Keep last ~900.
        # Cut point is around 6100.
        # Head has `prefix` (3000) + ``` (3). So Head has 1 marker. Inside code block.
        # Tail has part of code_content + ```.
        # Tail should start with ``` to be valid.

        truncated = CardBuilder._truncate_markdown(full, 1000)
        # 验证包含补全的 ```
        # 警告语之后应该是 ```
        assert "日志内容过长" in truncated
        assert "```" in truncated
        # 确保总共是偶数个 ``` (警告语里没有，Tail前补1个，Tail后原有1个 -> 2个)
        assert truncated.count("```") % 2 == 0

        # 3. 截断点在加粗内部 -> 应补全 **
        prefix = "a" * 3000
        bold_content = "bold" * 1000
        full = f"{prefix}**{bold_content}**"

        truncated = CardBuilder._truncate_markdown(full, 1000)
        assert "**" in truncated
        assert truncated.count("**") % 2 == 0

    def test_large_code_block(self, project):
        """大型代码块"""
        code_lines = "\n".join(f"    line_{i} = func({i})" for i in range(100))
        content = f"```python\n{code_lines}\n```"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        assert "line_0 = func(0)" in elem["content"]
        assert "line_99 = func(99)" in elem["content"]

    # ---- 嵌套和边界 Markdown ----

    def test_unclosed_code_block(self, project):
        """未闭合的代码块"""
        content = "```python\ndef foo():\n    pass"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        assert "```python" in elem["content"]
        assert "def foo():" in elem["content"]

    def test_multiple_backticks(self, project):
        """嵌套 backticks"""
        content = "使用 `` ` `` 表示单个反引号，或 ``` `` ``` 表示两个"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        assert elem["content"] == content

    def test_only_markdown_symbols(self, project):
        """仅包含 Markdown 符号字符"""
        content = "# ## ### * ** *** - --- > >> ``` ~~"
        elem = CardBuilder._build_content_element(content)
        assert elem["tag"] == "markdown"
        assert elem["content"] == content

    # ---- 目录元素 ----

    def test_directory_element_uses_markdown(self, project):
        """目录路径元素使用 markdown 标签"""
        elem = CardBuilder._build_directory_element(project)
        assert elem["tag"] == "markdown"
        assert f"`{project.root_path}`" in elem["content"]
        assert "📁" in elem["content"]

    def test_directory_element_no_project(self):
        """无项目时路径默认为 ~"""
        elem = CardBuilder._build_directory_element(None)
        assert elem["tag"] == "markdown"
        assert "`~`" in elem["content"]

    def test_directory_element_with_working_dir(self):
        """使用 working_dir 参数"""
        elem = CardBuilder._build_directory_element(None, working_dir="/home/user/work")
        assert elem["tag"] == "markdown"
        assert "`/home/user/work`" in elem["content"]

    def test_directory_path_with_spaces(self, project):
        """路径包含空格"""
        project.root_path = "/home/user/my project/src"
        elem = CardBuilder._build_directory_element(project)
        assert elem["tag"] == "markdown"
        assert "`/home/user/my project/src`" in elem["content"]

    # ---- 完整卡片中的边界情况 ----

    def test_full_card_empty_content(self, project):
        """完整卡片使用空内容"""
        _, content = CardBuilder.build_project_response_card(project, "Title", "", show_buttons=False)
        card = json.loads(content)
        assert "body" in card
        elements = card["body"]["elements"]
        md_elems = [e for e in elements if e.get("tag") == "markdown"]
        assert len(md_elems) >= 1

    def test_full_card_with_newlines_only(self, project):
        """完整卡片使用纯换行内容"""
        _, content = CardBuilder.build_project_response_card(project, "Title", "\n\n\n", show_buttons=False)
        card = json.loads(content)
        assert "body" in card

    def test_full_card_json_serialization_integrity(self, project):
        """确保含特殊字符的内容经 JSON 序列化后不损坏"""
        special_content = '包含 "引号" 和 \\反斜杠 以及\n换行\t制表符'
        _, card_str = CardBuilder.build_project_response_card(project, "Title", special_content, show_buttons=False)
        # 反序列化应成功
        card = json.loads(card_str)
        # 直接检查解析后的 content 字段，避免二次 JSON 序列化的转义干扰
        md_elems = [
            e for e in card["body"]["elements"] if e.get("tag") == "markdown" and "引号" in str(e.get("content", ""))
        ]
        assert len(md_elems) == 1
        content = md_elems[0]["content"]
        assert '包含 "引号"' in content
        assert "\\反斜杠" in content
        assert "\n" in content
        assert "\t" in content

    def test_notification_card_no_suggestions(self, project):
        """通知卡片无建议时不应有建议元素"""
        _, content = CardBuilder.build_notification_card(project, "info", "Info", "纯信息通知")
        card = json.loads(content)
        elements = card["body"]["elements"]
        suggestion_elems = [
            e for e in elements if e.get("tag") == "markdown" and "建议下一步" in str(e.get("content", ""))
        ]
        assert len(suggestion_elems) == 0

    def test_status_board_empty_uses_markdown_for_prompt(self):
        """空项目看板的提示使用 markdown 标签"""
        _, content = CardBuilder.build_status_board_card([], None)
        card = json.loads(content)
        elements = card["body"]["elements"]
        prompt_md = [e for e in elements if e.get("tag") == "markdown" and "暂无项目" in str(e.get("content", ""))]
        assert len(prompt_md) == 1
        assert "/new" in prompt_md[0]["content"]

    def test_deep_card_progress_bar_uses_markdown(self):
        """Deep 卡片的进度条使用 markdown 标签"""
        _, content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="Running",
                progress_bar="[████░░░░░░] 40%",
                show_buttons=False,
                engine_name="Coco",
            ),
        )
        card = json.loads(content)
        elements = card["body"]["elements"]
        progress_md = [e for e in elements if e.get("tag") == "markdown" and "📊" in str(e.get("content", ""))]
        assert len(progress_md) == 1
        assert "40%" in progress_md[0]["content"]


# ---------------------------------------------------------------------------
# 回复模式配置测试
# ---------------------------------------------------------------------------


class TestReplyModeConfig:
    """测试回复模式配置项（smart_reply_mode / default_reply_mode）"""

    def test_smart_reply_mode_default_is_direct(self):
        """智能模式默认回复模式为 direct"""
        settings = Settings()
        assert settings.smart_reply_mode == "direct"

    def test_default_reply_mode_default_is_thread(self):
        """其他模式默认回复模式为 thread"""
        settings = Settings()
        assert settings.default_reply_mode == "thread"

    def test_smart_reply_mode_can_be_set_to_thread(self):
        """智能模式回复模式可以设置为 thread"""
        settings = Settings(smart_reply_mode="thread")
        assert settings.smart_reply_mode == "thread"

    def test_default_reply_mode_can_be_set_to_direct(self):
        """其他模式回复模式可以设置为 direct"""
        settings = Settings(default_reply_mode="direct")
        assert settings.default_reply_mode == "direct"


class TestBuildDeepCardStructuredParams:
    """Tests for build_info_card new structured parameters."""

    def test_duration_line_renders(self):
        """duration_line should be combined with status_line."""
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="Body",
                engine_name="Coco",
                show_buttons=False,
                status_line="🔄 执行中",
                duration_line="⏱️ 3分钟12秒",
            ),
        )
        card = json.loads(card_content)
        elements = card["body"]["elements"]
        notation_elems = [e for e in elements if e.get("text_size") == "notation"]
        assert len(notation_elems) >= 1
        assert "执行中" in notation_elems[0]["content"]
        assert "3分钟12秒" in notation_elems[0]["content"]

    def test_footer_note_renders(self):
        """footer_note should appear as notation-sized element."""
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="Body",
                engine_name="Coco",
                show_buttons=False,
                footer_note="Generated by Spec Engine",
            ),
        )
        card = json.loads(card_content)
        elements = card["body"]["elements"]
        footer_elems = [e for e in elements if "Generated by Spec Engine" in str(e.get("content", ""))]
        assert len(footer_elems) == 1
        assert footer_elems[0].get("text_size") == "notation"

    def test_no_optional_params_still_works(self):
        """build_info_card with no optional params should still work."""
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test",
                content="Body",
                engine_name="Coco",
                show_buttons=False,
            ),
        )
        card = json.loads(card_content)
        assert card["body"]["elements"]

    def test_all_optional_params_together(self):
        """All new optional params at once."""
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Full Card",
                content="Main content",
                progress_bar="[████░░] 60% (3/5)",
                engine_name="Spec(Claude)",
                show_buttons=False,
                status_line="🔄 循环执行中",
                duration_line="⏱️ 5分",
                criteria_section="📋 标准\n✅ C1\n🔲 C2",
                footer_note="Powered by Spec",
            ),
        )
        card = json.loads(card_content)
        elements = card["body"]["elements"]
        content_strs = [str(e.get("content", "")) for e in elements]
        all_content = " ".join(content_strs)
        assert "循环执行中" in all_content
        assert "5分" in all_content
        assert "标准" in all_content
        assert "Powered by Spec" in all_content
        assert "Main content" in all_content


class TestTerminalMarkers:
    """Task 9: terminal_state marker lines appear at card bottom."""

    def test_completed_marker(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Done", content="ok", engine_name="Coco",
                show_buttons=False, terminal_state="completed",
            ),
        )
        card = json.loads(card_content)
        last_md = [e for e in card["body"]["elements"] if e.get("tag") == "markdown"][-1]
        assert "✅" in last_md["content"]
        assert "已完成" in last_md["content"]

    def test_failed_marker(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Err", content="err", engine_name="Coco",
                show_buttons=False, terminal_state="failed",
            ),
        )
        card = json.loads(card_content)
        last_md = [e for e in card["body"]["elements"] if e.get("tag") == "markdown"][-1]
        assert "❌" in last_md["content"]

    def test_no_marker_when_none(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Running", content="running", engine_name="Coco",
                show_buttons=False, terminal_state=None,
            ),
        )
        card = json.loads(card_content)
        from src.card.terminal import TERMINAL_MARKERS
        all_text = " ".join(str(e.get("content", "")) for e in card["body"]["elements"])
        for marker in TERMINAL_MARKERS.values():
            assert marker not in all_text


class TestFooterStatus:
    """Task 10: footer_status line appears before buttons."""

    def test_thinking_footer(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test", content="body", engine_name="Coco",
                is_executing=True, footer_status="thinking",
            ),
        )
        card = json.loads(card_content)
        notation_elems = [e for e in card["body"]["elements"]
                          if e.get("text_size") == "notation" and "🧠" in str(e.get("content", ""))]
        assert len(notation_elems) >= 1
        assert "正在思考" in notation_elems[0]["content"]

    def test_tool_running_footer(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test", content="body", engine_name="Coco",
                is_executing=True, footer_status="tool_running",
            ),
        )
        card = json.loads(card_content)
        all_text = " ".join(str(e.get("content", "")) for e in card["body"]["elements"])
        assert "🧰" in all_text
        assert "正在调用工具" in all_text

    def test_no_footer_when_none(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Test", content="body", engine_name="Coco",
                show_buttons=False, footer_status=None,
            ),
        )
        card = json.loads(card_content)
        from src.card.terminal import FOOTER_STATUS
        all_text = " ".join(str(e.get("content", "")) for e in card["body"]["elements"])
        for fs in FOOTER_STATUS.values():
            assert fs not in all_text


class TestStopDangerButton:
    """Task 11: stop_danger button appears during execution."""

    def test_stop_danger_appears_when_executing(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Running", content="work",
                engine_name="Coco", is_executing=True,
            ),
        )
        card = json.loads(card_content)
        all_text = json.dumps(card, ensure_ascii=False)
        assert "⛔ 强制停止" in all_text

    def test_stop_danger_appears_when_paused(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Paused", content="paused",
                engine_name="Coco", is_paused=True,
            ),
        )
        card = json.loads(card_content)
        all_text = json.dumps(card, ensure_ascii=False)
        assert "⛔ 强制停止" in all_text


class TestReadUnreadMarker:
    """Task 12: is_read=False adds 🔴 prefix to title."""

    def test_unread_marker_prefix(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="New Task", content="body",
                engine_name="Coco", show_buttons=False, is_read=False,
            ),
        )
        card = json.loads(card_content)
        header_title = card["header"]["title"]["content"]
        assert header_title.startswith("🔴 ")

    def test_read_no_marker(self):
        _, card_content = CardBuilder.build_info_card(
            project=None,
            state=EngineCardState(
                title="Task", content="body",
                engine_name="Coco", show_buttons=False, is_read=True,
            ),
        )
        card = json.loads(card_content)
        header_title = card["header"]["title"]["content"]
        assert not header_title.startswith("🔴")

    def test_default_is_read(self):
        state = EngineCardState()
        assert state.is_read is True

class TestTTADKCards:
    """测试 TTADK 工具和模型选择卡片"""

    def test_build_ttadk_tool_select_card_basic(self):
        """测试构建 TTADK 工具选择卡片的基本功能"""
        tools = [
            TTADKTool(name="claude", description="Claude AI Assistant", is_default=True),
            TTADKTool(name="coco", description="Coco AI Assistant"),
            TTADKTool(name="gemini", description="Google Gemini AI"),
        ]

        msg_type, content = CardBuilder.build_ttadk_tool_select_card(tools, project_id="test_project")

        assert msg_type == "interactive"
        card = json.loads(content)

        # 验证卡片结构
        assert card["schema"] == "2.0"
        assert "TTADK 工具选择" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "blue"

        # 验证元素
        elements = card["body"]["elements"]
        assert len(elements) > 0

        # 验证工具按钮存在
        content_str = json.dumps(card, ensure_ascii=False)
        assert "claude" in content_str
        assert "Claude AI Assistant" in content_str
        assert "coco" in content_str
        assert "gemini" in content_str
        assert "toggle_ttadk_yolo" in content_str

    def test_build_ttadk_soft_failure_card(self):
        msg_type, content = CardBuilder.build_ttadk_soft_failure_card_for("TTADK 暂不可用", project_id="p1")
        assert msg_type == "interactive"
        content_str = json.dumps(json.loads(content), ensure_ascii=False)
        assert "TTADK 暂不可用" in content_str
        assert "继续进入TTADK" in content_str
        assert "已为你保留选择" in content_str
        assert "show_ttadk_menu" in content_str

    def test_build_ttadk_model_select_card_basic(self):
        """测试构建 TTADK 模型选择卡片的基本功能"""
        models = [
            TTADKModel(name="claude-3-opus", description="Claude 3 Opus", is_default=True),
            TTADKModel(name="claude-3.5-sonnet", description="Claude 3.5 Sonnet"),
            TTADKModel(name="gpt-5.2", description="GPT-5.2"),
        ]

        msg_type, content = CardBuilder.build_ttadk_model_select_card(
            models, tool_name="claude", project_id="test_project"
        )

        assert msg_type == "interactive"
        card = json.loads(content)

        # 验证卡片结构
        assert card["schema"] == "2.0"
        assert "claude" in card["header"]["title"]["content"]
        assert "模型选择" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "blue"

        # 验证模型按钮存在
        content_str = json.dumps(card, ensure_ascii=False)
        assert "claude-3-opus" in content_str
        assert "Claude 3 Opus" in content_str
        assert "claude-3.5-sonnet" in content_str
        assert "gpt-5.2" in content_str
        assert "select_ttadk_model" in content_str

    def test_ttadk_cards_schema_v2(self):
        """验证 TTADK 卡片使用 schema 2.0 结构"""
        tools = [TTADKTool(name="claude", description="Test")]
        models = [TTADKModel(name="test-model", description="Test Model")]

        for card_tuple in [
            CardBuilder.build_ttadk_tool_select_card(tools),
            CardBuilder.build_ttadk_model_select_card(models, "claude"),
        ]:
            msg_type, content = card_tuple
            assert msg_type == "interactive"
            card = json.loads(content)
            assert card["schema"] == "2.0"
            assert "body" in card
            assert "elements" in card["body"]
