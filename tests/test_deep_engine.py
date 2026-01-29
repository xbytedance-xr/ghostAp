import pytest
import time
from unittest.mock import patch, MagicMock, Mock

from src.deep_engine.models import (
    DeepTask,
    DeepTaskStatus,
    DeepProject,
    DeepProjectStatus,
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
        assert "Deep Engine" in msg
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

    def test_stop_deep_command(self, recognizer):
        from src.agent.intent_recognizer import IntentType
        result = recognizer._quick_match("/stop_deep")
        assert result is not None
        assert result.primary_intent == IntentType.STOP_DEEP


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
