"""PageMutator: encapsulates card page mutation operations (create/update/stream/finalize)."""

from __future__ import annotations

import json
import logging
import re
import uuid

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.types import MutationOutcome, SequenceConflictError, TransportError
from src.card.shared.truncation import check_and_truncate_payload
from src.card.types import RenderedCard

logger = logging.getLogger(__name__)

_PAGE_CREATE_NAMESPACE = uuid.UUID("4388eebc-b0bc-5d6a-9c65-6ac058db0324")
_ERRMSG_RE = re.compile(r"ErrMsg:\s*([^;]+)")
_EMAIL_ADDRESS_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}(?![\w.-])"
)


def sanitize_card_text_for_audit(text: str) -> str:
    """Remove text patterns known to trigger Feishu card content audit."""
    if not text:
        return text
    return _EMAIL_ADDRESS_RE.sub("[redacted:email]", text)


def _sanitize_payload_for_audit(node):
    if isinstance(node, dict):
        for key, value in list(node.items()):
            node[key] = _sanitize_payload_for_audit(value)
        return node
    if isinstance(node, list):
        for idx, value in enumerate(node):
            node[idx] = _sanitize_payload_for_audit(value)
        return node
    if isinstance(node, str):
        return sanitize_card_text_for_audit(node)
    return node


def _guard_payload(card_payload: dict) -> dict:
    """Pre-flight guard: truncate payload if it exceeds Feishu limits (200 elements)."""
    card_payload = _sanitize_payload_for_audit(card_payload)
    raw = json.dumps(card_payload, ensure_ascii=False)
    guarded = check_and_truncate_payload(raw)
    if guarded is raw:
        return card_payload
    return _sanitize_payload_for_audit(json.loads(guarded))


def _find_element_content(payload: dict, element_id: str | None) -> tuple[bool, str]:
    """Return whether element_id exists and its actual markdown/plain_text content."""
    if not element_id:
        return False, ""

    def walk(node) -> str | None:
        if isinstance(node, dict):
            if node.get("element_id") == element_id and isinstance(node.get("content"), str):
                return node["content"]
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    found = walk(payload)
    return found is not None, found or ""


def _has_rendered_body_elements(payload: dict) -> bool:
    body = payload.get("body") if isinstance(payload, dict) else None
    elements = body.get("elements") if isinstance(body, dict) else None
    return isinstance(elements, list) and bool(elements)


def _actual_active_text(card: RenderedCard, payload: dict) -> str:
    if card.active_element is None:
        return ""
    found, text = _find_element_content(payload, card.active_element.element_id)
    if found:
        return text
    # Some tests and legacy callers pass skeletal RenderedCard payloads. Real
    # renderer output has body.elements; only fall back when there is no payload
    # body to inspect. Guard fallback/truncation cards must not poison last_text.
    if not _has_rendered_body_elements(payload):
        return card.active_element.text
    return ""


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


def _fallback_audit_rejected_card() -> dict:
    """Small known-good card used when Feishu audit rejects rendered content."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "yellow",
            "title": {"tag": "plain_text", "content": "⚠️ 卡片内容受限"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "任务已结束，但最终卡片包含飞书审核限制内容，已改为安全提示以避免停留在旧状态。\n\n"
                        "可发送状态命令查看任务状态；如需继续，请重新发起任务。"
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
        reply_in_thread: bool | None = None,
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
                        chat_id, card_id, reply_to=reply_to, reply_in_thread=reply_in_thread, idempotency_key=idempotency_key
                    )
                except Exception:
                    logger.debug("Streaming card creation failed, falling back to IM API")
                    message_id, card_id = self._client.create_card(
                        chat_id, card_payload, reply_to=reply_to, reply_in_thread=reply_in_thread, idempotency_key=idempotency_key
                    )
            else:
                message_id, card_id = self._client.create_card(
                    chat_id, card_payload, reply_to=reply_to, reply_in_thread=reply_in_thread, idempotency_key=idempotency_key
                )

            last_text = _actual_active_text(card, card_payload)
            self._bindings.set_page(
                session_id=session_id,
                page_index=card.page_index,
                message_id=message_id,
                card_id=card_id,
                signature=card.structure_signature,
                last_text=last_text,
            )
            return MutationOutcome(kind="applied", message=f"created:{message_id}")
        except TransportError as e:
            if e.is_audit_rejected:
                return self._create_audit_rejected_fallback_page(
                    session_id,
                    chat_id,
                    card,
                    reply_to=reply_to,
                    reply_in_thread=reply_in_thread,
                    error=e,
                )
            logger.warning("Card create failed: %s", str(e))
            return MutationOutcome(kind="reconcile", message=str(e))
        except Exception as e:
            logger.warning("Card create failed: %s", str(e))
            return MutationOutcome(kind="reconcile", message=str(e))

    def update_page(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> "MutationOutcome":
        """Update card structure via PATCH API."""

        try:
            seq = self._sequences.next_sequence(page.card_id)
            card_payload = _guard_payload(card.to_feishu_json())
            self._client.update_card(page.card_id, card_payload, sequence=seq)
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            if card.active_element:
                self._bindings.update_text(session_id, page.page_index, _actual_active_text(card, card_payload))
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
            if e.is_audit_rejected:
                return self._replace_with_audit_rejected_fallback(session_id, page, card, e)
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
            content = sanitize_card_text_for_audit(card.active_element.text)
            self._client.update_element(
                page.card_id,
                card.active_element.element_id,
                content,
                sequence=seq,
            )
            self._bindings.update_text(session_id, page.page_index, content)
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

    def _create_audit_rejected_fallback_page(
        self,
        session_id: str,
        chat_id: str,
        card: RenderedCard,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool | None = None,
        error: TransportError,
    ) -> MutationOutcome:
        logger.error(
            "Feishu audit rejected card create (code=%d); sending safe fallback: %s",
            error.code,
            str(error),
        )
        try:
            idempotency_key = _page_create_idempotency_key(session_id, card.page_index)
            message_id, card_id = self._client.create_card(
                chat_id,
                _fallback_audit_rejected_card(),
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
                idempotency_key=idempotency_key,
            )
            self._bindings.set_page(
                session_id=session_id,
                page_index=card.page_index,
                message_id=message_id,
                card_id=card_id,
                signature=card.structure_signature,
                last_text="card_audit_rejected",
            )
            return MutationOutcome(kind="applied", message=f"fallback_audit_rejected:{error.code}")
        except Exception as fallback_exc:
            logger.warning("Audit fallback card create failed: %s", str(fallback_exc))
            return MutationOutcome(kind="reconcile", message=str(error))

    def _replace_with_audit_rejected_fallback(
        self,
        session_id: str,
        page: PageBinding,
        card: RenderedCard,
        error: TransportError,
    ) -> MutationOutcome:
        logger.error(
            "Feishu audit rejected rendered card on %s (code=%d); patching safe fallback: %s",
            page.card_id,
            error.code,
            str(error),
        )
        try:
            seq = self._sequences.next_sequence(page.card_id)
            self._client.update_card(page.card_id, _fallback_audit_rejected_card(), sequence=seq)
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            self._bindings.update_text(session_id, page.page_index, "card_audit_rejected")
            return MutationOutcome(kind="applied", message=f"fallback_audit_rejected:{error.code}")
        except SequenceConflictError as seq_err:
            self._sequences.raise_floor(page.card_id, seq_err.next_floor)
            return MutationOutcome(kind="reconcile", message="sequence_conflict")
        except TransportError as fallback_err:
            if fallback_err.needs_recreate:
                self._bindings.remove_page(session_id, page.page_index)
                self._sequences.reset(page.card_id)
                return MutationOutcome(kind="reconcile", message=f"recreate:{fallback_err.code}")
            logger.warning("Audit fallback card patch failed on %s: %s", page.card_id, str(fallback_err))
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            self._bindings.update_text(session_id, page.page_index, "card_audit_rejected")
            return MutationOutcome(kind="applied", message=f"fallback_audit_suppressed:{fallback_err.code}")
        except Exception as fallback_exc:
            logger.warning("Audit fallback card patch failed on %s: %s", page.card_id, str(fallback_exc))
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            self._bindings.update_text(session_id, page.page_index, "card_audit_rejected")
            return MutationOutcome(kind="applied", message="fallback_audit_suppressed")
