import pytest
import threading
import time
from unittest.mock import patch, MagicMock, Mock

from src.deep_engine.models import (
    ContextEntry,
    DeepTask,
    DeepTaskStatus,
    DeepProject,
    DeepProjectStatus,
    ExecutionContext,
    ParsedRequirement,
    ExecutionResult,
    ProgressUpdate,
)
from src.deep_engine.parser import RequirementParser
from src.deep_engine.planner import TaskPlanner
from src.deep_engine.executor import TaskExecutor
from src.deep_engine.engine import DeepEngine, DeepEngineManager, DeepEngineCallbacks
from src.deep_engine.reporter import ProgressReporter


class TestDeepTask:
    def test_create_task(self):
        task = DeepTask.create(
            title="测试任务",
            description="这是一个测试任务",
            prompt="请执行测试",
            order=0,
        )
        assert task.title == "测试任务"
        assert task.description == "这是一个测试任务"
        assert task.prompt == "请执行测试"
        assert task.status == DeepTaskStatus.PENDING
        assert len(task.task_id) == 8

    def test_task_start(self):
        task = DeepTask.create("任务", "描述", "提示")
        task.start()
        assert task.status == DeepTaskStatus.IN_PROGRESS
        assert task.started_at is not None

    def test_task_complete(self):
        task = DeepTask.create("任务", "描述", "提示")
        task.start()
        task.complete("执行结果")
        assert task.status == DeepTaskStatus.COMPLETED
        assert task.result == "执行结果"
        assert task.completed_at is not None

    def test_task_fail_with_retry(self):
        task = DeepTask.create("任务", "描述", "提示")
        task.max_retries = 2
        task.start()
        task.fail("错误1")
        assert task.status == DeepTaskStatus.PENDING
        assert task.retry_count == 1

        task.start()
        task.fail("错误2")
        assert task.status == DeepTaskStatus.FAILED
        assert task.retry_count == 2
        assert task.error == "错误2"

    def test_task_skip(self):
        task = DeepTask.create("任务", "描述", "提示")
        task.skip("依赖失败")
        assert task.status == DeepTaskStatus.SKIPPED
        assert task.error == "依赖失败"

    def test_task_is_ready(self):
        task1 = DeepTask.create("任务1", "描述", "提示")
        task1.task_id = "task1"
        
        task2 = DeepTask.create("任务2", "描述", "提示", dependencies=["task1"])
        
        assert task2.is_ready(set()) is False
        assert task2.is_ready({"task1"}) is True

    def test_task_duration(self):
        task = DeepTask.create("任务", "描述", "提示")
        task.started_at = 100.0
        task.completed_at = 105.5
        assert task.duration() == 5.5

    def test_task_to_dict_and_from_dict(self):
        task = DeepTask.create("任务", "描述", "提示", order=1, dependencies=["dep1"])
        task.start()
        task.complete("结果")
        
        data = task.to_dict()
        restored = DeepTask.from_dict(data)
        
        assert restored.task_id == task.task_id
        assert restored.title == task.title
        assert restored.status == task.status
        assert restored.result == task.result


class TestDeepProject:
    def test_create_project(self):
        project = DeepProject.create("测试项目", "/tmp/test")
        assert project.name == "测试项目"
        assert project.root_path == "/tmp/test"
        assert project.status == DeepProjectStatus.IDLE
        assert len(project.project_id) == 8

    def test_set_tasks(self):
        project = DeepProject.create("项目", "/tmp")
        tasks = [
            DeepTask.create("任务1", "描述1", "提示1"),
            DeepTask.create("任务2", "描述2", "提示2"),
        ]
        project.set_tasks(tasks)
        
        assert len(project.tasks) == 2
        assert project.tasks[0].order == 0
        assert project.tasks[1].order == 1

    def test_project_lifecycle(self):
        project = DeepProject.create("项目", "/tmp")
        
        project.start()
        assert project.status == DeepProjectStatus.EXECUTING
        assert project.started_at is not None
        
        project.pause()
        assert project.status == DeepProjectStatus.PAUSED
        
        project.resume()
        assert project.status == DeepProjectStatus.EXECUTING
        
        project.complete()
        assert project.status == DeepProjectStatus.COMPLETED
        assert project.completed_at is not None

    def test_project_fail(self):
        project = DeepProject.create("项目", "/tmp")
        project.fail("执行错误")
        assert project.status == DeepProjectStatus.FAILED
        assert project.error == "执行错误"

    def test_get_next_task(self):
        project = DeepProject.create("项目", "/tmp")
        task1 = DeepTask.create("任务1", "描述1", "提示1")
        task2 = DeepTask.create("任务2", "描述2", "提示2", dependencies=[task1.task_id])
        project.set_tasks([task1, task2])
        
        next_task = project.get_next_task()
        assert next_task == task1
        
        task1.complete("完成")
        next_task = project.get_next_task()
        assert next_task == task2

    def test_project_counts(self):
        project = DeepProject.create("项目", "/tmp")
        tasks = [
            DeepTask.create("任务1", "", ""),
            DeepTask.create("任务2", "", ""),
            DeepTask.create("任务3", "", ""),
        ]
        project.set_tasks(tasks)
        
        assert project.total_count == 3
        assert project.completed_count == 0
        assert project.pending_count == 3
        
        tasks[0].complete("完成")
        tasks[1].fail("失败")
        tasks[1].fail("失败")
        
        assert project.completed_count == 1
        assert project.failed_count == 1

    def test_project_is_completed(self):
        project = DeepProject.create("项目", "/tmp")
        tasks = [DeepTask.create("任务1", "", "")]
        project.set_tasks(tasks)
        
        assert project.is_completed is False
        
        tasks[0].complete("完成")
        assert project.is_completed is True

    def test_project_to_dict_and_from_dict(self):
        project = DeepProject.create("项目", "/tmp")
        project.set_requirement(ParsedRequirement(
            original_text="需求",
            summary="概述",
            goals=["目标1", "目标2"],
        ))
        tasks = [DeepTask.create("任务1", "描述", "提示")]
        project.set_tasks(tasks)
        project.start()
        
        data = project.to_dict()
        restored = DeepProject.from_dict(data)
        
        assert restored.project_id == project.project_id
        assert restored.name == project.name
        assert restored.status == project.status
        assert len(restored.tasks) == 1
        assert restored.requirement.summary == "概述"


class TestParsedRequirement:
    def test_create_requirement(self):
        req = ParsedRequirement(
            original_text="原始需求",
            summary="需求概述",
            goals=["目标1", "目标2"],
            constraints=["约束1"],
            tech_stack=["Python"],
            estimated_complexity="medium",
        )
        assert req.summary == "需求概述"
        assert len(req.goals) == 2
        assert req.estimated_complexity == "medium"

    def test_requirement_to_dict_and_from_dict(self):
        req = ParsedRequirement(
            original_text="需求",
            summary="概述",
            goals=["目标"],
            tech_stack=["Python"],
        )
        data = req.to_dict()
        restored = ParsedRequirement.from_dict(data)
        
        assert restored.summary == req.summary
        assert restored.goals == req.goals


class TestExecutionResult:
    def test_create_result(self):
        result = ExecutionResult(
            task_id="task1",
            success=True,
            output="执行输出",
            duration=5.0,
        )
        assert result.success is True
        assert result.output == "执行输出"
        assert result.duration == 5.0

    def test_failed_result(self):
        result = ExecutionResult(
            task_id="task1",
            success=False,
            output="",
            duration=1.0,
            error="执行失败",
        )
        assert result.success is False
        assert result.error == "执行失败"


class TestProgressUpdate:
    def test_progress_percent(self):
        update = ProgressUpdate(
            project_id="proj1",
            current_task=None,
            completed_count=3,
            total_count=10,
            status=DeepProjectStatus.EXECUTING,
            message="执行中",
        )
        assert update.progress_percent == 30.0

    def test_progress_bar(self):
        update = ProgressUpdate(
            project_id="proj1",
            current_task=None,
            completed_count=5,
            total_count=10,
            status=DeepProjectStatus.EXECUTING,
            message="执行中",
        )
        bar = update.progress_bar
        assert "50%" in bar
        assert "█" in bar


class TestProgressReporter:
    @pytest.fixture
    def reporter(self):
        return ProgressReporter()

    def test_format_planning_start(self, reporter):
        msg = reporter.format_planning_start("帮我写一个爬虫")
        assert "Deep Agent" in msg
        assert "帮我写一个爬虫" in msg

    def test_format_planning_done(self, reporter):
        project = DeepProject.create("测试项目", "/tmp/test")
        tasks = [
            DeepTask.create("任务1", "描述1", "提示1"),
            DeepTask.create("任务2", "描述2", "提示2"),
        ]
        project.set_tasks(tasks)
        
        msg = reporter.format_planning_done(project)
        assert "任务规划完成" in msg
        assert "测试项目" in msg
        assert "2 个任务" in msg

    def test_format_task_start(self, reporter):
        task = DeepTask.create("创建文件", "创建项目文件", "提示")
        msg = reporter.format_task_start(task, 1, 5)
        assert "[1/5]" in msg
        assert "创建文件" in msg

    def test_format_task_done_success(self, reporter):
        task = DeepTask.create("任务", "描述", "提示")
        result = ExecutionResult(
            task_id=task.task_id,
            success=True,
            output="执行成功",
            duration=3.5,
        )
        msg = reporter.format_task_done(task, result, 2, 5)
        assert "任务完成" in msg
        assert "3.5s" in msg

    def test_format_task_done_failed(self, reporter):
        task = DeepTask.create("任务", "描述", "提示")
        result = ExecutionResult(
            task_id=task.task_id,
            success=False,
            output="",
            duration=1.0,
            error="执行错误",
        )
        msg = reporter.format_task_done(task, result, 2, 5)
        assert "任务失败" in msg
        assert "执行错误" in msg

    def test_format_project_done_completed(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.COMPLETED
        project.started_at = time.time() - 10
        project.completed_at = time.time()
        tasks = [DeepTask.create("任务", "", "")]
        tasks[0].complete("完成")
        project.set_tasks(tasks)
        
        msg = reporter.format_project_done(project)
        assert "全部任务完成" in msg

    def test_format_project_done_failed(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.FAILED
        tasks = [DeepTask.create("任务", "", "")]
        tasks[0].status = DeepTaskStatus.FAILED
        project.set_tasks(tasks)
        
        msg = reporter.format_project_done(project)
        assert "有失败" in msg

    def test_format_error(self, reporter):
        msg = reporter.format_error("发生了错误")
        assert "错误" in msg
        assert "发生了错误" in msg

    def test_format_status(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.EXECUTING
        tasks = [
            DeepTask.create("任务1", "", ""),
            DeepTask.create("任务2", "", ""),
        ]
        tasks[0].complete("完成")
        project.set_tasks(tasks)

        msg = reporter.format_status(project)
        assert "项目" in msg
        assert "1/2" in msg

    def test_get_planning_start_title(self, reporter):
        title = reporter.get_planning_start_title()
        assert "Deep Agent" in title

    def test_get_planning_done_title(self, reporter):
        title = reporter.get_planning_done_title()
        assert "任务规划完成" in title

    def test_get_task_start_title(self, reporter):
        title = reporter.get_task_start_title(2, 5)
        assert "[2/5]" in title

    def test_get_task_done_title_success(self, reporter):
        title = reporter.get_task_done_title(True, 3, 5)
        assert "任务完成" in title
        assert "[3/5]" in title

    def test_get_task_done_title_failed(self, reporter):
        title = reporter.get_task_done_title(False, 3, 5)
        assert "任务失败" in title
        assert "[3/5]" in title

    def test_get_project_done_title_completed(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.COMPLETED
        title = reporter.get_project_done_title(project)
        assert "全部任务完成" in title

    def test_get_project_done_title_failed(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.FAILED
        title = reporter.get_project_done_title(project)
        assert "有失败" in title

    def test_get_project_done_title_paused(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.PAUSED
        title = reporter.get_project_done_title(project)
        assert "暂停" in title

    def test_get_error_title(self, reporter):
        title = reporter.get_error_title()
        assert "错误" in title

    def test_get_status_title(self, reporter):
        title = reporter.get_status_title()
        assert "状态" in title

    def test_get_progress_info(self, reporter):
        project = DeepProject.create("测试项目", "/tmp")
        project.status = DeepProjectStatus.EXECUTING
        tasks = [
            DeepTask.create("任务1", "", ""),
            DeepTask.create("任务2", "", ""),
        ]
        tasks[0].complete("完成")
        project.set_tasks(tasks)

        info = reporter.get_progress_info(project)
        assert info["is_executing"] is True
        assert info["is_paused"] is False
        assert info["completed_count"] == 1
        assert info["total_count"] == 2
        assert "50%" in info["progress_bar"]
        assert info["project_name"] == "测试项目"

    def test_get_progress_info_paused(self, reporter):
        project = DeepProject.create("项目", "/tmp")
        project.status = DeepProjectStatus.PAUSED

        info = reporter.get_progress_info(project)
        assert info["is_executing"] is False
        assert info["is_paused"] is True


class TestRequirementParser:
    @pytest.fixture
    def parser(self):
        return RequirementParser()

    def test_is_complex_requirement_simple(self, parser):
        assert parser.is_complex_requirement("写个函数") is False

    def test_is_complex_requirement_complex(self, parser):
        text = "帮我写一个爬虫，首先要创建项目结构，然后实现数据抓取，接着解析数据，最后保存到文件。"
        assert parser.is_complex_requirement(text) is True

    def test_is_complex_requirement_numbered(self, parser):
        text = "1. 创建项目 2. 写代码 3. 测试"
        assert parser.is_complex_requirement(text) is True


class TestDeepEngineManager:
    def test_get_or_create(self):
        manager = DeepEngineManager()
        engine1 = manager.get_or_create("chat1", "/tmp/project1")
        engine2 = manager.get_or_create("chat1", "/tmp/project1")
        engine3 = manager.get_or_create("chat1", "/tmp/project2")
        
        assert engine1 is engine2
        assert engine1 is not engine3

    def test_get_nonexistent(self):
        manager = DeepEngineManager()
        engine = manager.get("chat1", "/tmp/nonexistent")
        assert engine is None

    def test_remove(self):
        manager = DeepEngineManager()
        manager.get_or_create("chat1", "/tmp/project1")
        manager.remove("chat1", "/tmp/project1")
        
        engine = manager.get("chat1", "/tmp/project1")
        assert engine is None

    def test_cleanup_all(self):
        manager = DeepEngineManager()
        manager.get_or_create("chat1", "/tmp/project1")
        manager.get_or_create("chat2", "/tmp/project2")
        manager.cleanup_all()
        
        assert manager.get("chat1", "/tmp/project1") is None
        assert manager.get("chat2", "/tmp/project2") is None

    def test_get_active_engines_and_find_by_deep_project_id(self):
        manager = DeepEngineManager()
        e1 = manager.get_or_create("chat1", "/tmp/project1")
        e2 = manager.get_or_create("chat1", "/tmp/project2")

        # simulate running engines
        e1._is_running = True
        e2._is_running = True

        active = manager.get_active_engines("chat1")
        assert len(active) == 2

        # simulate planned deep project
        p = DeepProject.create("p", "/tmp/project1")
        e1._project = p
        found = manager.find_by_deep_project_id("chat1", p.project_id)
        assert found is e1


class TestIntentRecognizerDeepCommands:
    @pytest.fixture
    def recognizer(self):
        from src.agent.intent_recognizer import IntentRecognizer
        return IntentRecognizer()

    def test_deep_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/deep 帮我写一个爬虫")
        assert result is not None
        assert result.primary_intent == IntentType.ENTER_DEEP
        assert result.primary_data.get("requirement") == "帮我写一个爬虫"

    def test_deep_status_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/deep_status")
        assert result is not None
        assert result.primary_intent == IntentType.DEEP_STATUS

    def test_deep_status_all_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/deep_status all")
        assert result is not None
        assert result.primary_intent == IntentType.DEEP_STATUS
        assert result.primary_data.get("arg") == "all"

    def test_stop_deep_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/stop_deep")
        assert result is not None
        assert result.primary_intent == IntentType.STOP_DEEP

    def test_stop_deep_all_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/stop_deep all")
        assert result is not None
        assert result.primary_intent == IntentType.STOP_DEEP
        assert result.primary_data.get("arg") == "all"


class TestWsClientDeepCommands:
    def test_is_deep_command_deep(self):
        from src.feishu.ws_client import FeishuWSClient
        client = FeishuWSClient.__new__(FeishuWSClient)
        assert client._is_deep_command("/deep 帮我写爬虫") is True
        assert client._is_deep_command("/Deep 需求") is True
        assert client._is_deep_command("/DEEP test") is True

    def test_is_deep_command_deep_status(self):
        from src.feishu.ws_client import FeishuWSClient
        client = FeishuWSClient.__new__(FeishuWSClient)
        assert client._is_deep_command("/deep_status") is True
        assert client._is_deep_command("/DEEP_STATUS") is True

    def test_is_deep_command_stop_deep(self):
        from src.feishu.ws_client import FeishuWSClient
        client = FeishuWSClient.__new__(FeishuWSClient)
        assert client._is_deep_command("/stop_deep") is True
        assert client._is_deep_command("/STOP_DEEP") is True

    def test_is_deep_command_not_deep(self):
        from src.feishu.ws_client import FeishuWSClient
        client = FeishuWSClient.__new__(FeishuWSClient)
        assert client._is_deep_command("帮我写代码") is False
        assert client._is_deep_command("/coco") is False
        assert client._is_deep_command("/exit") is False
        assert client._is_deep_command("deep 模式") is False


class TestContextEntry:
    def test_create_entry(self):
        entry = ContextEntry(
            entry_type="task_result",
            content="任务完成",
            task_id="abc123",
        )
        assert entry.entry_type == "task_result"
        assert entry.content == "任务完成"
        assert entry.task_id == "abc123"
        assert entry.timestamp > 0

    def test_entry_to_dict_and_from_dict(self):
        entry = ContextEntry(
            entry_type="user_injection",
            content="改用 PostgreSQL",
            task_id=None,
            timestamp=1000.0,
        )
        data = entry.to_dict()
        restored = ContextEntry.from_dict(data)
        assert restored.entry_type == entry.entry_type
        assert restored.content == entry.content
        assert restored.task_id is None
        assert restored.timestamp == 1000.0


class TestExecutionContext:
    def test_add_result(self):
        ctx = ExecutionContext()
        ctx.add_result("t1", "创建文件", True, "文件已创建")
        assert ctx.entry_count == 1
        # task_result 不触发 flag
        assert ctx.has_meaningful_context() is False

    def test_inject_user_context(self):
        ctx = ExecutionContext()
        ctx.inject_user_context("改用 PostgreSQL")
        assert ctx.entry_count == 1
        assert ctx.has_meaningful_context() is True

    def test_consume_flag(self):
        ctx = ExecutionContext()
        ctx.inject_user_context("新需求")
        assert ctx.has_meaningful_context() is True
        ctx.consume_new_context_flag()
        assert ctx.has_meaningful_context() is False

    def test_record_deviation(self):
        ctx = ExecutionContext()
        ctx.record_deviation("t1", "输出格式不匹配")
        assert ctx.entry_count == 1

    def test_record_adaptation(self):
        ctx = ExecutionContext()
        ctx.record_adaptation("t1", "已调整数据库类型")
        assert ctx.entry_count == 1

    def test_build_context_prompt_empty(self):
        ctx = ExecutionContext()
        assert ctx.build_context_prompt() == ""

    def test_build_context_prompt_with_entries(self):
        ctx = ExecutionContext()
        ctx.add_result("t1", "创建文件", True, "完成")
        ctx.inject_user_context("改用 PostgreSQL")
        prompt = ctx.build_context_prompt()
        assert "执行上下文" in prompt
        assert "任务结果" in prompt
        assert "用户指示" in prompt
        assert "PostgreSQL" in prompt

    def test_build_context_prompt_max_entries(self):
        ctx = ExecutionContext()
        for i in range(20):
            ctx.add_result(f"t{i}", f"任务{i}", True, f"完成{i}")
        prompt = ctx.build_context_prompt(max_entries=5)
        # 只包含最后 5 条
        assert "任务15" in prompt
        assert "任务19" in prompt
        # 不包含前面的
        assert "任务0" not in prompt

    def test_thread_safety(self):
        ctx = ExecutionContext()
        errors = []

        def writer():
            try:
                for i in range(100):
                    ctx.inject_user_context(f"消息 {i}")
                    ctx.add_result(f"t{i}", f"任务{i}", True, "完成")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    ctx.has_meaningful_context()
                    ctx.build_context_prompt()
                    ctx.entry_count
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0

    def test_to_dict_and_from_dict(self):
        ctx = ExecutionContext()
        ctx.add_result("t1", "任务1", True, "完成")
        ctx.inject_user_context("新需求")

        data = ctx.to_dict()
        assert len(data["entries"]) == 2
        assert data["new_context_flag"] is True

        restored = ExecutionContext.from_dict(data)
        assert restored.entry_count == 2
        assert restored.has_meaningful_context() is True

    def test_from_dict_empty(self):
        ctx = ExecutionContext.from_dict({})
        assert ctx.entry_count == 0
        assert ctx.has_meaningful_context() is False


class TestDeepTaskAdaptedFields:
    def test_adapted_fields_default_none(self):
        task = DeepTask.create("任务", "描述", "提示")
        assert task.original_prompt is None
        assert task.adapted_prompt is None

    def test_adapted_fields_set(self):
        task = DeepTask.create("任务", "描述", "原始提示")
        task.original_prompt = "原始提示"
        task.prompt = "调整后提示"
        task.adapted_prompt = "调整后提示"
        assert task.original_prompt == "原始提示"
        assert task.adapted_prompt == "调整后提示"

    def test_adapted_fields_serialization(self):
        task = DeepTask.create("任务", "描述", "提示")
        task.original_prompt = "原始"
        task.adapted_prompt = "调整后"
        data = task.to_dict()
        restored = DeepTask.from_dict(data)
        assert restored.original_prompt == "原始"
        assert restored.adapted_prompt == "调整后"

    def test_adapted_fields_absent_in_dict(self):
        data = {
            "task_id": "abc",
            "title": "任务",
            "description": "描述",
            "prompt": "提示",
        }
        task = DeepTask.from_dict(data)
        assert task.original_prompt is None
        assert task.adapted_prompt is None


class TestTaskPlannerAdapt:
    @pytest.fixture
    def planner(self):
        return TaskPlanner()

    @patch("src.deep_engine.planner.TaskPlanner._get_llm")
    def test_adapt_returns_adapted(self, mock_get_llm, planner):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='```json\n{"should_adapt": true, "reason": "用户要求改用PostgreSQL", "adapted_prompt": "使用 PostgreSQL 创建数据库"}\n```'
        )
        mock_get_llm.return_value = mock_llm

        task = DeepTask.create("创建数据库", "创建数据库结构", "使用 SQLite 创建数据库")
        context = "## 执行上下文\n- 💬 用户指示: 改用 PostgreSQL"

        was_adapted, prompt, reason = planner.adapt_task_prompt(task, context)
        assert was_adapted is True
        assert "PostgreSQL" in prompt
        assert "PostgreSQL" in reason

    @patch("src.deep_engine.planner.TaskPlanner._get_llm")
    def test_adapt_returns_no_adapt(self, mock_get_llm, planner):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='```json\n{"should_adapt": false, "reason": "无需调整"}\n```'
        )
        mock_get_llm.return_value = mock_llm

        task = DeepTask.create("创建文件", "创建项目文件", "创建 main.py")
        context = "## 执行上下文\n- 📋 任务结果: 完成"

        was_adapted, prompt, reason = planner.adapt_task_prompt(task, context)
        assert was_adapted is False
        assert prompt == "创建 main.py"

    @patch("src.deep_engine.planner.TaskPlanner._get_llm")
    def test_adapt_error_fallback(self, mock_get_llm, planner):
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("LLM 调用失败")
        mock_get_llm.return_value = mock_llm

        task = DeepTask.create("任务", "描述", "原始 prompt")
        was_adapted, prompt, reason = planner.adapt_task_prompt(task, "上下文")
        assert was_adapted is False
        assert prompt == "原始 prompt"
        assert "异常" in reason

    @patch("src.deep_engine.planner.TaskPlanner._get_llm")
    def test_adapt_unparseable_response(self, mock_get_llm, planner):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="这不是 JSON")
        mock_get_llm.return_value = mock_llm

        task = DeepTask.create("任务", "描述", "原始 prompt")
        was_adapted, prompt, reason = planner.adapt_task_prompt(task, "上下文")
        assert was_adapted is False
        assert prompt == "原始 prompt"


class TestDeepEngineContextInjection:
    def test_inject_context(self):
        engine = DeepEngine.__new__(DeepEngine)
        engine._execution_context = ExecutionContext()
        engine.inject_context("改用 PostgreSQL")
        assert engine._execution_context.has_meaningful_context() is True
        assert engine._execution_context.entry_count == 1

    def test_execution_context_property(self):
        engine = DeepEngine.__new__(DeepEngine)
        engine._execution_context = ExecutionContext()
        assert engine.execution_context is engine._execution_context

    @patch("src.deep_engine.engine.DeepEngine._ensure_executor")
    @patch("src.deep_engine.planner.TaskPlanner.adapt_task_prompt")
    def test_execute_triggers_adaptation(self, mock_adapt, mock_ensure_exec):
        """测试 execute 循环中上下文注入触发 adaptation。"""
        engine = DeepEngine.__new__(DeepEngine)
        engine.settings = MagicMock()
        engine._planner = TaskPlanner.__new__(TaskPlanner)
        engine._planner._llm = None
        engine._planner.settings = MagicMock()
        engine._execution_context = ExecutionContext()
        engine._should_stop = False
        engine._is_running = False

        # 创建项目
        project = DeepProject.create("测试", "/tmp/test")
        task = DeepTask.create("任务1", "描述", "原始prompt")
        project.set_tasks([task])
        engine._project = project

        # Mock executor — 需要正确更新 task 状态
        mock_executor = MagicMock()
        def execute_side_effect(t, on_chunk=None):
            t.start()
            t.complete("完成")
            return ExecutionResult(
                task_id=t.task_id, success=True, output="完成", duration=1.0
            )
        mock_executor.execute.side_effect = execute_side_effect
        mock_ensure_exec.return_value = mock_executor

        # Mock adapt
        mock_adapt.return_value = (True, "调整后的 prompt", "用户要求变更")

        # 注入上下文
        engine.inject_context("请使用 TypeScript")

        # 记录回调
        adapted_calls = []
        callbacks = DeepEngineCallbacks(
            on_context_adapted=lambda t, r, p: adapted_calls.append((t, r, p)),
        )

        engine.execute(callbacks)

        # 验证 adaptation 被触发
        assert mock_adapt.called
        assert len(adapted_calls) == 1
        assert adapted_calls[0][1] == "用户要求变更"

    @patch("src.deep_engine.engine.DeepEngine._ensure_executor")
    def test_execute_no_adaptation_without_context(self, mock_ensure_exec):
        """没有新上下文时不触发 adaptation。"""
        engine = DeepEngine.__new__(DeepEngine)
        engine.settings = MagicMock()
        engine._planner = MagicMock()
        engine._execution_context = ExecutionContext()
        engine._should_stop = False
        engine._is_running = False

        project = DeepProject.create("测试", "/tmp/test")
        task = DeepTask.create("任务1", "描述", "prompt")
        project.set_tasks([task])
        engine._project = project

        mock_executor = MagicMock()
        def execute_side_effect(t, on_chunk=None):
            t.start()
            t.complete("完成")
            return ExecutionResult(
                task_id=t.task_id, success=True, output="完成", duration=1.0
            )
        mock_executor.execute.side_effect = execute_side_effect
        mock_ensure_exec.return_value = mock_executor

        engine.execute()

        # adapt_task_prompt 不应被调用
        engine._planner.adapt_task_prompt.assert_not_called()

    @patch("src.deep_engine.engine.DeepEngine._ensure_executor")
    @patch("src.deep_engine.planner.TaskPlanner.replan_task")
    def test_execute_replan_on_failure(self, mock_replan, mock_ensure_exec):
        """任务失败时触发 replan。"""
        engine = DeepEngine.__new__(DeepEngine)
        engine.settings = MagicMock()
        engine._planner = TaskPlanner.__new__(TaskPlanner)
        engine._planner._llm = None
        engine._planner.settings = MagicMock()
        engine._execution_context = ExecutionContext()
        engine._should_stop = False
        engine._is_running = False

        project = DeepProject.create("测试", "/tmp/test")
        task = DeepTask.create("任务1", "描述", "prompt")
        task.max_retries = 2
        project.set_tasks([task])
        engine._project = project

        # 第一次失败（task.fail 增加 retry_count 但保持 PENDING），第二次成功
        call_count = [0]
        mock_executor = MagicMock()
        def execute_side_effect(t, on_chunk=None):
            call_count[0] += 1
            t.start()
            if call_count[0] == 1:
                t.fail("编译错误")
                return ExecutionResult(
                    task_id=t.task_id, success=False, output="", duration=1.0, error="编译错误"
                )
            else:
                t.complete("完成")
                return ExecutionResult(
                    task_id=t.task_id, success=True, output="完成", duration=1.0
                )
        mock_executor.execute.side_effect = execute_side_effect
        mock_ensure_exec.return_value = mock_executor

        replanned_task = DeepTask.create("任务1", "[重试] 描述", "改进后的 prompt")
        mock_replan.return_value = replanned_task

        engine.execute()

        assert mock_replan.called


class TestProgressReporterContextMethods:
    @pytest.fixture
    def reporter(self):
        return ProgressReporter()

    def test_format_context_injected(self, reporter):
        msg = reporter.format_context_injected("改用 PostgreSQL")
        assert "上下文已注入" in msg
        assert "PostgreSQL" in msg

    def test_format_context_injected_truncates(self, reporter):
        long_msg = "x" * 300
        msg = reporter.format_context_injected(long_msg)
        assert "..." in msg

    def test_format_task_adapted(self, reporter):
        task = DeepTask.create("创建数据库", "描述", "prompt")
        msg = reporter.format_task_adapted(task, "用户要求变更", "使用 PostgreSQL 创建")
        assert "任务指令已调整" in msg
        assert "创建数据库" in msg
        assert "用户要求变更" in msg

    def test_get_context_injected_title(self, reporter):
        title = reporter.get_context_injected_title()
        assert "上下文已注入" in title

    def test_get_task_adapted_title(self, reporter):
        title = reporter.get_task_adapted_title()
        assert "任务指令已调整" in title


class TestIntentRecognizerDeepUpdate:
    @pytest.fixture
    def recognizer(self):
        from src.agent.intent_recognizer import IntentRecognizer
        return IntentRecognizer()

    def test_deep_update_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/deep_update 改用 PostgreSQL")
        assert result is not None
        assert result.primary_intent == IntentType.DEEP_UPDATE
        assert result.primary_data.get("message") == "改用 PostgreSQL"

    def test_deep_update_exact(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/deep_update")
        assert result is not None
        assert result.primary_intent == IntentType.DEEP_UPDATE

    def test_deep_update_in_intent_map(self):
        from src.agent.intent_recognizer import IntentRecognizer, IntentType
        assert "deep_update" in IntentRecognizer.INTENT_MAP
        assert IntentRecognizer.INTENT_MAP["deep_update"] == IntentType.DEEP_UPDATE


class TestWsClientDeepUpdate:
    def test_is_deep_command_deep_update(self):
        from src.feishu.ws_client import FeishuWSClient
        client = FeishuWSClient.__new__(FeishuWSClient)
        assert client._is_deep_command("/deep_update 改用 PostgreSQL") is True
        assert client._is_deep_command("/deep_update") is True
        assert client._is_deep_command("/DEEP_UPDATE test") is True
