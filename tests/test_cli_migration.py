import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from src.agent_session import SyncTTADKCLISession, create_engine_session


class TestCLISession(unittest.TestCase):
    @patch("src.ttadk.startup_common.precheck_ttadk_startup_model")
    def test_factory_returns_cli_session(self, mock_precheck):
        mock_precheck.return_value = {"model": "gpt-5.2", "validated": True}

        session = create_engine_session(agent_type="ttadk_coco", cwd="/tmp")

        # Handle potential wrappers (RateLimitAwareSession, ModelFailureAwareSession)
        inner = session
        while hasattr(inner, "_inner"):
            inner = inner._inner

        self.assertIsInstance(inner, SyncTTADKCLISession)

    @patch("src.agent_session.subprocess.Popen")
    def test_cli_session_execution(self, mock_popen):
        # Mock process
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["Hello\n", "World\n"])
        mock_proc.stderr = iter([])  # Empty stderr
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None

        mock_popen.return_value = mock_proc

        session = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")

        events = []

        def on_event(e):
            events.append(e)

        result = session.send_prompt("hi", on_event=on_event)

        self.assertEqual(result.text, "Hello\nWorld")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].text, "Hello\n")

        # Verify env vars
        args, kwargs = mock_popen.call_args
        env = kwargs.get("env")
        self.assertEqual(env.get("PYTHONUNBUFFERED"), "1")
        self.assertEqual(env.get("NO_COLOR"), "1")
        self.assertEqual(env.get("TERM"), "dumb")

    @patch("src.agent_session.subprocess.Popen")
    def test_cli_session_filters_preamble_and_keeps_json(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(
            [
                "  _____ _____  _    ____  _  __\n",
                "TikTok AI-Driven Development Kit\n",
                "Version 0.3.9\n",
                '{"type":"chunk","text":"hello"}\n',
                '{"type":"done"}\n',
            ]
        )
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        session = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
        events = []
        result = session.send_prompt("hi", on_event=lambda e: events.append(e))

        self.assertEqual(
            result.text,
            '{"type":"chunk","text":"hello"}\n{"type":"done"}',
        )
        self.assertEqual(
            [e.text for e in events],
            [
                '{"type":"chunk","text":"hello"}\n',
                '{"type":"done"}\n',
            ],
        )

    @patch("src.agent_session.subprocess.Popen")
    def test_cli_session_skips_invalid_braces_and_extracts_later_json(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(
            [
                "prefix {not-json}\n",
                "still noise\n",
                '{"type":"chunk","text":"ok"}\n',
                '{"type":"done"}\n',
            ]
        )
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        session = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
        events = []
        result = session.send_prompt("hi", on_event=lambda e: events.append(e))

        self.assertEqual(result.text, '{"type":"chunk","text":"ok"}\n{"type":"done"}')
        self.assertEqual(
            [e.text for e in events],
            [
                '{"type":"chunk","text":"ok"}\n',
                '{"type":"done"}\n',
            ],
        )

    @patch("src.agent_session.subprocess.Popen")
    def test_cli_session_extracts_jsonl_from_mixed_lines(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(
            [
                'noise -> {"type":"chunk","text":"first"}\n',
                'more noise {"type":"chunk","text":"second"} tail\n',
                "not-json-line\n",
                '{"type":"done"}\n',
            ]
        )
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        session = SyncTTADKCLISession(agent_type="ttadk_coco", cwd="/tmp")
        events = []
        result = session.send_prompt("hi", on_event=lambda e: events.append(e))

        self.assertEqual(
            result.text,
            '{"type":"chunk","text":"first"}\n{"type":"chunk","text":"second"}\n{"type":"done"}',
        )
        self.assertEqual(
            [e.text for e in events],
            [
                '{"type":"chunk","text":"first"}\n',
                '{"type":"chunk","text":"second"}\n',
                '{"type":"done"}\n',
            ],
        )


if __name__ == "__main__":
    unittest.main()
