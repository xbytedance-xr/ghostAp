import unittest
import json

from src.feishu.message_formatter import FeishuMessageFormatter


class TestFormatCommandResult(unittest.TestCase):

    def test_success_title(self):
        msg_type, data = FeishuMessageFormatter.format_command_result(
            command="ls", working_dir="/tmp", stdout="ok", stderr="",
            return_code=0, success=True,
        )
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "✅ 执行成功")

    def test_failure_title(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="ls", working_dir="/tmp", stdout="", stderr="fail",
            return_code=1, success=False,
        )
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "❌ 执行失败")

    def test_working_dir_present(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="pwd", working_dir="/home/user", stdout="out", stderr="",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("📂 **工作目录**: `/home/user`", md)

    def test_working_dir_none(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="pwd", working_dir=None, stdout="out", stderr="",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertNotIn("📂 **工作目录**", md)

    def test_error_message_shown_directly(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="rm", working_dir=None, stdout="ignored", stderr="ignored",
            return_code=1, success=False, error_message="timeout",
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("**错误**: timeout", md)
        self.assertNotIn("📤 输出", md)
        self.assertNotIn("⚠️ 错误输出", md)

    def test_stdout_displayed(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="echo hi", working_dir=None, stdout="hi\n", stderr="",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("**📤 输出**:", md)
        self.assertIn("hi", md)

    def test_stderr_displayed(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="cmd", working_dir=None, stdout="", stderr="error msg\n",
            return_code=1, success=False,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("**⚠️ 错误输出**:", md)
        self.assertIn("error msg", md)

    def test_stdout_truncated_over_2000(self):
        long_out = "x" * 2500
        _, data = FeishuMessageFormatter.format_command_result(
            command="cat", working_dir=None, stdout=long_out, stderr="",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("... (输出已截断)", md)
        self.assertNotIn("x" * 2500, md)

    def test_stderr_truncated_over_1000(self):
        long_err = "e" * 1500
        _, data = FeishuMessageFormatter.format_command_result(
            command="cmd", working_dir=None, stdout="", stderr=long_err,
            return_code=1, success=False,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("... (已截断)", md)

    def test_no_stdout_no_stderr(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="true", working_dir=None, stdout="", stderr="",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("*（无输出）*", md)

    def test_return_code_shown(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="exit", working_dir=None, stdout="", stderr="",
            return_code=42, success=False,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("🔢 **返回码**: 42", md)

    def test_both_stdout_and_stderr(self):
        _, data = FeishuMessageFormatter.format_command_result(
            command="make", working_dir="/proj", stdout="building...", stderr="warn: x",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("**📤 输出**:", md)
        self.assertIn("**⚠️ 错误输出**:", md)
        self.assertNotIn("*（无输出）*", md)


class TestSafeTruncateMarkdown(unittest.TestCase):

    def test_none_returns_empty(self):
        self.assertEqual(FeishuMessageFormatter.safe_truncate_markdown(None), "")

    def test_empty_returns_empty(self):
        self.assertEqual(FeishuMessageFormatter.safe_truncate_markdown(""), "")

    def test_within_length_returns_as_is(self):
        text = "hello world"
        self.assertEqual(
            FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100),
            text,
        )

    def test_exact_length_returns_as_is(self):
        text = "a" * 100
        self.assertEqual(
            FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100),
            text,
        )

    def test_over_length_keep_head_even_fence(self):
        text = "abc```def```" + "x" * 200
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100, keep_head=True)
        self.assertIn("⚠️ 内容过长", result)
        self.assertTrue(result.startswith("abc"))
        fence_count = result.count("```")
        self.assertEqual(fence_count % 2, 0)

    def test_over_length_keep_head_odd_fence(self):
        text = "abc```def" + "x" * 200
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100, keep_head=True)
        self.assertIn("⚠️ 内容过长", result)
        fence_count_in_truncated = result.split("⚠️")[0].count("```")
        self.assertEqual(fence_count_in_truncated % 2, 0)

    def test_over_length_keep_tail_even_fence(self):
        text = "x" * 200 + "```abc```def"
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100, keep_head=False)
        self.assertIn("⚠️ 内容过长", result)
        self.assertTrue(result.startswith("\n\n> ⚠️"))

    def test_over_length_keep_tail_odd_fence(self):
        text = "x" * 200 + "```tail_content"
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100, keep_head=False)
        self.assertIn("⚠️ 内容过长", result)
        parts_after_notice = result.split("⚠️ 内容过长（超过 100 字符），已自动截断。\n\n")[1]
        self.assertTrue(parts_after_notice.startswith("```\n"))

    def test_keep_head_truncated_content_length(self):
        text = "a" * 300
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=200, keep_head=True)
        self.assertLessEqual(len(result), 300)
        self.assertIn("⚠️ 内容过长", result)

    def test_keep_tail_notice_prepended(self):
        text = "b" * 300
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=200, keep_head=False)
        self.assertTrue(result.startswith("\n\n> ⚠️ 内容过长"))


class TestIsPostFormat(unittest.TestCase):

    def test_valid_post_tuple(self):
        self.assertTrue(FeishuMessageFormatter.is_post_format(("post", "data")))

    def test_non_post_type(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format(("text", "data")))

    def test_plain_string(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format("string"))

    def test_single_element_tuple(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format(("post",)))

    def test_none(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format(None))

    def test_three_element_tuple(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format(("post", "a", "b")))

    def test_list_not_tuple(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format(["post", "data"]))

    def test_integer(self):
        self.assertFalse(FeishuMessageFormatter.is_post_format(42))


class TestFormatSafetyBlock(unittest.TestCase):

    def test_returns_post_format(self):
        msg_type, data = FeishuMessageFormatter.format_safety_block("rm -rf /", "dangerous")
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🚫 命令被安全检查拦截")

    def test_contains_command_and_reason(self):
        _, data = FeishuMessageFormatter.format_safety_block("sudo reboot", "needs approval")
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("`sudo reboot`", md)
        self.assertIn("needs approval", md)

    def test_contains_admin_hint(self):
        _, data = FeishuMessageFormatter.format_safety_block("cmd", "reason")
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("如需执行此命令，请联系管理员", md)


class TestFormatHelp(unittest.TestCase):

    def test_smart_mode(self):
        msg_type, data = FeishuMessageFormatter.format_help("/home", is_coco_mode=False)
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "💡 使用说明")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertIn("`/home`", md)
        self.assertIn("Shell 模式", md)
        self.assertIn("Coco 模式", md)
        self.assertIn("目录切换", md)

    def test_coco_mode(self):
        _, data = FeishuMessageFormatter.format_help("/home", is_coco_mode=True)
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🤖 当前在 Coco 模式")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertIn("/end_coco", md)
        self.assertIn("/coco_info", md)
        self.assertNotIn("/home", md)


class TestFormatCocoEnter(unittest.TestCase):

    def test_returns_post_format(self):
        msg_type, data = FeishuMessageFormatter.format_coco_enter()
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🤖 已进入 Coco 模式")

    def test_contains_hints(self):
        _, data = FeishuMessageFormatter.format_coco_enter()
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("/end_coco", md)
        self.assertIn("/coco_info", md)


class TestFormatCocoResponse(unittest.TestCase):

    def test_returns_post_with_response(self):
        msg_type, data = FeishuMessageFormatter.format_coco_response("hello from coco")
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🤖 Coco")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertEqual(md, "hello from coco")


class TestFormatDirChange(unittest.TestCase):

    def test_success(self):
        result = FeishuMessageFormatter.format_dir_change("/new/path", success=True)
        self.assertEqual(result, "📂 **已切换到**: `/new/path`")

    def test_failure(self):
        result = FeishuMessageFormatter.format_dir_change("/bad/path", success=False)
        self.assertEqual(result, "❌ **切换失败**: /bad/path")


class TestFormatCurrentDir(unittest.TestCase):

    def test_format(self):
        result = FeishuMessageFormatter.format_current_dir("/home/user")
        self.assertEqual(result, "📂 **当前工作目录**: `/home/user`")


class TestFormatMultiTaskPlan(unittest.TestCase):

    def test_returns_post(self):
        tasks = [{"description": "step 1"}, {"description": "step 2"}]
        msg_type, data = FeishuMessageFormatter.format_multi_task_plan(tasks)
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "📋 执行计划")

    def test_tasks_numbered(self):
        tasks = [{"description": "a"}, {"description": "b"}, {"description": "c"}]
        _, data = FeishuMessageFormatter.format_multi_task_plan(tasks)
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("1. a", md)
        self.assertIn("2. b", md)
        self.assertIn("3. c", md)

    def test_missing_description_key(self):
        tasks = [{"command": "ls"}]
        _, data = FeishuMessageFormatter.format_multi_task_plan(tasks)
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("未知操作", md)

    def test_empty_tasks(self):
        tasks = []
        _, data = FeishuMessageFormatter.format_multi_task_plan(tasks)
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("🚀 **开始执行**...", md)

    def test_contains_start_marker(self):
        tasks = [{"description": "do thing"}]
        _, data = FeishuMessageFormatter.format_multi_task_plan(tasks)
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("🚀 **开始执行**...", md)


class TestFormatUnknownIntent(unittest.TestCase):

    def test_returns_post(self):
        msg_type, data = FeishuMessageFormatter.format_unknown_intent()
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🤔 无法理解你的意图")

    def test_contains_suggestions(self):
        _, data = FeishuMessageFormatter.format_unknown_intent()
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("shell 命令", md)
        self.assertIn("Coco 模式", md)


class TestFormatError(unittest.TestCase):

    def test_format(self):
        result = FeishuMessageFormatter.format_error("something broke")
        self.assertEqual(result, "❌ **错误**: something broke")


class TestFormatWarning(unittest.TestCase):

    def test_format(self):
        result = FeishuMessageFormatter.format_warning("be careful")
        self.assertEqual(result, "⚠️ **警告**: be careful")


class TestHelperMethods(unittest.TestCase):

    def test_text_without_styles(self):
        result = FeishuMessageFormatter._text("hello")
        self.assertEqual(result, {"tag": "text", "text": "hello"})

    def test_text_with_styles(self):
        result = FeishuMessageFormatter._text("bold", styles=["bold"])
        self.assertEqual(result, {"tag": "text", "text": "bold", "style": ["bold"]})

    def test_link(self):
        result = FeishuMessageFormatter._link("click", "https://example.com")
        self.assertEqual(result, {"tag": "a", "text": "click", "href": "https://example.com"})

    def test_code_block_default_language(self):
        result = FeishuMessageFormatter._code_block("echo hi")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tag"], "code_block")
        self.assertEqual(result[0]["language"], "BASH")
        self.assertEqual(result[0]["text"], "echo hi")

    def test_code_block_custom_language(self):
        result = FeishuMessageFormatter._code_block("print(1)", language="PYTHON")
        self.assertEqual(result[0]["language"], "PYTHON")

    def test_md(self):
        result = FeishuMessageFormatter._md("**bold**")
        self.assertEqual(result, [{"tag": "md", "text": "**bold**"}])

    def test_hr(self):
        result = FeishuMessageFormatter._hr()
        self.assertEqual(result, [{"tag": "hr"}])


if __name__ == "__main__":
    unittest.main()
