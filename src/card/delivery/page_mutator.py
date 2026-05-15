"""PageMutator: encapsulates card page mutation operations (create/update/stream/finalize)."""

from __future__ import annotations

import json
import logging
import re
import uuid

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.types import MutationOutcome, SequenceConflictError, TransportError
from src.card.render.payload_truncator import check_and_truncate_payload
from src.card.types import RenderedCard

logger = logging.getLogger(__name__)

_PAGE_CREATE_NAMESPACE = uuid.UUID("4388eebc-b0bc-5d6a-9c65-6ac058db0324")
_ERRMSG_RE = re.compile(r"ErrMsg:\s*([^;]+)")


def _guard_payload(card_payload: dict) -> dict:
    """Pre-flight guard: truncate payload if it exceeds Feishu limits (200 elements)."""
    raw = json.dumps(card_payload, ensure_ascii=False)
    guarded = check_and_truncate_payload(raw)
    if guarded is raw:
        return card_payload
    return json.loads(guarded)


def _fallback_invalid_card(reason: str) -> dict:
    """Small known-good card used when Feishu rejects the rendered card JSON."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "⚠️ 卡片渲染失败"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "当前卡片内容不符合飞书卡片格式，已停止自动重建以避免重复刷屏。\n\n"
                        f"原因：{reason or 'card_content_invalid'}"
                    ),
                }
            ]
        },
    }


def _format_invalid_card_reason(error: TransportError) -> str:
    parts: list[str] = []
    if error.code:
        parts.append(f"code={error.code}")
    match = _ERRMSG_RE.search(str(error))
    if match:
        parts.append(match.group(1).strip())
    return "；".join(parts) or "card_content_invalid"


def _page_create_idempotency_key(session_id: str, page_index: int) -> str:
    """Stable Feishu IM uuid for visible card-message creation."""
    return str(uuid.uuid5(_PAGE_CREATE_NAMESPACE, f"{session_id}:{page_index}"))


class PageMutator:
    """Handles page-level card mutations against the Feishu API.

    Stateless with respect to session lifecycle — all state lives in
    the injected collaborators (client, bindings, sequences).
    CardDelivery retains ownership of these collaborators' lifecycles.
    """

    def __init__(self, client, bindings: BindingStore, sequences: SequenceManager) -> None:
        self._client = client
        self._bindings = bindings
        self._sequences = sequences

    def create_page(
        self,
        session_id: str,
        chat_id: str,
        card: RenderedCard,
        *,
        reply_to: str | None = None,
    ) -> "MutationOutcome":
        """Create a new card page via API."""

        try:
            card_payload = _guard_payload(card.to_feishu_json())
            is_streaming = card_payload.get("config", {}).get("streaming_mode", False)
            idempotency_key = _page_create_idempotency_key(session_id, card.page_index)

            if is_streaming:
                try:
                    card_id = self._client.create_streaming_card(card_payload)
                    message_id = self._client.send_card_reference(
                        chat_id, card_id, reply_to=reply_to, idempotency_key=idempotency_key
                    )
                except Exception:
                    logger.debug("Streaming card creation failed, falling back to IM API")
                    message_id, card_id = self._client.create_card(
                        chat_id, card_payload, reply_to=reply_to, idempotency_key=idempotency_key
                    )
            else:
                message_id, card_id = self._client.create_card(
                    chat_id, card_payload, reply_to=reply_to, idempotency_key=idempotency_key
                )

            last_text = card.active_element.text if card.active_element else ""
            self._bindings.set_page(
                session_id=session_id,
                page_index=card.page_index,
                message_id=message_id,
                card_id=card_id,
                signature=card.structure_signature,
                last_text=last_text,
            )
            return MutationOutcome(kind="applied", message=f"created:{message_id}")
        except Exception as e:
            logger.warning("Card create failed: %s", str(e))
            return MutationOutcome(kind="reconcile", message=str(e))

    def update_page(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> "MutationOutcome":
        """Update card structure via PATCH API."""

        try:
            seq = self._sequences.next_sequence(page.card_id)
            self._client.update_card(page.card_id, _guard_payload(card.to_feishu_json()), sequence=seq)
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            if card.active_element:
                self._bindings.update_text(session_id, page.page_index, card.active_element.text)
            return MutationOutcome(kind="applied", message=f"updated:{page.card_id}")
        except TimeoutError as e:
            logger.warning("Card update timed out on %s; dropping binding to avoid stale late writes: %s", page.card_id, str(e))
            self._bindings.remove_page(session_id, page.page_index)
            self._sequences.reset(page.card_id)
            return MutationOutcome(kind="reconcile", message="recreate:timeout")
        except SequenceConflictError as e:
            self._sequences.raise_floor(page.card_id, e.next_floor)
            logger.debug("Sequence conflict on %s, raised floor to %d", page.card_id, e.next_floor)
            return MutationOutcome(kind="reconcile", message="sequence_conflict")
        except TransportError as e:
            if e.needs_recreate:
                logger.warning(
                    "Stale card binding on %s (code=%d), removing binding to force recreation",
                    page.card_id, e.code,
                )
                self._bindings.remove_page(session_id, page.page_index)
                self._sequences.reset(page.card_id)
                return MutationOutcome(kind="reconcile", message=f"recreate:{e.code}")
            if e.is_content_invalid:
                return self._replace_with_invalid_card_fallback(session_id, page, card, e)
            logger.warning("Transport error updating %s: %s", page.card_id, str(e))
            return MutationOutcome(kind="reconcile", message=str(e))
        except Exception as e:
            logger.warning("Card update failed: %s", str(e))
            return MutationOutcome(kind="reconcile", message=str(e))

    def stream_element(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> "MutationOutcome":
        """Push text update via CardKit element_content API."""

        if card.active_element is None:
            return MutationOutcome(kind="skipped")

        try:
            seq = self._sequences.next_sequence(page.card_id)
            self._client.update_element(
                page.card_id,
                card.active_element.element_id,
                card.active_element.text,
                sequence=seq,
            )
            self._bindings.update_text(session_id, page.page_index, card.active_element.text)
            return MutationOutcome(kind="applied", message=f"element:{page.card_id}")
        except SequenceConflictError as e:
            self._sequences.raise_floor(page.card_id, e.next_floor)
            logger.debug("Element sequence conflict on %s, falling back to full update", page.card_id)
            return self.update_page(session_id, page, card)
        except TransportError as e:
            if e.needs_recreate:
                logger.warning(
                    "Stale card binding on %s (code=%d), removing binding to force recreation",
                    page.card_id, e.code,
                )
                self._bindings.remove_page(session_id, page.page_index)
                self._sequences.reset(page.card_id)
                return MutationOutcome(kind="reconcile", message=f"recreate:{e.code}")
            logger.debug("Element update failed (%s), falling back to full update", str(e))
            return self.update_page(session_id, page, card)
        except TimeoutError as e:
            logger.warning("Element update timed out on %s; dropping binding to avoid stale late writes: %s", page.card_id, str(e))
            self._bindings.remove_page(session_id, page.page_index)
            self._sequences.reset(page.card_id)
            return MutationOutcome(kind="reconcile", message="recreate:timeout")
        except Exception as e:
            logger.debug("Element update failed (%s), falling back to full update", str(e))
            return self.update_page(session_id, page, card)

    def finalize_page(self, session_id: str, page: PageBinding) -> None:
        """Finalize a stale page."""
        if page.card_id:
            self._sequences.reset(page.card_id)
        self._bindings.remove_page(session_id, page.page_index)

    def _replace_with_invalid_card_fallback(
        self,
        session_id: str,
        page: PageBinding,
        card: RenderedCard,
        error: TransportError,
    ) -> MutationOutcome:
        """Patch a stable fallback card and mark the bad signature handled."""
        logger.error(
            "Feishu rejected rendered card JSON on %s (code=%d); patching fallback and suppressing recreate loop: %s",
            page.card_id,
            error.code,
            str(error),
        )
        try:
            seq = self._sequences.next_sequence(page.card_id)
            reason = _format_invalid_card_reason(error)
            self._client.update_card(page.card_id, _fallback_invalid_card(reason), sequence=seq)
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            self._bindings.update_text(session_id, page.page_index, "card_content_invalid")
            return MutationOutcome(kind="applied", message=f"fallback_content_invalid:{error.code}")
        except SequenceConflictError as seq_err:
            self._sequences.raise_floor(page.card_id, seq_err.next_floor)
            return MutationOutcome(kind="reconcile", message="sequence_conflict")
        except TransportError as fallback_err:
            if fallback_err.needs_recreate:
                self._bindings.remove_page(session_id, page.page_index)
                self._sequences.reset(page.card_id)
                return MutationOutcome(kind="reconcile", message=f"recreate:{fallback_err.code}")
            logger.warning("Fallback card patch failed on %s: %s", page.card_id, str(fallback_err))
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            self._bindings.update_text(session_id, page.page_index, "card_content_invalid")
            return MutationOutcome(kind="applied", message=f"fallback_suppressed:{fallback_err.code}")
        except Exception as fallback_exc:
            logger.warning("Fallback card patch failed on %s: %s", page.card_id, str(fallback_exc))
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            self._bindings.update_text(session_id, page.page_index, "card_content_invalid")
            return MutationOutcome(kind="applied", message="fallback_suppressed")
