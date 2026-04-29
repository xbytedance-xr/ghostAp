"""Lightweight ACP sub-session helper for auxiliary prompts.

Creates a short-lived ACP session, sends a single prompt, collects the
text response, and tears down the session.  Used by engines for criteria
decomposition and review-parsing fallback — tasks that should NOT pollute
the main execution session's conversation history.
"""

import logging
from typing import Callable, Optional

from ..acp import ACPEvent, ACPEventType
from ..utils.errors import get_error_detail

logger = logging.getLogger(__name__)


def prompt_via_acp(
    text: str,
    create_session_fn: Callable,
    agent_type: str,
    cwd: str,
    timeout: int = 60,
    model_name: Optional[str] = None,
) -> str:
    """Send *text* through a disposable ACP sub-session and return the response.

    Parameters
    ----------
    text:
        The prompt to send.
    create_session_fn:
        Factory that creates a ``SyncSession`` (typically
        ``create_engine_session``).
    agent_type:
        Which ACP agent to start (e.g. ``"coco"``).
    cwd:
        Working directory for the agent process.
    timeout:
        Per-prompt timeout in seconds.
    model_name:
        Optional model override.

    Returns
    -------
    str
        The concatenated text response, or ``""`` on any failure.
    """
    session = None
    try:
        session = create_session_fn(
            agent_type=agent_type,
            cwd=cwd,
            model_name=model_name,
        )

        collected: list[str] = []

        def _on_event(event: ACPEvent) -> None:
            if event.event_type == ACPEventType.TEXT_CHUNK and event.text:
                collected.append(event.text)

        session.send_prompt(text, on_event=_on_event, timeout=timeout)
        return "".join(collected)

    except Exception as exc:
        logger.debug("ACP sub-session prompt failed: %s", get_error_detail(exc))
        return ""

    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                logger.debug("ACP sub-session close failed", exc_info=True)
