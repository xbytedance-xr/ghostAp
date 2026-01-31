import json
import pytest

from src.project.context import ProjectContext, ProjectStatus, CocoSessionSnapshot
from src.card.builder import CardBuilder
from src.card.themes import get_theme, THEMES


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
        
        elements = card["elements"]
        assert any("Test Title" in str(e) for e in elements)
        assert any("Test content here" in str(e) for e in elements)
        # 响应式布局：默认 <=2 个按钮用 action（更贴近桌面端），更多按钮用 column_set
        action_elements = [e for e in elements if e.get("tag") == "action"]
        column_set_elements = [e for e in elements if e.get("tag") == "column_set"]
        assert len(action_elements) + len(column_set_elements) >= 1

        if action_elements:
            actions = action_elements[-1].get("actions", [])
            assert len(actions) >= 1
            for btn in actions:
                assert btn.get("tag") == "button"
                assert "behaviors" in btn
                assert btn["behaviors"][0]["type"] == "callback"
                assert isinstance(btn["behaviors"][0]["value"], dict)
                assert btn.get("size") == "small"
        else:
            assert len(column_set_elements) >= 1
            columns = column_set_elements[0]["columns"]
            assert len(columns) == 2
            for col in columns:
                if col["elements"]:
                    btn = col["elements"][0]
                    assert "behaviors" in btn
                    assert btn["behaviors"][0]["type"] == "callback"
                    assert isinstance(btn["behaviors"][0]["value"], dict)
                    assert btn.get("size") == "small"

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

    def test_build_project_response_card_no_buttons(self, sample_project):
        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Title",
            content="Content",
            show_buttons=False,
        )
        
        card = json.loads(content)
        action_elements = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(action_elements) == 0

    def test_build_status_board_card_empty(self):
        msg_type, content = CardBuilder.build_status_board_card([], None)
        
        assert msg_type == "interactive"
        
        card = json.loads(content)
        assert "项目看板" in card["header"]["title"]["content"]
        assert any("暂无项目" in str(e) for e in card["elements"])

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
        assert any("建议下一步" in str(e) for e in card["elements"])

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
        assert any("Something went wrong" in str(e) for e in card["elements"])

    def test_build_error_card_with_project(self, sample_project):
        msg_type, content = CardBuilder.build_error_card(
            "Error message",
            project=sample_project,
        )
        
        card = json.loads(content)
        content_str = json.dumps(card)
        
        assert "/tmp/test" in content_str
        assert "Error message" in content_str

    def test_build_project_response_card_with_images(self, sample_project):
        msg_type, content = CardBuilder.build_project_response_card(
            project=sample_project,
            title="Claude",
            content="分析结果",
            image_keys=["img_v2_abc", "img_v2_def"],
        )

        card = json.loads(content)
        elements = card["elements"]
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
        elements = card["elements"]
        img_elements = [e for e in elements if e.get("tag") == "img"]
        assert len(img_elements) == 0

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

    def test_build_deep_card_basic(self, sample_project):
        msg_type, content = CardBuilder.build_deep_card(
            project=sample_project,
            title="Test Title",
            content="Test content",
            engine_name="Coco",
        )

        assert msg_type == "interactive"
        card = json.loads(content)
        header_title = card["header"]["title"]["content"]
        assert "🧠" in header_title
        assert "Test Project" in header_title
        assert "Deep" in header_title
        assert "Coco" in header_title

    def test_build_deep_card_with_progress_bar(self, sample_project):
        msg_type, content = CardBuilder.build_deep_card(
            project=sample_project,
            title="Executing",
            content="Task in progress",
            progress_bar="[█████░░░░░] 50% (2/4)",
            is_executing=True,
            deep_project_id="proj123",
        )

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)
        assert "50%" in content_str
        assert "deep_pause" in content_str
        assert "deep_stop" in content_str

    def test_build_deep_card_paused(self, sample_project):
        msg_type, content = CardBuilder.build_deep_card(
            project=sample_project,
            title="Paused",
            content="Execution paused",
            is_paused=True,
            deep_project_id="proj123",
        )

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)
        assert "deep_resume" in content_str
        assert "deep_stop" in content_str

    def test_build_deep_card_no_project(self):
        msg_type, content = CardBuilder.build_deep_card(
            project=None,
            title="Test",
            content="No project",
            engine_name="Coco",
        )

        card = json.loads(content)
        header_title = card["header"]["title"]["content"]
        assert "Deep Engine" in header_title
        assert "Coco" in header_title
        assert card["header"]["template"] == "turquoise"

    def test_build_deep_card_claude_engine(self, sample_project):
        msg_type, content = CardBuilder.build_deep_card(
            project=sample_project,
            title="Test",
            content="Content",
            engine_name="Claude",
        )

        card = json.loads(content)
        assert "Claude" in card["header"]["title"]["content"]

    def test_build_deep_card_no_buttons(self, sample_project):
        msg_type, content = CardBuilder.build_deep_card(
            project=sample_project,
            title="Title",
            content="Content",
            show_buttons=False,
        )

        card = json.loads(content)
        column_set_elements = [e for e in card["elements"] if e.get("tag") == "column_set"]
        assert len(column_set_elements) == 0

    def test_build_deep_card_completed_shows_mode_buttons(self, sample_project):
        msg_type, content = CardBuilder.build_deep_card(
            project=sample_project,
            title="Done",
            content="All done",
            is_executing=False,
            is_paused=False,
            show_buttons=True,
        )

        card = json.loads(content)
        content_str = json.dumps(card, ensure_ascii=False)
        assert "Coco" in content_str or "Claude" in content_str

    def test_build_deep_header_title_with_project(self, sample_project):
        title = CardBuilder._build_deep_header_title(sample_project, "Coco")
        assert title == "🧠 Test Project · Deep (Coco)"

    def test_build_deep_header_title_no_project(self):
        title = CardBuilder._build_deep_header_title(None, "Claude")
        assert title == "🧠 Deep Engine (Claude)"

    def test_build_deep_buttons_executing(self):
        buttons = CardBuilder._build_deep_buttons("proj123", is_executing=True)
        assert len(buttons) == 2
        assert buttons[0]["behaviors"][0]["value"]["action"] == "deep_pause"
        assert buttons[1]["behaviors"][0]["value"]["action"] == "deep_stop"

    def test_build_deep_buttons_paused(self):
        buttons = CardBuilder._build_deep_buttons("proj123", is_paused=True)
        assert len(buttons) == 2
        assert buttons[0]["behaviors"][0]["value"]["action"] == "deep_resume"
        assert buttons[1]["behaviors"][0]["value"]["action"] == "deep_stop"

    def test_build_deep_buttons_neither(self):
        buttons = CardBuilder._build_deep_buttons("proj123")
        assert len(buttons) == 0
