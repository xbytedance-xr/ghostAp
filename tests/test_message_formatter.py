import json
import unittest

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
        long_out = "x" * 20000
        _, data = FeishuMessageFormatter.format_command_result(
            command="cat", working_dir=None, stdout=long_out, stderr="",
            return_code=0, success=True,
        )
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("... (输出已截断)", md)
        self.assertNotIn("x" * 20000, md)

    def test_stderr_truncated_over_1000(self):
        long_err = "e" * 10000
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

    def test_none_and_empty(self):
        self.assertEqual(FeishuMessageFormatter.safe_truncate_markdown(None), "")
        self.assertEqual(FeishuMessageFormatter.safe_truncate_markdown(""), "")

    def test_within_and_exact_length(self):
        self.assertEqual(FeishuMessageFormatter.safe_truncate_markdown("hello world", max_length=100), "hello world")
        text = "a" * 100
        self.assertEqual(FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100), text)

    def test_keep_head_with_fence_handling(self):
        # Even fence count
        text = "abc```def```" + "x" * 200
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100, keep_head=True)
        self.assertIn("⚠️ 内容过长", result)
        self.assertTrue(result.startswith("abc"))
        self.assertEqual(result.count("```") % 2, 0)

        # Odd fence count
        text2 = "abc```def" + "x" * 200
        result2 = FeishuMessageFormatter.safe_truncate_markdown(text2, max_length=100, keep_head=True)
        self.assertIn("⚠️ 内容过长", result2)
        fence_count = result2.split("⚠️")[0].count("```")
        self.assertEqual(fence_count % 2, 0)

    def test_keep_tail_with_fence_handling(self):
        # Even fence
        text = "x" * 200 + "```abc```def"
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=100, keep_head=False)
        self.assertIn("⚠️ 内容过长", result)
        self.assertTrue(result.startswith("\n\n> ⚠️"))

        # Odd fence
        text2 = "x" * 200 + "```tail_content"
        result2 = FeishuMessageFormatter.safe_truncate_markdown(text2, max_length=100, keep_head=False)
        self.assertIn("⚠️ 内容过长", result2)
        parts_after = result2.split("⚠️ 内容过长（超过 100 字符），已自动截断。\n\n")[1]
        self.assertTrue(parts_after.startswith("```\n"))

    def test_truncated_content_reasonable_length(self):
        text = "a" * 300
        result = FeishuMessageFormatter.safe_truncate_markdown(text, max_length=200, keep_head=True)
        self.assertLessEqual(len(result), 300)
        self.assertIn("⚠️ 内容过长", result)


class TestIsPostFormat(unittest.TestCase):

    def test_valid_and_invalid_formats(self):
        self.assertTrue(FeishuMessageFormatter.is_post_format(("post", "data")))
        self.assertFalse(FeishuMessageFormatter.is_post_format(("text", "data")))
        self.assertFalse(FeishuMessageFormatter.is_post_format("string"))
        self.assertFalse(FeishuMessageFormatter.is_post_format(("post",)))
        self.assertFalse(FeishuMessageFormatter.is_post_format(None))
        self.assertFalse(FeishuMessageFormatter.is_post_format(("post", "a", "b")))
        self.assertFalse(FeishuMessageFormatter.is_post_format(["post", "data"]))
        self.assertFalse(FeishuMessageFormatter.is_post_format(42))


class TestFormatSafetyBlock(unittest.TestCase):

    def test_format_safety_block(self):
        msg_type, data = FeishuMessageFormatter.format_safety_block("sudo reboot", "needs approval")
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🚫 命令被安全检查拦截")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertIn("`sudo reboot`", md)
        self.assertIn("needs approval", md)
        self.assertIn("如需执行此命令，请联系管理员", md)


class TestFormatHelp(unittest.TestCase):

    def test_smart_and_coco_mode(self):
        msg_type, data = FeishuMessageFormatter.format_help("/home", is_coco_mode=False)
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "💡 使用说明")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertIn("`/home`", md)
        self.assertIn("Shell 模式", md)
        self.assertIn("Coco 模式", md)

        _, data2 = FeishuMessageFormatter.format_help("/home", is_coco_mode=True)
        parsed2 = json.loads(data2)
        self.assertEqual(parsed2["zh_cn"]["title"], "🤖 当前在 Coco 模式")
        md2 = parsed2["zh_cn"]["content"][0][0]["text"]
        self.assertIn("/end_coco", md2)
        self.assertNotIn("/home", md2)


class TestFormatCocoEnter(unittest.TestCase):

    def test_format_coco_enter(self):
        msg_type, data = FeishuMessageFormatter.format_coco_enter()
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🤖 已进入 Coco 模式")
        md = parsed["zh_cn"]["content"][0][0]["text"]
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

    def test_success_and_failure(self):
        self.assertEqual(FeishuMessageFormatter.format_dir_change("/new/path", success=True), "📂 **已切换到**: `/new/path`")
        self.assertEqual(FeishuMessageFormatter.format_dir_change("/bad/path", success=False), "❌ **切换失败**: /bad/path")


class TestFormatCurrentDir(unittest.TestCase):

    def test_format(self):
        result = FeishuMessageFormatter.format_current_dir("/home/user")
        self.assertEqual(result, "📂 **当前工作目录**: `/home/user`")


class TestFormatMultiTaskPlan(unittest.TestCase):

    def test_format_with_tasks(self):
        tasks = [{"description": "a"}, {"description": "b"}, {"description": "c"}]
        msg_type, data = FeishuMessageFormatter.format_multi_task_plan(tasks)
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "📋 执行计划")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertIn("1. a", md)
        self.assertIn("2. b", md)
        self.assertIn("3. c", md)
        self.assertIn("🚀 **开始执行**...", md)

    def test_edge_cases(self):
        # Missing description key
        _, data = FeishuMessageFormatter.format_multi_task_plan([{"command": "ls"}])
        md = json.loads(data)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("未知操作", md)

        # Empty tasks
        _, data2 = FeishuMessageFormatter.format_multi_task_plan([])
        md2 = json.loads(data2)["zh_cn"]["content"][0][0]["text"]
        self.assertIn("🚀 **开始执行**...", md2)


class TestFormatUnknownIntent(unittest.TestCase):

    def test_format_unknown_intent(self):
        msg_type, data = FeishuMessageFormatter.format_unknown_intent()
        self.assertEqual(msg_type, "post")
        parsed = json.loads(data)
        self.assertEqual(parsed["zh_cn"]["title"], "🤔 无法理解你的意图")
        md = parsed["zh_cn"]["content"][0][0]["text"]
        self.assertIn("shell 命令", md)
        self.assertIn("Coco 模式", md)


class TestFormatErrorAndWarning(unittest.TestCase):

    def test_error_and_warning(self):
        self.assertEqual(FeishuMessageFormatter.format_error("something broke"), "❌ **错误**: something broke")
        self.assertEqual(FeishuMessageFormatter.format_warning("be careful"), "⚠️ **警告**: be careful")


class TestHelperMethods(unittest.TestCase):

    def test_text_and_link(self):
        self.assertEqual(FeishuMessageFormatter._text("hello"), {"tag": "text", "text": "hello"})
        self.assertEqual(FeishuMessageFormatter._text("bold", styles=["bold"]), {"tag": "text", "text": "bold", "style": ["bold"]})
        self.assertEqual(FeishuMessageFormatter._link("click", "https://example.com"), {"tag": "a", "text": "click", "href": "https://example.com"})

    def test_code_block_and_md_and_hr(self):
        result = FeishuMessageFormatter._code_block("echo hi")
        self.assertEqual(result[0]["tag"], "code_block")
        self.assertEqual(result[0]["language"], "BASH")
        self.assertEqual(result[0]["text"], "echo hi")
        result2 = FeishuMessageFormatter._code_block("print(1)", language="PYTHON")
        self.assertEqual(result2[0]["language"], "PYTHON")
        self.assertEqual(FeishuMessageFormatter._md("**bold**"), [{"tag": "md", "text": "**bold**"}])
        self.assertEqual(FeishuMessageFormatter._hr(), [{"tag": "hr"}])


if __name__ == "__main__":
    unittest.main()
