import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

from ..engine_base import BaseEngineManager
from ..utils.engine_identity import resolve_engine_identity
from .engine import SpecEngine
from .models import SpecProject


class SpecEngineManager(BaseEngineManager["SpecEngine"]):
    """Manages SpecEngine instances per chat.

    Uses a secondary index (_chat_keys) to avoid O(n) full-table scans.
    """

    def _create_engine(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str,
        engine_name: str,
        model_name: Optional[str],
    ) -> "SpecEngine":
        return SpecEngine(
            chat_id=chat_id,
            root_path=root_path,
            agent_type=agent_type,
            engine_name=engine_name,
            model_name=model_name,
        )

    def _resolve_engine_identity(
        self,
        *,
        engine_name: str = "Coco",
        agent_type: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> tuple[str, str, Optional[str]]:
        normalized_agent = str(agent_type or "").strip().lower()
        normalized_name = str(engine_name or "").strip() or "Coco"

        if normalized_agent.startswith("ttadk_"):
            return SpecEngine._infer_engine_name(normalized_agent), normalized_agent, model_name
        if normalized_agent == "claude":
            return "Claude", "claude", None
        if normalized_agent in {"aiden", "codex", "gemini", "coco"}:
            from ..mode import InteractionMode

            mode_map = {
                "coco": InteractionMode.COCO,
                "aiden": InteractionMode.AIDEN,
                "codex": InteractionMode.CODEX,
                "gemini": InteractionMode.GEMINI,
            }
            identity = resolve_engine_identity(mode=mode_map[normalized_agent])
            return identity.engine_name, identity.agent_type, (model_name or identity.model_name)
        if normalized_agent:
            return SpecEngine._infer_engine_name(normalized_agent), normalized_agent, model_name

        from ..ttadk import get_ttadk_manager
        from ..mode import InteractionMode

        if normalized_name.lower() == "ttadk":
            ttadk_manager = get_ttadk_manager()
            current_tool = ttadk_manager.get_current_tool()
            current_model = ttadk_manager.get_current_model()
            identity = resolve_engine_identity(
                mode=InteractionMode.TTADK,
                ttadk_tool_name=current_tool,
                ttadk_model_name=current_model,
            )
            return identity.engine_name, identity.agent_type, (model_name or identity.model_name)

        if normalized_name.lower().startswith("claude"):
            identity = resolve_engine_identity(mode=InteractionMode.CLAUDE)
            return identity.engine_name, identity.agent_type, identity.model_name

        if normalized_name.lower().startswith("aiden"):
            identity = resolve_engine_identity(mode=InteractionMode.AIDEN)
            return identity.engine_name, identity.agent_type, (model_name or identity.model_name)

        if normalized_name.lower().startswith("codex"):
            identity = resolve_engine_identity(mode=InteractionMode.CODEX)
            return identity.engine_name, identity.agent_type, (model_name or identity.model_name)

        if normalized_name.lower().startswith("gemini"):
            identity = resolve_engine_identity(mode=InteractionMode.GEMINI)
            return identity.engine_name, identity.agent_type, (model_name or identity.model_name)

        identity = resolve_engine_identity(mode=InteractionMode.COCO)
        return identity.engine_name, identity.agent_type, (model_name or identity.model_name)

    def get_or_create(
        self,
        chat_id: str,
        root_path: str,
        engine_name: str = "Coco",
        *,
        agent_type: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> "SpecEngine":
        key = f"{chat_id}:{root_path}"
        resolved_engine_name, resolved_agent_type, resolved_model_name = self._resolve_engine_identity(
            engine_name=engine_name,
            agent_type=agent_type,
            model_name=model_name,
        )

        with self._lock:
            existing = self._engines.get(key)
            should_replace = False
            if existing:
                should_replace = (
                    not existing.is_running
                    and (
                        existing.engine_name.lower() != resolved_engine_name.lower()
                        or existing._agent_type != resolved_agent_type
                        or existing._model_name != resolved_model_name
                    )
                )

            if existing is None or should_replace:
                if existing is not None:
                    existing.cleanup()
                self._engines[key] = SpecEngine(
                    chat_id=chat_id,
                    root_path=root_path,
                    agent_type=resolved_agent_type,
                    engine_name=resolved_engine_name,
                    model_name=resolved_model_name,
                )
                self._add_index(chat_id, key)

            return self._engines[key]

    def load_or_create_from_disk(self, chat_id: str, root_path: str, engine_name: str = "Coco") -> "SpecEngine":
        """Create engine and hydrate project state from disk if present.

        用于进程重启后的断点续传：handler 在 `/spec_status`/`/spec_resume` 时可调用。
        """
        seed_engine = self.get_or_create(chat_id, root_path, engine_name=engine_name)
        if not seed_engine.settings.spec_allow_resume_from_disk:
            return seed_engine

        state_path = os.path.join(root_path, seed_engine.settings.spec_state_filename)
        persisted_project = None
        persisted_runtime = None
        persisted_saved_at = None
        persisted_compact = None

        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                proj = data.get("project")
                if isinstance(proj, dict):
                    persisted_project = proj
                    persisted_runtime = data.get("runtime_context") if isinstance(data.get("runtime_context"), dict) else None
                    persisted_saved_at = data.get("saved_at")
                    persisted_compact = proj.get("_compact")
            except Exception:
                persisted_project = None
                persisted_runtime = None

        runtime = dict(persisted_runtime or {})
        engine = self.get_or_create(
            chat_id,
            root_path,
            engine_name=runtime.get("engine_name") or engine_name,
            agent_type=runtime.get("agent_type"),
            model_name=runtime.get("model_name") or runtime.get("current_model"),
        )

        if persisted_project is not None and engine.project is None:
            try:
                engine._project = SpecProject.from_dict(persisted_project)
                engine._restore_runtime_context(runtime)
                engine._resume_meta = {
                    "state_path": state_path,
                    "saved_at": persisted_saved_at,
                    "compact": persisted_compact,
                }
            except Exception:
                logger.debug("failed to attach persistence metadata", exc_info=True)
        return engine
