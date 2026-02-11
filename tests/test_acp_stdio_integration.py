"""End-to-end ACP stdio integration test.

This validates that GhostAP's ACP client sends JSON-RPC params that match
`agent-client-protocol`'s pydantic schema (incl. aliases like `sessionId`).

It spins up a minimal ACP agent in a subprocess (python -c) using
`acp.stdio.AgentSideConnection` and exercises:
- initialize
- new_session
- load_session (used by GhostAP health_check)
- prompt
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from src.acp.session import ACPSession


_FAKE_AGENT_CODE = textwrap.dedent(
    r"""
    import asyncio
    import sys
    import uuid

    from acp.helpers import update_agent_message_text
    from acp.schema import InitializeResponse, LoadSessionResponse, NewSessionResponse, PromptResponse
    from acp.stdio import AgentSideConnection


    class FakeAgent:
        def __init__(self):
            self._conn = None
            self._sessions = set()

        def on_connect(self, conn):
            self._conn = conn

        async def initialize(self, protocol_version: int, client_capabilities=None, client_info=None, **kwargs):
            return InitializeResponse(protocol_version=protocol_version)

        async def new_session(self, cwd: str, mcp_servers=None, **kwargs):
            sid = "s_" + uuid.uuid4().hex[:8]
            self._sessions.add(sid)
            return NewSessionResponse(session_id=sid)

        async def load_session(self, cwd: str, session_id: str, mcp_servers=None, **kwargs):
            self._sessions.add(session_id)
            return LoadSessionResponse()

        async def prompt(self, prompt, session_id: str, **kwargs):
            # Emit a streaming message chunk so GhostAP can aggregate text.
            if self._conn is not None:
                await self._conn.session_update(session_id=session_id, update=update_agent_message_text("hello-from-fake"))
            return PromptResponse(stop_reason="end_turn")


    async def _make_stdio_streams():
        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        reader_protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)

        transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout.buffer)
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        return reader, writer


    async def main():
        reader, writer = await _make_stdio_streams()
        conn = AgentSideConnection(FakeAgent(), writer, reader, listening=True)
        await conn.listen()


    if __name__ == "__main__":
        asyncio.run(main())
    """
).strip()


@pytest.mark.asyncio
async def test_acp_stdio_prompt_and_health_check(tmp_path):
    # Use a temp cwd so the agent is sandboxed.
    cwd = str(tmp_path)

    s = ACPSession(
        agent_cmd=sys.executable,
        agent_args=["-u", "-c", _FAKE_AGENT_CODE],
        cwd=cwd,
    )

    try:
        session_id = await s.start()
        assert session_id

        # health_check internally calls `session/load`.
        assert await s.health_check(timeout=2.0) is True

        r = await s.prompt("ping")
        assert r.stop_reason
        assert "hello-from-fake" in (r.text or "")
    finally:
        await s.close()

