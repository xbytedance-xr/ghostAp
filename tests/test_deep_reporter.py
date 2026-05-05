import unittest

from src.deep_engine.models import DeepProject, DeepProjectStatus
from src.deep_engine.reporter import ProgressReporter


def _make_project(**kwargs) -> DeepProject:
    defaults = {
        "project_id": "test",
        "name": "Test Project",
        "root_path": "/tmp/test",
        "status": DeepProjectStatus.IDLE,
    }
    defaults.update(kwargs)
    return DeepProject(**defaults)


class TestFormatPlanningStart(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_contains_requirement(self):
        result = self.reporter.format_planning_start("实现用户登录功能")
        self.assertIn("实现用户登录功能", result)

    def test_contains_header(self):
        result = self.reporter.format_planning_start("some task")
        self.assertIn("Deep Agent 启动", result)
        self.assertIn("正在分析需求", result)
        self.assertIn("正在规划任务", result)


class TestFormatPlanningDone(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_contains_project_info(self):
        project = _make_project(name="MyProject", root_path="/home/dev/myproject")
        result = self.reporter.format_planning_done(project)
        self.assertIn("MyProject", result)
        self.assertIn("/home/dev/myproject", result)

    def test_contains_header(self):
        project = _make_project()
        result = self.reporter.format_planning_done(project)
        self.assertIn("任务规划完成", result)
        self.assertIn("准备开始执行", result)


class TestFormatProjectDone(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_completed_with_duration(self):
        project = _make_project(
            status=DeepProjectStatus.COMPLETED,
            started_at=1000.0,
            completed_at=1125.0,
        )
        result = self.reporter.format_project_done(project)
        self.assertIn("全部任务完成", result)
        self.assertIn("2 分钟 5 秒", result)

    def test_completed_without_duration(self):
        project = _make_project(
            status=DeepProjectStatus.COMPLETED,
            started_at=None,
            completed_at=None,
        )
        result = self.reporter.format_project_done(project)
        self.assertIn("全部任务完成", result)
        self.assertNotIn("总耗时", result)

    def test_failed_status(self):
        project = _make_project(
            status=DeepProjectStatus.FAILED,
            started_at=1000.0,
            completed_at=1060.0,
        )
        result = self.reporter.format_project_done(project)
        self.assertIn("执行完成（有失败）", result)
        self.assertIn("1 分钟 0 秒", result)

    def test_failed_without_duration(self):
        project = _make_project(
            status=DeepProjectStatus.FAILED,
            started_at=None,
        )
        result = self.reporter.format_project_done(project)
        self.assertIn("执行完成（有失败）", result)
        self.assertNotIn("总耗时", result)

    def test_paused_status(self):
        project = _make_project(status=DeepProjectStatus.PAUSED)
        result = self.reporter.format_project_done(project)
        self.assertIn("执行已暂停", result)

    def test_paused_no_duration_line(self):
        project = _make_project(
            status=DeepProjectStatus.PAUSED,
            started_at=1000.0,
            completed_at=1060.0,
        )
        result = self.reporter.format_project_done(project)
        self.assertNotIn("总耗时", result)


class TestFormatError(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_none_error(self):
        result = self.reporter.format_error(None)
        self.assertIn("未知错误", result)

    def test_empty_error(self):
        result = self.reporter.format_error("")
        self.assertIn("未知错误", result)

    def test_whitespace_error(self):
        result = self.reporter.format_error("   ")
        self.assertIn("未知错误", result)

    def test_timeout_error_class(self):
        result = self.reporter.format_error("TimeoutError: connection timed out")
        self.assertIn("操作超时", result)
        self.assertIn("稍后重试", result)

    def test_timeout_lowercase(self):
        result = self.reporter.format_error("request timeout after 30s")
        self.assertIn("操作超时", result)
        self.assertIn("稍后重试", result)

    def test_timeout_chinese(self):
        result = self.reporter.format_error("操作耗时过长，请稍后重试")
        self.assertIn("操作超时", result)

    def test_internal_error(self):
        result = self.reporter.format_error("internal error occurred")
        self.assertIn("Internal error", result)
        self.assertIn("请检查错误信息后重试", result)

    def test_timeout_and_internal(self):
        result = self.reporter.format_error("TimeoutError: internal error in service")
        self.assertIn("操作超时", result)
        self.assertIn("Internal error", result)
        self.assertIn("稍后重试", result)

    def test_normal_error(self):
        result = self.reporter.format_error("file not found")
        self.assertIn("请检查错误信息后重试", result)
        self.assertNotIn("操作超时", result)
        self.assertNotIn("Internal error", result)

    def test_error_text_in_code_block(self):
        result = self.reporter.format_error("something broke")
        self.assertIn("something broke", result)
        self.assertIn("```", result)

    def test_header_present(self):
        result = self.reporter.format_error("any error")
        self.assertIn("Deep Agent 错误", result)


class TestFormatStatus(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_idle(self):
        project = _make_project(status=DeepProjectStatus.IDLE)
        result = self.reporter.format_status(project)
        self.assertIn("等待开始", result)

    def test_planning(self):
        project = _make_project(status=DeepProjectStatus.PLANNING)
        result = self.reporter.format_status(project)
        self.assertIn("正在规划", result)

    def test_executing(self):
        project = _make_project(
            status=DeepProjectStatus.EXECUTING,
            started_at=1000.0,
            completed_at=1065.0,
        )
        result = self.reporter.format_status(project)
        self.assertIn("执行中", result)
        self.assertIn("1 分钟 5 秒", result)

    def test_paused(self):
        project = _make_project(status=DeepProjectStatus.PAUSED)
        result = self.reporter.format_status(project)
        self.assertIn("已暂停", result)

    def test_completed(self):
        project = _make_project(status=DeepProjectStatus.COMPLETED)
        result = self.reporter.format_status(project)
        self.assertIn("已完成", result)

    def test_failed(self):
        project = _make_project(status=DeepProjectStatus.FAILED)
        result = self.reporter.format_status(project)
        self.assertIn("执行失败", result)

    def test_contains_project_name(self):
        project = _make_project(name="Demo", status=DeepProjectStatus.IDLE)
        result = self.reporter.format_status(project)
        self.assertIn("Demo", result)

    def test_no_duration_when_not_started(self):
        project = _make_project(status=DeepProjectStatus.IDLE, started_at=None)
        result = self.reporter.format_status(project)
        self.assertNotIn("已执行", result)


class TestGetProjectDoneTitle(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_completed(self):
        project = _make_project(status=DeepProjectStatus.COMPLETED)
        self.assertEqual(self.reporter.get_project_done_title(project), "🎉 全部任务完成！")

    def test_failed(self):
        project = _make_project(status=DeepProjectStatus.FAILED)
        self.assertEqual(self.reporter.get_project_done_title(project), "⚠️ 执行完成（有失败）")

    def test_other_status(self):
        project = _make_project(status=DeepProjectStatus.PAUSED)
        self.assertEqual(self.reporter.get_project_done_title(project), "⏸️ 执行已暂停")

    def test_idle_status(self):
        project = _make_project(status=DeepProjectStatus.IDLE)
        self.assertEqual(self.reporter.get_project_done_title(project), "⏸️ 执行已暂停")


class TestGetProgressInfo(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_basic_keys(self):
        project = _make_project(
            project_id="p1",
            name="MyProj",
            status=DeepProjectStatus.EXECUTING,
        )
        result = self.reporter.get_progress_info(project, completed=3, total=10)
        self.assertEqual(result["completed_count"], 3)
        self.assertEqual(result["total_count"], 10)
        self.assertEqual(result["status"], DeepProjectStatus.EXECUTING)
        self.assertEqual(result["project_name"], "MyProj")
        self.assertEqual(result["project_id"], "p1")
        self.assertTrue(result["is_executing"])
        self.assertFalse(result["is_paused"])

    def test_progress_bar_present(self):
        project = _make_project(status=DeepProjectStatus.EXECUTING)
        result = self.reporter.get_progress_info(project, completed=5, total=10)
        self.assertIn("50%", result["progress_bar"])
        self.assertIn("5/10", result["progress_bar"])

    def test_zero_total(self):
        project = _make_project(status=DeepProjectStatus.IDLE)
        result = self.reporter.get_progress_info(project, completed=0, total=0)
        self.assertIn("0%", result["progress_bar"])
        self.assertFalse(result["is_executing"])
        self.assertFalse(result["is_paused"])

    def test_paused_flags(self):
        project = _make_project(status=DeepProjectStatus.PAUSED)
        result = self.reporter.get_progress_info(project, completed=2, total=5)
        self.assertFalse(result["is_executing"])
        self.assertTrue(result["is_paused"])

    def test_defaults(self):
        project = _make_project(status=DeepProjectStatus.COMPLETED)
        result = self.reporter.get_progress_info(project)
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(result["total_count"], 0)

    def test_all_keys_present(self):
        project = _make_project(status=DeepProjectStatus.IDLE)
        result = self.reporter.get_progress_info(project)
        expected_keys = {
            "progress_bar",
            "completed_count",
            "total_count",
            "status",
            "project_name",
            "project_id",
            "is_executing",
            "is_paused",
        }
        self.assertEqual(set(result.keys()), expected_keys)


class TestTitleHelpers(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_planning_start_title(self):
        self.assertEqual(self.reporter.get_planning_start_title(), "🧠 Deep Agent 启动")

    def test_planning_done_title(self):
        self.assertEqual(self.reporter.get_planning_done_title(), "✅ 任务规划完成")

    def test_error_title(self):
        self.assertEqual(self.reporter.get_error_title(), "❌ Deep Agent 错误")

    def test_status_title(self):
        self.assertEqual(self.reporter.get_status_title(), "📊 任务状态")

    def test_context_injected_title(self):
        self.assertEqual(self.reporter.get_context_injected_title(), "💬 上下文已注入")


class TestFormatContextInjected(unittest.TestCase):
    def setUp(self):
        self.reporter = ProgressReporter()

    def test_contains_message(self):
        result = self.reporter.format_context_injected("请先安装依赖")
        self.assertIn("请先安装依赖", result)
        self.assertIn("上下文已注入", result)
        self.assertIn("下一个任务执行前生效", result)


if __name__ == "__main__":
    unittest.main()
