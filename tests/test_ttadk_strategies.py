import json
import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from src.ttadk.strategies import (
    InteractiveStrategy,
    OfficialCLIModelsStrategy,
    ProbeStrategy,
    TTADKOfficialCLIError,
    TTADKProbeError,
)


class TestModelTokenHeuristics(unittest.TestCase):
    def test_is_model_token_filters_pure_numeric_like_tokens(self):
        """回归：官方 list 输出可能包含日期/版本号，不能误判为模型 id。"""
        from src.ttadk.models import is_model_token

        assert is_model_token("2026-03-06") is False
        assert is_model_token("1.2.3") is False
        assert is_model_token("gpt-5.2") is True
        assert is_model_token("gpt-4") is True


class TestModelsParserSSOT(unittest.TestCase):
    def test_parse_models_from_invalid_model_output_to_models(self):
        from src.ttadk.models import parse_ttadk_models_from_output_to_models

        payload = "✗ Error: Invalid model 'X'. Available models: gpt-5.2-codex-ttadk, gpt-5.2-ttadk"
        ms = parse_ttadk_models_from_output_to_models(payload)
        self.assertEqual([m.name for m in ms], ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"])

    def test_parse_models_from_json_output_to_models_preserves_friendly(self):
        from src.ttadk.models import parse_ttadk_models_from_output_to_models

        payload = json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-5.2-codex-ttadk",
                        "display_name": "GPT 5.2 Codex (Recommended)",
                    }
                ]
            }
        )
        ms = parse_ttadk_models_from_output_to_models(payload)
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].name, "gpt-5.2-codex-ttadk")
        self.assertEqual(ms[0].friendly_name, "GPT 5.2 Codex (Recommended)")

    def test_parse_models_from_text_output_to_models(self):
        from src.ttadk.models import parse_ttadk_models_from_output_to_models

        payload = "Available models:\n- gpt-5.2-codex-ttadk\n- gpt-5.2-ttadk\n"
        ms = parse_ttadk_models_from_output_to_models(payload)
        self.assertEqual([m.name for m in ms], ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"])


class TestNoParserForkInStrategy(unittest.TestCase):
    def test_strategy_has_no_legacy_parsers(self):
        """防回归：策略层不得再出现 payload->models 的解析分叉实现。"""
        from src.ttadk.strategies import TTADKModelsListStrategy

        assert not hasattr(TTADKModelsListStrategy, "_parse_models_from_json")
        assert not hasattr(TTADKModelsListStrategy, "_parse_models_from_text")


class TestTTADKEnvSandboxInjection(unittest.TestCase):
    def test_probe_strategy_subprocess_run_injects_sandbox_env(self):
        """回归：ProbeStrategy 非 PTY 路径也必须注入隔离 env（避免写入真实 ~/.ttadk）。"""
        from src.ttadk.strategies import ProbeStrategy

        captured: dict = {}

        def _fake_run(args, capture_output, text, timeout, cwd, env=None, **kwargs):
            captured["env"] = dict(env or {})
            p = MagicMock()
            p.returncode = 1
            p.stdout = ""
            p.stderr = "✗ Error: Invalid model 'X'. Available models: gpt-5.2-ttadk"
            return p

        with patch("subprocess.run", side_effect=_fake_run):
            s = ProbeStrategy(runner=None, timeout_s=0.1)
            _ = s.fetch("coco", cwd="/tmp")

        env = captured.get("env") or {}
        assert env.get("HOME"), "expected HOME to be set in sandbox env"
        assert "XDG_CONFIG_HOME" in env
        assert env.get("CLAUDECODE") is None

    def test_official_cli_models_strategy_subprocess_run_injects_sandbox_env(self):
        """回归：OfficialCLIModelsStrategy 必须注入隔离 env。"""
        from src.ttadk.strategies import OfficialCLIModelsStrategy

        calls: list[dict] = []

        def _fake_run(args, capture_output, text, timeout, cwd, env=None, **kwargs):
            calls.append({"args": list(args), "env": dict(env or {}), "cwd": cwd})
            p = MagicMock()
            p.returncode = 0
            p.stdout = "Usage: ttadk models ..."
            p.stderr = ""
            return p

        with patch("subprocess.run", side_effect=_fake_run):
            s = OfficialCLIModelsStrategy(runner=None, timeout_s=0.1)
            # 仅触发 help probe 即可验证 env 注入
            try:
                _ = s.fetch("codex")
            except Exception:
                # 某些路径下可能继续尝试 list 并解析失败；本测试只关心 env 注入
                pass

        assert calls, "expected subprocess.run to be called"
        env = calls[0]["env"]
        assert env.get("HOME")
        assert "XDG_CONFIG_HOME" in env

    def test_sandbox_env_can_be_disabled_by_settings(self):
        """回归：关闭开关后不应覆盖 HOME/XDG_CONFIG_HOME。"""
        from src.ttadk.env_sandbox import build_ttadk_subprocess_env

        base_env = {"HOME": "/real_home", "XDG_CONFIG_HOME": "/real_xdg"}
        fake_settings = type(
            "S",
            (),
            {
                "ttadk_sandbox_home_enabled": False,
                "ttadk_sandbox_home_root": "",
                "ttadk_sandbox_cover_cache_home": False,
            },
        )()
        env, root = build_ttadk_subprocess_env(cwd="/tmp", base_env=base_env, get_settings_fn=lambda: fake_settings)
        assert root == ""
        assert env.get("HOME") == "/real_home"
        assert env.get("XDG_CONFIG_HOME") == "/real_xdg"


class TestProbeStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = ProbeStrategy()

    @patch("subprocess.run")
    def test_fetch_success(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout = ""
        mock_process.stderr = "✗ Error: Invalid model 'INVALID_PROBE'. Available models: gpt-4, gpt-3.5-turbo"
        mock_run.return_value = mock_process

        models = self.strategy.fetch("codex")
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0].name, "gpt-4")
        self.assertEqual(models[1].name, "gpt-3.5-turbo")

    @patch("subprocess.run")
    def test_fetch_failure(self, mock_run):
        """非 Invalid model 输出应抛出可诊断异常（由 fetcher 记录），避免静默返回空列表。"""
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr = "Some other error"
        mock_run.return_value = mock_process

        with self.assertRaises(TTADKProbeError):
            self.strategy.fetch("codex")

    @patch("subprocess.run")
    def test_fetch_invalid_model_but_available_models_empty_raises(self, mock_run):
        """命中 Invalid model 但 Available models 为空时，应抛出可诊断异常供上层记录 snippet。"""
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout = "banner"
        mock_process.stderr = "✗ Error: Invalid model 'INVALID_PROBE'. Available models: "
        mock_run.return_value = mock_process

        with self.assertRaises(TTADKProbeError) as ctx:
            self.strategy.fetch("coco")
        e = ctx.exception
        self.assertEqual(getattr(e, "returncode", None), 1)
        self.assertIn("Invalid model", getattr(e, "stderr", ""))


class TestInteractiveStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = InteractiveStrategy()
        # InteractiveStrategy 默认禁用；单测需要覆盖交互流程时显式开启
        self._settings_patcher = patch(
            "src.config.get_settings",
            return_value=type("S", (), {"ttadk_interactive_enabled": True})(),
        )
        self._settings_patcher.start()

        # Mock sys.stdin.isatty to True for all these tests
        self._isatty_patcher = patch("sys.stdin.isatty", return_value=True)
        self._isatty_patcher.start()

    def tearDown(self):
        try:
            self._settings_patcher.stop()
            self._isatty_patcher.stop()
        except Exception:
            pass

    def test_strip_ansi(self):
        from src.ttadk.models import strip_ansi

        text = "\x1b[31mRed\x1b[0m Text"
        self.assertEqual(strip_ansi(text), "Red Text")

    def test_parse_model_selection_menu(self):
        output = """? Select a model:
  \x1b[36m❯\x1b[39m \x1b[36mGPT 5.2\x1b[39m
    GPT 4.1
"""
        names = self.strategy._parse_model_selection_menu(output)
        self.assertEqual(names, ["GPT 5.2", "GPT 4.1"])

    def test_extract_real_model_name(self):
        output = """
        Model Details
        model: gpt-5.2-codex
        """
        name = self.strategy._extract_real_model_name(output)
        self.assertEqual(name, "gpt-5.2-codex")

    @patch("pty.openpty")
    @patch("fcntl.ioctl")
    @patch("subprocess.Popen")
    @patch("os.close")
    @patch("select.select")
    @patch("os.read")
    @patch("os.write")
    def test_fetch_max_models_limit(
        self,
        mock_write,
        mock_read,
        mock_select,
        mock_close,
        mock_ioctl,
        mock_openpty,
        mock_popen,
    ):
        """当菜单模型数量过多时，应按 TTADK_INTERACTIVE_MAX_MODELS 截断，避免 O(n) 过慢。"""
        mock_openpty.return_value = (1, 2)
        p = MagicMock()
        p.pid = 12345
        p.wait.return_value = 0
        mock_popen.return_value = p

        # 让 select 始终可读
        mock_select.return_value = ([1], [], [])

        # 第一次读：返回菜单（包含 30 个模型）
        menu = "? Select a model:\n" + "\n".join([f"  M{i}" for i in range(30)]) + "\n"
        # 后续读：每次进入详情页读到 model: 即返回
        payloads = [menu.encode("utf-8")] + [b"model: real-x\n"] * 50
        mock_read.side_effect = payloads

        old = os.environ.get("TTADK_INTERACTIVE_MAX_MODELS")
        os.environ["TTADK_INTERACTIVE_MAX_MODELS"] = "3"
        try:
            models = self.strategy.fetch("codex")
            self.assertLessEqual(len(models), 3)
        finally:
            if old is None:
                os.environ.pop("TTADK_INTERACTIVE_MAX_MODELS", None)
            else:
                os.environ["TTADK_INTERACTIVE_MAX_MODELS"] = old

    @patch("pty.openpty")
    @patch("fcntl.ioctl")
    @patch("subprocess.Popen")
    @patch("os.killpg")
    @patch("os.close")
    @patch("select.select")
    @patch("os.read")
    @patch("os.write")
    def test_fetch_process_cleanup(
        self, mock_write, mock_read, mock_select, mock_close, mock_killpg, mock_popen, mock_ioctl, mock_openpty
    ):
        # Setup mocks
        mock_openpty.return_value = (1, 2)  # master, slave

        p = MagicMock()
        p.pid = 12345
        p.wait.return_value = 0
        mock_popen.return_value = p

        # Mock interaction:
        # 1. Prompt
        # 2. Model details

        # We need to simulate the read loop returning data
        # First call to read returns prompt
        # Second call returns model details
        # Third call returns empty to stop

        # Mock select to return ready
        mock_select.return_value = ([1], [], [])

        # Mock os.read side effects
        # We need careful orchestration here because fetch calls read multiple times
        # 1. _read_until_prompt -> reads prompt_output
        # 2. _select_and_extract -> _read_until_model_display -> reads details_output

        # Let's simplify and make it raise exception to trigger finally block immediately
        # to verify cleanup logic specifically
        mock_select.side_effect = Exception("Force cleanup")

        # Run fetch
        self.strategy.fetch("codex")

        # Verify cleanup path attempted process group terminate
        assert mock_killpg.called

    @patch("pty.openpty")
    @patch("fcntl.ioctl")
    @patch("subprocess.Popen")
    @patch("os.killpg")
    @patch("os.close")
    @patch("select.select")
    @patch("os.read")
    @patch("os.write")
    def test_fetch_process_force_kill(
        self, mock_write, mock_read, mock_select, mock_close, mock_killpg, mock_popen, mock_ioctl, mock_openpty
    ):
        # Setup mocks
        mock_openpty.return_value = (1, 2)

        p = MagicMock()
        p.pid = 12345

        # Simulate wait timeout to trigger kill
        def _wait(timeout=None):
            raise TimeoutError("timeout")

        p.wait.side_effect = _wait
        mock_popen.return_value = p

        mock_select.side_effect = Exception("Force cleanup")

        # Run fetch
        self.strategy.fetch("codex")

        # Verify terminate/kill called
        assert p.terminate.called
        assert p.kill.called
        assert mock_killpg.called


class TestOfficialCLIModelsStrategy(unittest.TestCase):
    def test_probe_failed_raises_diagnostics_error(self):
        """probe 不支持时不应静默返回空列表，应抛出可诊断异常供 fetcher 记录。"""

        def runner(args, cwd, timeout):
            # 任意 help 都失败
            return (2, "", "unknown command")

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        with self.assertRaises(TTADKOfficialCLIError) as ctx:
            s.fetch("codex")
        e = ctx.exception
        self.assertIn("official_cli_probe_failed", str(e))
        self.assertIsNotNone(getattr(e, "returncode", None))

    def test_list_json_success(self):
        """命中 JSON 输出时应正确解析并去重。"""
        calls = []

        def runner(args, cwd, timeout):
            calls.append(list(args))
            # probe: ttadk models --help
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            # list: 返回 JSON
            if args[:3] == ["ttadk", "models", "list"] and "-f" in args:
                return (0, json.dumps({"models": ["gpt-5.2-codex-ttadk", "gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"]}), "")
            return (1, "", "bad")

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        models = s.fetch("codex")
        self.assertEqual([m.name for m in models], ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"])

    def test_list_json_preserves_friendly_name_when_present(self):
        """官方 JSON 若包含 display/friendly 字段，应保留到 TTADKModel.friendly_name。"""

        def runner(args, cwd, timeout):
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            if args[:3] == ["ttadk", "models", "list"] and "-f" in args:
                return (
                    0,
                    json.dumps(
                        {
                            "models": [
                                {
                                    "id": "gpt-5.2-codex-ttadk",
                                    "display_name": "GPT 5.2 Codex (Recommended)",
                                }
                            ]
                        }
                    ),
                    "",
                )
            return (1, "", "bad")

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        models = s.fetch("codex")
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "gpt-5.2-codex-ttadk")
        self.assertEqual(models[0].friendly_name, "GPT 5.2 Codex (Recommended)")

    def test_strategy_json_matches_ssot_parser(self):
        """回归：strategy 层必须复用 SSOT 解析器，输出应一致。"""
        from src.ttadk.models import parse_ttadk_models_from_output_to_models

        def runner(args, cwd, timeout):
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            if args[:3] == ["ttadk", "models", "list"] and "-f" in args:
                return (
                    0,
                    json.dumps(
                        {
                            "models": [
                                {
                                    "id": "gpt-5.2-codex-ttadk",
                                    "display_name": "GPT 5.2 Codex (Recommended)",
                                }
                            ]
                        }
                    ),
                    "",
                )
            return (1, "", "bad")

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        strategy_models = s.fetch("codex")
        ssot_models = parse_ttadk_models_from_output_to_models(
            json.dumps(
                {
                    "models": [
                        {
                            "id": "gpt-5.2-codex-ttadk",
                            "display_name": "GPT 5.2 Codex (Recommended)",
                        }
                    ]
                }
            )
        )

        self.assertEqual([m.name for m in strategy_models], [m.name for m in ssot_models])
        self.assertEqual(strategy_models[0].friendly_name, ssot_models[0].friendly_name)

    def test_list_text_success(self):
        """JSON 不可用时应降级文本解析。"""

        def runner(args, cwd, timeout):
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            # json 版本失败
            if args[:3] == ["ttadk", "models", "list"] and "-f" in args:
                return (0, "not json", "")
            # 文本版本成功
            if args[:3] == ["ttadk", "models", "list"] and "-f" not in args:
                return (0, "Available models:\n- gpt-5.2-codex-ttadk\n- gpt-5.2-ttadk\n", "")
            return (1, "", "")

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        models = s.fetch("codex")
        self.assertEqual([m.name for m in models], ["gpt-5.2-codex-ttadk", "gpt-5.2-ttadk"])

    def test_list_prefers_probed_subcmd(self):
        """probe 命中 models 时，应优先走 models 分支，避免无谓尝试 model 分支。"""
        calls = []

        def runner(args, cwd, timeout):
            calls.append(list(args))
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            if args[:6] == ["ttadk", "models", "list", "-t", "codex", "-f"]:
                return (0, '{"models": ["gpt-5.2-codex-ttadk"]}', "")
            # If we ever try `ttadk model ...`, fail
            if len(args) >= 2 and args[1] == "model":
                return (2, "", "should-not-call-model-subcmd")
            return (1, "", "bad")

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        models = s.fetch("codex")
        self.assertEqual([m.name for m in models], ["gpt-5.2-codex-ttadk"])
        assert all((cmd[1] != "model") for cmd in calls if cmd and cmd[0] == "ttadk")

    def test_unstable_raises_diagnostics_error(self):
        """全部超时/异常时也应抛出可诊断异常，而不是静默空列表。"""

        def runner(args, cwd, timeout):
            if args[:3] == ["ttadk", "models", "--help"]:
                return (0, "Usage: ttadk models ...", "")
            raise subprocess.TimeoutExpired(args=args, timeout=timeout)

        s = OfficialCLIModelsStrategy(runner=runner, timeout_s=0.2)
        with self.assertRaises(TTADKOfficialCLIError) as ctx:
            s.fetch("codex")
        self.assertIn("official_cli_unstable", str(ctx.exception))


class TestModelFetcherDiagnostics(unittest.TestCase):
    def test_fetcher_records_fail_reason_from_exception_prefix(self):
        """fetcher: should record fail_reason derived from exception message prefix."""

        from src.ttadk.model_fetcher import TTADKModelFetcher

        class _FailStrategy:
            name = "official_cli"
            timeout_s = 0.1

            def fetch(self, tool_name: str, cwd=None):
                raise TTADKOfficialCLIError(
                    "official_cli_probe_failed: tool=codex",
                    returncode=2,
                    stdout="",
                    stderr="nope",
                )

        f = TTADKModelFetcher(runner=None)
        # 覆盖策略链为单策略，避免环境差异
        f._strategies = [_FailStrategy()]
        r = f.fetch_tool_models_with_diagnostics("codex", cwd="/tmp", force_refresh=False)
        self.assertEqual(len(r.diagnostics.attempts), 1)
        a = r.diagnostics.attempts[0]
        self.assertEqual(a.get("strategy"), "official_cli")
        self.assertEqual(a.get("fail_reason"), "official_cli_probe_failed")


class TestInteractiveStrategyEnv(unittest.TestCase):
    def test_non_tty_skips(self):
        """Test that InteractiveStrategy skips execution when not in TTY environment."""
        strategy = InteractiveStrategy()

        # Mock sys.stdin.isatty to return False
        with patch("sys.stdin.isatty", return_value=False):
            # Ensure no environment variables force it
            with patch.dict(os.environ, {}, clear=True):
                result = strategy.fetch("test_tool")
                self.assertEqual(result, [])

    def test_ci_env_skips(self):
        """Test that InteractiveStrategy skips in CI environment even if TTY says yes (some CIs mimic TTY)."""
        strategy = InteractiveStrategy()

        # Mock sys.stdin.isatty to return True (simulating a CI runner with PTY)
        with patch("sys.stdin.isatty", return_value=True):
            # Set CI env var
            with patch.dict(os.environ, {"CI": "true"}):
                result = strategy.fetch("test_tool")
                self.assertEqual(result, [])

    def test_debian_frontend_skips(self):
        """Test that InteractiveStrategy skips when DEBIAN_FRONTEND=noninteractive."""
        strategy = InteractiveStrategy()

        with patch("sys.stdin.isatty", return_value=True):
            with patch.dict(os.environ, {"DEBIAN_FRONTEND": "noninteractive"}):
                result = strategy.fetch("test_tool")
                self.assertEqual(result, [])

    def test_force_interactive_env(self):
        """Test that TTADK_FORCE_INTERACTIVE overrides checks."""
        strategy = InteractiveStrategy()

        # Must enable the feature flag first
        with patch("src.config.get_settings", return_value=type("S", (), {"ttadk_interactive_enabled": True})()):
            # Should run even if not TTY if forced
            with patch("sys.stdin.isatty", return_value=False):
                with patch.dict(os.environ, {"TTADK_FORCE_INTERACTIVE": "1"}):
                    # Mock _run_interactive to avoid actual execution
                    # But fetch calls openpty etc directly.
                    # We need to mock pty.openpty to check if it proceeds past the check
                    with patch("pty.openpty") as mock_pty:
                        mock_pty.return_value = (1, 2)  # master, slave fds
                        with patch("os.close"), patch("fcntl.ioctl"), patch("subprocess.Popen"), \
                             patch.object(InteractiveStrategy, "_read_until_prompt", return_value=""):
                            # Just ensure it tries to run (mocking enough to not crash)
                            try:
                                strategy.fetch("test_tool")
                            except Exception:
                                # It might fail later in the function, but we just want to know
                                # if it passed the early return check.
                                # If it returns [] immediately, mock_pty won't be called.
                                pass

                            mock_pty.assert_called()

    def test_cli_capabilities_probe_records_attempt_and_dedupes_concurrency(self):
        """回归：ttadk CLI 能力探测应写入 diagnostics.attempts，并支持并发去重（最多执行一次 run_simple）。"""
        import threading

        from src.ttadk.model_fetcher import TTADKModelFetcher

        calls = {"n": 0}

        class _Runner:
            def run_simple(self, args, cwd, timeout):
                calls["n"] += 1
                assert args[:2] == ["ttadk", "--help"]
                # Ensure first worker holds inflight long enough for the second to enter
                import time as _time

                _time.sleep(0.08)
                return (0, "Commands:\n  models\n  sync\nVersion 0.3.8\n", "")

        f = TTADKModelFetcher(runner=_Runner())

        # Force `_is_official_cli_enabled` to fall back to `ttadk --help` without
        # triggering additional subprocess calls from official_cli._probe.
        class _Boom:
            def _probe(self, *a, **k):
                raise RuntimeError("boom")

        f._official_cli = _Boom()

        # A fake strategy that forces _maybe_add_official_cli() to call capabilities probe
        class _OnlyOfficial:
            name = "official_cli"

            def fetch(self, tool_name: str, cwd=None):
                return []

        # Add a core strategy name to avoid `_select_strategies` short-circuiting
        # when only official_cli exists.
        class _DummyProbe:
            name = "probe"

            def fetch(self, tool_name: str, cwd=None):
                return []

        f._strategies = [_OnlyOfficial(), _DummyProbe()]

        # Run two concurrent fetches to exercise inflight de-dupe.
        results = []

        def _worker():
            r = f.fetch_tool_models_with_diagnostics("codex", cwd="/tmp", force_refresh=True)
            results.append(r)

        ts = [threading.Thread(target=_worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert results
        # dedupe: at most 1 actual ttadk --help call
        assert calls["n"] <= 1

        # diagnostics must contain capability attempt
        d = results[0].diagnostics
        assert any(a.get("strategy") == "ttadk_cli_capabilities" for a in (d.attempts or []))
