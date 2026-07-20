"""Behavioral equivalence classes for the Slock task classifier."""

from __future__ import annotations

from src.slock_engine.task_classifier import TaskClassifier


def _assert_classification(cases: tuple[str, ...], *, chitchat: bool) -> None:
    for text in cases:
        assert TaskClassifier.is_chitchat(text) is chitchat, text
        assert TaskClassifier.is_task(text) is (not chitchat), text


def test_representative_chitchat_equivalence_classes() -> None:
    _assert_classification(
        (
            "",
            " \t\n ",
            "hi",
            "HELLO!",
            "  你好  ",
            "谢谢",
            "THANKS",
            "got it",
            "哈哈",
            "LOL",
            "👍",
            "你好啊!",
            "今天天气不错",
            "!!!",
            "🎉",
            "x",
            "ab",
        ),
        chitchat=True,
    )


def test_representative_task_equivalence_classes() -> None:
    _assert_classification(
        (
            "fix",
            "Deploy",
            "ci",
            "  修bug  ",
            "写测试",
            "帮我写一个排序算法",
            "please fix the login page",
            "refactor the database connection pool",
        ),
        chitchat=False,
    )
