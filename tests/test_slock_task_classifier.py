"""Comprehensive tests for TaskClassifier.is_chitchat / is_task logic."""

from __future__ import annotations

import pytest

from src.slock_engine.task_classifier import TaskClassifier

# ---------------------------------------------------------------------------
# Rule 1: Empty / whitespace -> chitchat
# ---------------------------------------------------------------------------


class TestEmptyAndWhitespace:
    @pytest.mark.parametrize("text", ["", " ", "  ", "\t", "\n", " \t\n "])
    def test_empty_or_whitespace_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True
        assert TaskClassifier.is_task(text) is False


# ---------------------------------------------------------------------------
# Rule 2: Explicit greeting patterns -> chitchat
# ---------------------------------------------------------------------------


class TestChineseGreetings:
    @pytest.mark.parametrize(
        "text",
        ["你好", "早上好", "晚安", "早安", "下午好", "晚上好", "嗨"],
    )
    def test_chinese_greetings_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize(
        "text",
        ["你好!", "早上好！", "晚安。", "早安~"],
    )
    def test_chinese_greetings_with_trailing_punct(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


class TestEnglishGreetings:
    @pytest.mark.parametrize("text", ["hi", "hello", "hey", "yo"])
    def test_english_greetings_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["Hi", "HELLO", "Hey", "YO"])
    def test_english_greetings_case_insensitive(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["hi!", "hello.", "hey~", "yo!"])
    def test_english_greetings_with_trailing_punct(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


# ---------------------------------------------------------------------------
# Rule 2: Acknowledgments -> chitchat
# ---------------------------------------------------------------------------


class TestAcknowledgments:
    @pytest.mark.parametrize(
        "text",
        ["ok", "好的", "收到", "收到了", "了解", "明白", "知道了", "嗯", "对"],
    )
    def test_acknowledgments_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize(
        "text",
        ["thanks", "thank you", "thx", "谢谢", "alright", "okok"],
    )
    def test_thank_you_variants_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["ok!", "好的。", "收到！", "了解~"])
    def test_acknowledgments_with_trailing_punct(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["OK", "Ok", "THANKS", "THX"])
    def test_acknowledgments_case_insensitive(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


# ---------------------------------------------------------------------------
# Rule 2: Reactions -> chitchat
# ---------------------------------------------------------------------------


class TestReactions:
    @pytest.mark.parametrize(
        "text",
        ["哈哈", "嘿嘿", "呵呵", "lol", "haha", "666", "nb", "nice", "cool"],
    )
    def test_reactions_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["👍", "😂", "🙏"])
    def test_emoji_reactions_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["yes", "no", "got it", "roger", "ack"])
    def test_english_reactions_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["LOL", "HAHA", "NB", "NICE", "COOL"])
    def test_reactions_case_insensitive(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


# ---------------------------------------------------------------------------
# Rule 3: CJK chitchat keywords <= 6 chars -> chitchat
# ---------------------------------------------------------------------------


class TestCJKChitchatKeywords:
    @pytest.mark.parametrize(
        "text",
        [
            "你好啊",
            "谢谢啦",
            "好的呢",
            "收到啦",
            "了解了",
            "明白了",
            "知道啦",
            "没事",
            "没问题",
            "可以的",
            "行的",
            "好吧",
            "好哒",
            "嗯嗯",
            "对对",
            "是的",
            "好滴",
            "ok啦",
            "谢啦",
            "早啊",
            "晚安啊",
            "你好呀",
            "好嘞",
        ],
    )
    def test_cjk_chitchat_keywords_are_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["你好啊!", "谢谢啦。", "好的呢~"])
    def test_cjk_chitchat_keywords_with_trailing_punct(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


# ---------------------------------------------------------------------------
# Rule 4: Valid CJK tasks (CJK + length >= 2) -> NOT chitchat
# ---------------------------------------------------------------------------


class TestValidCJKTasks:
    @pytest.mark.parametrize(
        "text",
        ["修bug", "写测试", "部署服务", "加日志", "查日志"],
    )
    def test_cjk_dev_commands_are_tasks(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "帮我写一个排序算法",
            "修复登录页面的bug",
            "部署到生产环境",
            "优化数据库查询",
            "添加单元测试",
        ],
    )
    def test_longer_cjk_messages_are_tasks(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True

    @pytest.mark.parametrize(
        "text",
        ["改代码", "跑CI", "上线", "发版"],
    )
    def test_short_cjk_task_commands_are_tasks(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True


# ---------------------------------------------------------------------------
# Rule 5: Pure punctuation / emoji -> chitchat
# ---------------------------------------------------------------------------


class TestPurePunctuationAndEmoji:
    @pytest.mark.parametrize("text", ["!!!", "???", "...", "!?!", "~"])
    def test_pure_punctuation_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["👍👍", "😂😂😂", "🙏🙏", "🎉"])
    def test_pure_emoji_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["👍!!!", "...🙏", "?!😂"])
    def test_mixed_punct_and_emoji_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


# ---------------------------------------------------------------------------
# Rule 6: Dev term whitelist -> NOT chitchat
# ---------------------------------------------------------------------------


class TestDevTermWhitelist:
    @pytest.mark.parametrize(
        "text",
        [
            "fix",
            "bug",
            "wip",
            "test",
            "deploy",
            "run",
            "build",
            "lint",
            "push",
            "pull",
            "sync",
            "ship",
            "dev",
            "ci",
            "cd",
            "doc",
        ],
    )
    def test_dev_terms_are_tasks(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True

    @pytest.mark.parametrize("text", ["FIX", "Bug", "WIP", "TEST", "Deploy"])
    def test_dev_terms_case_insensitive(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True


# ---------------------------------------------------------------------------
# Rule 7: Short non-CJK (<=3 chars) without dev term -> chitchat
# ---------------------------------------------------------------------------


class TestShortNonCJK:
    @pytest.mark.parametrize("text", ["k", "y", "n", "1", "a", "x"])
    def test_single_char_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["ab", "xx", "mm", "99"])
    def test_two_char_non_dev_term_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True

    @pytest.mark.parametrize("text", ["abc", "xyz", "wat", "sup"])
    def test_three_char_non_dev_term_is_chitchat(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is True


# ---------------------------------------------------------------------------
# Rule 8: Longer valid messages -> NOT chitchat
# ---------------------------------------------------------------------------


class TestValidLongerMessages:
    @pytest.mark.parametrize(
        "text",
        [
            "please fix the login page",
            "add error handling to the API endpoint",
            "refactor the database connection pool",
            "update the dependencies",
            "review my pull request",
        ],
    )
    def test_longer_english_messages_are_tasks(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "帮我写一个排序算法",
            "检查一下这个接口的返回值",
            "把这个功能重构一下",
        ],
    )
    def test_longer_chinese_messages_are_tasks(self, text: str) -> None:
        assert TaskClassifier.is_chitchat(text) is False
        assert TaskClassifier.is_task(text) is True


# ---------------------------------------------------------------------------
# is_task is the inverse of is_chitchat
# ---------------------------------------------------------------------------


class TestIsTaskInverse:
    @pytest.mark.parametrize(
        "text,expected_chitchat",
        [
            ("", True),
            ("hi", True),
            ("fix", False),
            ("修bug", False),
            ("你好", True),
            ("帮我部署", False),
            ("!!!", True),
            ("please fix this", False),
        ],
    )
    def test_is_task_is_inverse_of_is_chitchat(
        self, text: str, expected_chitchat: bool
    ) -> None:
        assert TaskClassifier.is_chitchat(text) is expected_chitchat
        assert TaskClassifier.is_task(text) is (not expected_chitchat)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_whitespace_around_greeting_is_chitchat(self) -> None:
        # strip() is applied, so leading/trailing whitespace should not matter
        assert TaskClassifier.is_chitchat("  hi  ") is True
        assert TaskClassifier.is_chitchat("  你好  ") is True

    def test_whitespace_around_task_is_still_task(self) -> None:
        assert TaskClassifier.is_chitchat("  fix  ") is False
        assert TaskClassifier.is_chitchat("  修bug  ") is False

    def test_got_it_with_space(self) -> None:
        assert TaskClassifier.is_chitchat("got it") is True

    def test_thank_you_with_space(self) -> None:
        assert TaskClassifier.is_chitchat("thank you") is True

    def test_single_cjk_char_greeting(self) -> None:
        # Single CJK char "嗯" matches explicit pattern
        assert TaskClassifier.is_chitchat("嗯") is True
        # Single CJK char "对" matches explicit pattern
        assert TaskClassifier.is_chitchat("对") is True
