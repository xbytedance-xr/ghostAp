import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional
import logging

logger = logging.getLogger(__name__)


_INVALID_MODEL_RE = re.compile(r"\binvalid\s+model\b", re.IGNORECASE)
_AVAILABLE_MODELS_RE = re.compile(r"Available models\s*:?\s*(.*)", re.IGNORECASE | re.DOTALL)

# 移除 ANSI 颜色码（ttadk 输出可能包含）
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[\?0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]")

# stdin is not a terminal/tty（ttadk code 在非 TTY 环境下常见）
_STDIN_NOT_TTY_RE = re.compile(r"stdin\s+is\s+not\s+a\s+(terminal|tty)", re.IGNORECASE)

# 真实模型 ID 通常由字母数字与分隔符组成（-._:）
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$")

# ISO8601 时间戳（例如 2026-03-07T07:24:28.514Z）常出现在日志/缓存中，
# 但不是有效模型 ID；需要在 token 过滤阶段排除，避免污染 models_cache.json 的解析。
_ISO8601_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$",
    re.IGNORECASE,
)


def truncate_snippet(text: str, max_len: int = 240) -> str:
    if not text:
        return ""
    text = str(text)
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def strict_truncate(s: str, lim: int = 200) -> str:
    try:
        lim = int(lim or 0)
    except Exception:
        lim = 0
    if lim <= 0:
        return ""
    try:
        ss = str(s or "")
    except Exception:
        ss = ""
    if len(ss) <= lim:
        return ss
    suffix = "…(truncated)"
    if lim <= len(suffix):
        return ss[:lim]
    return ss[: max(0, lim - len(suffix))] + suffix


def redact_and_truncate(
    text: object,
    *,
    hard_limit: int = 240,
    get_settings_fn: object = None,
    cfg_limit_key: str = "snippet_limit",
) -> str:
    try:
        from ..acp.diagnostics import get_diagnostics_config, redact_text

        if get_settings_fn is None:
            from ..config import get_settings as _gs

            get_settings_fn = _gs

        cfg = get_diagnostics_config(get_settings_fn=get_settings_fn)
        enabled = bool(getattr(cfg, "redact_enabled", True))
        patterns = list(getattr(cfg, "redact_patterns", []) or [])
        repl = str(getattr(cfg, "redact_replacement", "***REDACTED***") or "***REDACTED***")
        try:
            cfg_lim = int(getattr(cfg, str(cfg_limit_key or "") or "snippet_limit", 0) or 0)
        except Exception:
            cfg_lim = 0
        lim = int(hard_limit or 240)
        if cfg_lim > 0:
            lim = min(cfg_lim, lim) if lim > 0 else cfg_lim
        lim = max(1, lim)
    except Exception:
        enabled, patterns, repl, lim = True, [], "***REDACTED***", max(1, int(hard_limit or 240))

    try:
        s = str(text or "")
    except Exception:
        s = ""
    if enabled:
        try:
            from ..acp.diagnostics import redact_text

            s = redact_text(s, patterns, repl)
        except Exception:
            logger.debug("redact_and_truncate: import module", exc_info=True)
    return strict_truncate(s, int(lim))


def strip_ansi(text: str) -> str:
    """移除 ANSI/control 序列（用于解析 CLI 输出）。"""
    if not text:
        return ""
    try:
        return _ANSI_ESCAPE_RE.sub("", str(text))
    except Exception:
        return str(text)


def is_model_token(name: str) -> bool:
    """判断字符串是否可能是“真实模型 ID token”。

    该校验用于从非结构化输出中筛选候选模型名，尽量避免把友好文案/标题误当模型。
    """
    if not name:
        return False
    s = str(name).strip()
    if not s:
        return False
    if len(s) > 128:
        return False
    try:
        if not _MODEL_TOKEN_RE.match(s):
            return False
    except Exception:
        logger.debug("is_model_token: evaluate condition", exc_info=True)
        return False

    # 排除 ISO8601 时间戳（常见噪声）
    try:
        if _ISO8601_TS_RE.match(s):
            return False
    except Exception:
        logger.debug("is_model_token: evaluate condition", exc_info=True)
    # 经验规则：真实模型通常包含分隔符（-._:），纯字母数字更像短标签/标题
    if not any(ch in s for ch in ("-", ".", "_", ":")):
        return False
    # 经验规则：真实模型通常包含字母（过滤纯数字/日期/版本号等噪声 token）。
    try:
        if not re.search(r"[A-Za-z]", s):
            return False
    except Exception:
        logger.debug("is_model_token: evaluate condition", exc_info=True)
        return False
    # 经验规则：真实模型通常包含版本号/代际号（至少一个数字）。
    # 该规则用于过滤诸如 "not_a_model" 这类噪声 token。
    try:
        if not re.search(r"\d", s):
            return False
    except Exception:
        logger.debug("is_model_token: evaluate condition", exc_info=True)
        return False
    return True


def is_invalid_model_error(text: str) -> bool:
    if not text:
        return False
    clean = _ANSI_ESCAPE_RE.sub("", str(text))

    # 主要模式：ttadk 0.3.x 常见输出
    if _INVALID_MODEL_RE.search(clean):
        return True

    # 兼容常见变体：不同子命令 / 不同语言 / 不同参数报错
    # 注意：这里尽量收敛为“明确与 model 参数相关的错误”，避免误判。
    patterns = [
        # "unknown model xxx"
        re.compile(r"\bunknown\s+model\b", re.IGNORECASE),
        # "model must be one of: ..."
        re.compile(r"\bmodel\b[\s\S]{0,120}?\bmust\b[\s\S]{0,120}?\bone\s+of\b", re.IGNORECASE),
        # "invalid value ... --model" / "invalid value ... -m"
        re.compile(r"\binvalid\b[\s\S]{0,120}?(--model|\s-m\b)", re.IGNORECASE),
        # 中文/本地化输出（保守匹配）
        re.compile(r"(无效模型|模型无效|未知模型)", re.IGNORECASE),
    ]
    for p in patterns:
        try:
            if p.search(clean):
                return True
        except Exception:
            continue
    return False


def is_stdin_not_tty_error(text: str) -> bool:
    """判断是否为“stdin 不是终端”的可恢复错误。

    该错误通常发生在：以非交互方式运行 `ttadk code`（或下游工具）时。
    我们会在 TTADK→ACP 启动链路中据此触发 PTY 重试。
    """
    if not text:
        return False
    clean = _ANSI_ESCAPE_RE.sub("", str(text))
    try:
        return bool(_STDIN_NOT_TTY_RE.search(clean))
    except Exception:
        logger.debug("is_stdin_not_tty_error: return bool(_STDIN_NOT_TTY_RE.sea...", exc_info=True)
        return False


def extract_available_models(text: str) -> list[str]:
    """从 Invalid model 错误输出中提取真实模型列表（逗号/空白分隔）。"""
    if not text:
        return []
    clean = _ANSI_ESCAPE_RE.sub("", str(text))
    m = _AVAILABLE_MODELS_RE.search(clean)
    raw = ""
    if m:
        raw = (m.group(1) or "").strip()

    # 兼容输出变体："model must be one of: ..."（不一定包含 Available models）
    if not raw:
        m2 = re.search(r"model\s+must\s+be\s+one\s+of\s*:?(.*)", clean, re.IGNORECASE | re.DOTALL)
        if m2:
            raw = (m2.group(1) or "").strip()

    # 兼容：有些工具会输出 "Available models:" 但列表为空（例如 coco）。
    # 此时返回空，交由上层走 force_refresh / file_cache / interactive 等后备。
    if not raw:
        return []

    # 截断掉后续无关内容（例如 <id>...、Command failed...）
    raw = re.split(r"\n<id>.*", raw, maxsplit=1)[0]
    raw = re.split(r"\nCommand failed:.*", raw, maxsplit=1)[0]
    raw = raw.strip()
    if not raw:
        return []

    # 进一步截断“可用模型列表段”，避免异常对象拼接的 stdout/stderr snippet 等内容污染解析。
    # 典型噪声："Authorization: Bearer ..." 等 header/日志。
    # 规则：
    # - 单行列表（常见）只取第一行
    # - 多行 bullet 列表保留连续 bullet 行，遇到 header-like 行则停止
    normalized_raw = raw.replace("\r", "\n")
    lines = list(normalized_raw.split("\n"))
    # 去掉首尾空行
    while lines and not (lines[0] or "").strip():
        lines.pop(0)
    while lines and not (lines[-1] or "").strip():
        lines.pop()
    if not lines:
        return []

    bullet_prefixes = ("-", "*", "•", "·", ">", "❯")
    first = (lines[0] or "").strip()
    is_bullet = first.startswith(bullet_prefixes)
    if not is_bullet:
        # 既可能是单行逗号分隔，也可能是“多行纯 token”（无 bullet，仅缩进）。
        # 规则：若连续多行都像 token，则保留这些 token 行；否则只取第一行。
        kept2: list[str] = []
        for ln in lines:
            s = (ln or "").strip()
            if not s:
                break
            # 避免把后续的命令回显/日志行带入
            if re.match(r"^(command|usage)\b", s, re.IGNORECASE):
                break
            if _MODEL_TOKEN_RE.match(s):
                kept2.append(s)
                continue
            break
        if len(kept2) >= 2:
            raw = "\n".join(kept2).strip()
        else:
            # 单行格式：优先只取第一行（避免后续日志行进入 token 化）
            raw = lines[0].strip()
    else:
        kept: list[str] = []
        header_like = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_\-]*\s*:\s+")
        for ln in lines:
            s = (ln or "").rstrip("\n")
            if not s.strip():
                break
            if header_like.match(s):
                break
            # 只保留 bullet 行（或其缩进变体）
            if s.strip().startswith(bullet_prefixes):
                kept.append(s)
                continue
            # 兼容：某些输出是“纯 token 多行”（无 bullet），只要像 token 就保留
            if _MODEL_TOKEN_RE.match(s.strip()):
                kept.append(s)
                continue
            break
        raw = "\n".join(kept).strip()
    if not raw:
        return []

    # 将多行列表统一处理为 token 序列
    tokens: list[str] = []
    normalized = raw.replace("\r", "\n")
    # 常见格式：逗号分隔 / 换行分隔 / 空白分隔
    if "," in normalized or "\n" in normalized:
        normalized = normalized.replace("\n", ",")
        for chunk in normalized.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            tokens.extend(chunk.split())
    else:
        tokens = normalized.split()

    models: list[str] = []
    for t in tokens:
        name = (t or "").strip()
        if not name:
            continue
        # 清理常见前缀/后缀噪声
        name = name.lstrip("-*•·>❯")
        name = name.strip().strip(".,;:)")
        if not name:
            continue
        # 过滤掉不像模型 ID 的 token
        if not _MODEL_TOKEN_RE.match(name):
            continue
        models.append(name)

    # 去重保持顺序
    seen = set()
    out: list[str] = []
    for x in models:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def parse_ttadk_models_from_output_to_models(text: str) -> list["TTADKModel"]:
    """从 TTADK 原生命令输出中提取“真实模型列表”（best-effort），并尽量保留友好展示名。

    返回 `list[TTADKModel]`：
    - `name`: 必须是 `is_model_token()` 判定通过的真实模型 ID token
    - `friendly_name`: 若输出提供 display/friendly 字段则保留；否则为空或退化为 `name`

    解析优先级：
    1) Invalid-model 输出（Available models / model must be one of）
    2) JSON 输出（list/dict/嵌套）
    3) 文本输出 token 提取

    Contract:
    - 永不抛异常
    - 去重保序（以真实 `name` 为 key）
    """
    if not text:
        return []

    try:
        clean = strip_ansi(str(text))
    except Exception:
        clean = str(text)

    payload = (clean or "").strip()
    if not payload:
        return []

    # 1) Invalid model: 最可信
    try:
        names = extract_available_models(payload)
    except Exception:
        names = []
    if names:
        out: list[TTADKModel] = []
        seen: set[str] = set()
        for n in names:
            nn = str(n or "").strip()
            if not nn or nn in seen:
                continue
            if not is_model_token(nn):
                continue
            seen.add(nn)
            out.append(TTADKModel(name=nn, description=nn, friendly_name=nn))
        return out

    # 2) JSON
    if payload.lstrip().startswith(("{", "[")):
        data = None
        try:
            data = json.loads(payload)
        except Exception:
            # 容错：截取首个 JSON blob
            s = payload.find("{")
            e = payload.rfind("}")
            if s >= 0 and e > s:
                try:
                    data = json.loads(payload[s : e + 1])
                except Exception:
                    data = None
            if data is None:
                s = payload.find("[")
                e = payload.rfind("]")
                if s >= 0 and e > s:
                    try:
                        data = json.loads(payload[s : e + 1])
                    except Exception:
                        data = None

        def _iter_items(obj: object) -> list[object]:
            if isinstance(obj, list):
                return list(obj)
            if isinstance(obj, dict):
                for k in (
                    "models",
                    "model_list",
                    "available_models",
                    "llm_models",
                    "llms",
                    "items",
                    "data",
                    "result",
                ):
                    v = obj.get(k)
                    if isinstance(v, list):
                        return list(v)
                return [obj]
            return []

        if data is not None:
            items = _iter_items(data)
            out: list[TTADKModel] = []
            seen: set[str] = set()

            for x in items:
                # string-only list
                if isinstance(x, str):
                    rid = x.strip()
                    if not rid or not is_model_token(rid) or rid in seen:
                        continue
                    seen.add(rid)
                    out.append(TTADKModel(name=rid, description=rid, friendly_name=rid))
                    continue

                if not isinstance(x, dict):
                    continue

                # 真实模型 ID（优先级固定，避免 name 字段语义漂移）
                rid = x.get("id") or x.get("model_id") or x.get("model") or x.get("model_name") or x.get("real_name")

                name_field = x.get("name")
                if rid is None and isinstance(name_field, str) and is_model_token(name_field):
                    rid = name_field

                if not isinstance(rid, str):
                    continue
                rid = rid.strip()
                if not rid or not is_model_token(rid) or rid in seen:
                    continue

                # 友好名字段：优先使用明确的 display/friendly 字段
                friendly = x.get("friendly_name") or x.get("display_name") or x.get("label") or x.get("title")
                if not isinstance(friendly, str) or not friendly.strip():
                    # 当 name 不是 token 时，更像显示名（保持历史策略行为）
                    if isinstance(name_field, str) and name_field.strip() and not is_model_token(name_field):
                        friendly = name_field
                    else:
                        friendly = ""
                else:
                    friendly = friendly.strip()

                desc = x.get("description")
                if not isinstance(desc, str) or not desc.strip():
                    desc = friendly if friendly else rid
                else:
                    desc = desc.strip()

                seen.add(rid)
                out.append(TTADKModel(name=rid, description=desc, friendly_name=str(friendly or "")))

            if out:
                return out

    # 3) 文本：提取 token（best-effort）
    try:
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{2,128}", payload)
    except Exception:
        tokens = []

    out2: list[TTADKModel] = []
    seen2: set[str] = set()
    for t in tokens:
        s = (t or "").strip()
        if not s or not is_model_token(s):
            continue
        if s in seen2:
            continue
        seen2.add(s)
        out2.append(TTADKModel(name=s, description=s, friendly_name=s))
    return out2


def parse_ttadk_models_from_output(text: str) -> list[str]:
    """从 TTADK 原生命令输出中提取“真实模型 ID 列表”（best-effort）。

    适用场景：
    - `ttadk code -m INVALID...` 的报错输出（Available models: ...）
    - `ttadk models/model list` 的 JSON/文本输出（若某些版本存在）

    约束：
    - 返回值仅包含 `is_model_token()` 判定为“像真实模型 ID”的 token
    - 解析失败返回空列表（上层可继续降级到其他信息源）
    """
    # SSOT：复用 `parse_ttadk_models_from_output_to_models()`，避免策略/解析规则漂移。
    # 该函数对外保持“返回真实模型 id token 列表”的历史契约不变。
    try:
        models = parse_ttadk_models_from_output_to_models(text)
    except Exception:
        models = []
    out: list[str] = []
    seen: set[str] = set()
    for m in models or []:
        try:
            n = str(getattr(m, "name", "") or "").strip()
        except Exception:
            n = ""
        if not n or n in seen:
            continue
        if not is_model_token(n):
            continue
        seen.add(n)
        out.append(n)
    return out


def extract_invalid_model_diagnostics(
    *,
    stdout: str = "",
    stderr: str = "",
    snippet_limit: int = 240,
) -> dict:
    """从一次 ttadk 运行输出中提取 Invalid model 诊断信息（运行期闭环用）。

    返回结构（稳定字段）：
    - invalid_model: bool
    - available_models: list[str]
    - stdout_snippet/stderr_snippet: str（截断）
    - combined_snippet: str（截断，便于日志）
    """
    out = strip_ansi(stdout or "")
    err = strip_ansi(stderr or "")
    blob = (out + "\n" + err).strip()
    invalid = is_invalid_model_error(blob)
    names = extract_available_models(blob) if invalid else []
    return {
        "invalid_model": bool(invalid),
        "available_models": list(names or []),
        "stdout_snippet": truncate_snippet(out, max_len=int(snippet_limit or 240)),
        "stderr_snippet": truncate_snippet(err, max_len=int(snippet_limit or 240)),
        "combined_snippet": truncate_snippet(blob, max_len=int(snippet_limit or 240)),
    }


def build_invalid_model_context(
    err: Exception,
    *,
    get_settings_fn: Optional[Callable[[], object]] = None,
    limit: int = 1600,
    parse_limit: Optional[int] = None,
) -> dict:
    """SSOT：从异常对象构造 Invalid model 诊断上下文（best-effort, never raises）。

    设计目标：统一运行期/启动期的 invalid-model 诊断构造，避免在上层与 repair 编排层出现
    多套“拼 blob + regex 解析”的分叉实现。

    返回字段（稳定，供上层 attempts/日志使用）：
    - err_blob: str           # 合并后的可匹配文本（已脱敏/截断，非空）
    - stderr_snippet: str     # 诊断片段（已脱敏/截断）
    - stdout_snippet: str     # 诊断片段（已脱敏/截断）
    - available_models: list[str]
    - is_invalid_model: bool

    约束：
    - available_models/is_invalid_model 的解析必须基于“原始错误文本”（必要时取尾部片段），
      不得依赖脱敏/截断后的 err_blob，以避免解析信息被截断吞掉。
    - 永不抛异常。
    """

    def _default_get_settings() -> object:
        from ..config import get_settings

        return get_settings()

    getter = get_settings_fn or _default_get_settings

    def _safe_str(x: object) -> str:
        try:
            return str(x or "")
        except Exception:
            logger.debug("_safe_str: return str(x or '')", exc_info=True)
            return ""

    # 1) 聚合原始文本（尽量包含可解析信息；never raises）
    parts: list[str] = []
    try:
        parts.append(_safe_str(err) or "")
    except Exception:
        parts.append("")
    for k in ("stderr_snippet", "stdout_snippet", "stderr", "stdout", "message"):
        try:
            v = getattr(err, k, None)
            if v:
                parts.append(_safe_str(v))
        except Exception:
            continue

    blob_raw = "\n".join([p for p in parts if p]).strip()
    if not blob_raw:
        blob_raw = "(empty)"

    # 2) 解析窗口：默认取较大的尾部片段（available models 通常在尾部）
    plim = parse_limit
    if plim is None:
        try:
            s = getter()
            plim = int(getattr(s, "ttadk_runtime_invalid_model_parse_limit", 0) or 0) or 12_000
        except Exception:
            plim = 12_000
    try:
        plim = int(plim or 0)
    except Exception:
        plim = 12_000
    plim = max(800, min(plim, 200_000))

    blob_for_parse = blob_raw
    try:
        if plim > 0 and len(blob_for_parse) > plim:
            blob_for_parse = blob_for_parse[-plim:]
    except Exception:
        blob_for_parse = blob_raw

    # 3) invalid-model 判断与 available models 提取（基于原始片段）
    try:
        invalid = bool(is_invalid_model_error(blob_for_parse))
    except Exception:
        invalid = False
    try:
        models = list(extract_available_models(blob_for_parse) or []) if invalid else []
    except Exception:
        models = []

    # 4) 回显片段：脱敏+截断（不影响解析）
    try:
        stderr_raw = _safe_str(getattr(err, "stderr_snippet", "") or getattr(err, "stderr", "") or "")
    except Exception:
        stderr_raw = ""
    try:
        stdout_raw = _safe_str(getattr(err, "stdout_snippet", "") or getattr(err, "stdout", "") or "")
    except Exception:
        stdout_raw = ""

    try:
        hard = int(limit or 1600)
    except Exception:
        hard = 1600
    hard = max(1, hard)

    # err_blob 属于“合并上下文”：用 total_limit 作为配置上限（更适合诊断）。
    err_blob = redact_and_truncate(blob_raw, hard_limit=hard, get_settings_fn=getter, cfg_limit_key="total_limit")
    stderr_snip = redact_and_truncate(stderr_raw, hard_limit=min(600, hard), get_settings_fn=getter, cfg_limit_key="snippet_limit")
    stdout_snip = redact_and_truncate(stdout_raw, hard_limit=min(600, hard), get_settings_fn=getter, cfg_limit_key="snippet_limit")

    if not err_blob:
        err_blob = "(empty)"

    return {
        "err_blob": err_blob,
        "stderr_snippet": stderr_snip,
        "stdout_snippet": stdout_snip,
        "available_models": models,
        "is_invalid_model": bool(invalid),
    }


def choose_best_available_model(*, input_model: str, available_models: list[str]) -> str | None:
    """从 available_models 中选择最可能匹配 input_model 的真实模型名。

    规则（保守、可解释）：
    1) 精确命中
    2) 前缀命中：`<input>-...`
    3) 包含命中：包含 input_model
    4) 家族命中：如果 input_model 像 `gpt-5.2`，优先选择包含该版本号且包含 `ttadk` 的项

    返回 None 表示无法可靠选择（应降级为 (auto)）。
    """
    intent = (input_model or "").strip()
    cands = [str(x).strip() for x in (available_models or []) if str(x).strip()]
    if not intent or not cands:
        return None

    # 去重保序
    seen = set()
    uniq: list[str] = []
    for x in cands:
        if x not in seen:
            seen.add(x)
            uniq.append(x)

    # 1) exact
    for x in uniq:
        if x == intent:
            return x

    # 2) prefix
    for x in uniq:
        if x.startswith(intent + "-"):
            return x

    # 3) contains
    for x in uniq:
        if intent in x:
            return x

    # 4) family hint: version-like token (e.g., gpt-5.2)
    try:
        m = re.search(r"([A-Za-z]+-[0-9]+\.[0-9]+)", intent)
        family = m.group(1) if m else ""
    except Exception:
        family = ""
    if family:
        # Prefer ttadk-suffixed models in same family
        for x in uniq:
            if family in x and "ttadk" in x:
                return x
        for x in uniq:
            if family in x:
                return x

    return None


def parse_models_cache_json(
    payload: object, *, tool_name: str, allow_cross_tool_fallback: bool = False
) -> tuple[list[str], bool]:
    """解析 `models_cache.json` 的通用 helper。

    返回 (model_names, exact_tool_hit)。
    - exact_tool_hit=True: 只取 payload[tool] 的列表。
    - exact_tool_hit=False: 未命中 tool；默认返回空（更安全），可选 allow_cross_tool_fallback=True 时聚合所有 tool 的模型作为低可信兜底。
    """
    tool = (tool_name or "").strip().lower()
    if not tool:
        return ([], False)
    if not isinstance(payload, dict):
        return ([], False)

    raw = payload.get(tool)
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                n = item.get("name") or item.get("id") or item.get("model") or item.get("model_name")
                if isinstance(n, str):
                    names.append(n)
        # 过滤 + 去重
        out: list[str] = []
        seen = set()
        for n in names:
            n = (n or "").strip()
            if not n or not is_model_token(n):
                continue
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
        return (out, True)

    if not allow_cross_tool_fallback:
        return ([], False)

    # 低可信兜底：跨 tool 聚合
    for v in payload.values():
        if not isinstance(v, list):
            continue
        for item in v:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                n = item.get("name") or item.get("id") or item.get("model") or item.get("model_name")
                if isinstance(n, str):
                    names.append(n)

    out2: list[str] = []
    seen2 = set()
    for n in names:
        n = (n or "").strip()
        if not n or not is_model_token(n):
            continue
        if n in seen2:
            continue
        seen2.add(n)
        out2.append(n)
    return (out2, False)


@dataclass
class TTADKTool:
    """TTADK 工具定义（用于工具选择与状态展示）。"""

    name: str
    description: str = ""
    is_default: bool = False
    skip_model_selection: bool = False


@dataclass
class TTADKModel:
    """TTADK 模型定义（真实模型 ID + 展示元信息）。"""

    name: str  # 真实模型名称（如 gpt-5.2-codex-ttadk）
    description: str = ""  # 描述
    is_default: bool = False
    friendly_name: str = ""  # 友好显示名称（如 GPT 5.2 Codex (Recommended)）


@dataclass
class ACPToolOption:
    """ACP 工具选择 UI 选项模型。

    说明：
    - 与 ACP 协议无关，仅用于卡片/命令层的工具选择展示；
    - 下游卡片/handler 只依赖 name/description/is_default/emoji 这几个字段；
    - 放在 ttadk.models 中是为了与 TTADKTool 等工具/模型定义靠近，避免污染 ACP 核心模型层。
    """

    name: str
    description: str = ""
    is_default: bool = False
    emoji: str = "🤖"


@dataclass
class ACPModelOption:
    """ACP 模型选择 UI 选项模型。

    同样只承载展示/选择语义，不参与 ACP 协议结构体定义。
    """

    name: str
    description: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class ModelDescriptor:
    """模型描述符：用于解耦 display/alias 与真实 model_id。

    约束：
    - `model_id` 必须用于下游启动参数/日志/修复重试（真实可调用名）
    - `display_name` 仅用于 UI 展示
    - `aliases` 用于兼容历史输入（display/别名/旧配置）
    """

    model_id: str
    display_name: str = ""
    aliases: list[str] = field(default_factory=list)
    source: str = ""  # cache | probe | interactive | defaults | runtime_seed | unknown
    verified: bool = False


def _normalize_model_key(name: object) -> str:
    try:
        s = str(name or "")
    except Exception:
        s = ""
    return s.strip().lower()


def build_model_id_index(descriptors: list[object]) -> tuple[dict[str, str], list[str]]:
    """构建 name/alias/display/model_id → model_id 的索引。

    冲突处理：
    - 同一个 key 映射到多个 model_id 时，保持“先到先得”以确保确定性
    - 冲突会写入 warnings（不抛异常）
    """

    idx: dict[str, str] = {}
    warnings: list[str] = []

    for d in descriptors or []:
        # 兼容：允许直接传 TTADKModel
        model_id = ""
        display_name = ""
        aliases: list[str] = []

        try:
            if isinstance(d, ModelDescriptor):
                model_id = str(d.model_id or "")
                display_name = str(d.display_name or "")
                aliases = [str(x) for x in (d.aliases or []) if str(x).strip()]
            else:
                # TTADKModel 或其他 dict-like
                model_id = str(getattr(d, "model_id", "") or getattr(d, "name", "") or "")
                display_name = str(getattr(d, "display_name", "") or getattr(d, "friendly_name", "") or "")
                raw_aliases = getattr(d, "aliases", None)
                if isinstance(raw_aliases, list):
                    aliases = [str(x) for x in raw_aliases if str(x).strip()]
        except Exception:
            model_id, display_name, aliases = "", "", []

        model_id = (model_id or "").strip()
        display_name = (display_name or "").strip()
        if not model_id:
            continue

        # 统一候选 key（去重保序）
        keys: list[str] = []
        for k in [model_id, display_name, *aliases]:
            kk = _normalize_model_key(k)
            if not kk:
                continue
            if kk not in keys:
                keys.append(kk)

        for k in keys:
            prev = idx.get(k)
            if prev is None:
                idx[k] = model_id
                continue
            if prev != model_id:
                # 确保稳定：保留旧映射，记录冲突
                warnings.append(f"model_alias_conflict:{k}:{prev}->{model_id}")

    return idx, warnings


def resolve_model_id(
    *,
    tool_name: str,
    input_name: str,
    descriptors: list[object],
    allow_unknown_passthrough: bool = False,
    max_candidates: int = 20,
) -> tuple["ResolvedModelResult", dict]:
    """唯一入口：将用户输入（display/alias/model_id）解析为真实 model_id。

    说明：
    - 返回的 `ResolvedModelResult.real_name` 语义固定为真实 `model_id`
    - 本函数只负责“映射/归一化”，不做“是否可用”的强校验（强校验由上层 require_valid 决策）
    - unknown 输入默认不透传（allow_unknown_passthrough=False），避免把 display 误当 model_id

    diagnostics 字段（稳定字段，best-effort）：
    - model_display: str
    - resolution_source: str
    - resolution_reason: str
    - candidates: list[dict]  # {model_id, display}
    - warnings: list[str]
    """

    tool = (tool_name or "").strip().lower()
    raw = (input_name or "").strip()

    idx, idx_warnings = build_model_id_index(list(descriptors or []))
    idx_warn = list(idx_warnings or [])

    def _iter_descriptor_items() -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        for d in descriptors or []:
            try:
                if isinstance(d, ModelDescriptor):
                    mid = str(d.model_id or "").strip()
                    disp = str(d.display_name or "").strip()
                else:
                    mid = str(getattr(d, "model_id", "") or getattr(d, "name", "") or "").strip()
                    disp = str(getattr(d, "display_name", "") or getattr(d, "friendly_name", "") or "").strip()
            except Exception:
                mid, disp = "", ""
            if not mid:
                continue
            items.append((mid, disp))
        return items

    def _find_display(mid: str) -> str:
        mid = str(mid or "").strip()
        if not mid:
            return ""
        for d in descriptors or []:
            try:
                if isinstance(d, ModelDescriptor):
                    if str(d.model_id or "").strip() == mid:
                        return str(d.display_name or "").strip()
                else:
                    rid = str(getattr(d, "model_id", "") or getattr(d, "name", "") or "").strip()
                    if rid == mid:
                        return str(getattr(d, "display_name", "") or getattr(d, "friendly_name", "") or "").strip()
            except Exception:
                continue
        return ""

    def _build_candidates(query: str) -> list[dict]:
        q = _normalize_model_key(query)
        if not q:
            return []
        out: list[dict] = []
        seen: set[str] = set()
        # 简单子串匹配：优先命中 key（display/alias/model_id）包含 query 的模型
        for k, mid in (idx or {}).items():
            try:
                if q in (k or ""):
                    if mid in seen:
                        continue
                    seen.add(mid)
                    out.append({"model_id": mid, "display": _find_display(mid)})
                    if len(out) >= int(max_candidates or 20):
                        break
            except Exception:
                continue
        return out

    warnings: list[str] = []
    # 控制 warnings 规模，避免污染日志
    if idx_warn:
        warnings.extend(idx_warn[:10])

    if not raw:
        r = ResolvedModelResult(
            tool_name=tool,
            input_name="",
            real_name="",
            source="unknown",
            validated=False,
            warnings=["missing_model_intent"],
        )
        return r, {
            "model_display": "",
            "resolution_source": "unknown",
            "resolution_reason": "empty_input",
            "candidates": [],
            "warnings": list(warnings),
        }

    key = _normalize_model_key(raw)
    resolved: str | None = None
    reason = ""
    src = ""

    try:
        resolved = idx.get(key)
    except Exception:
        resolved = None

    if resolved:
        mid = str(resolved or "").strip()
        # source 语义：exact 表示用户已输入 model_id；friendly 表示来自 display/aliases
        if _normalize_model_key(mid) == key:
            src = "exact"
            reason = "model_id_hit"
        else:
            src = "friendly"
            reason = "friendly_or_alias_hit"
        r = ResolvedModelResult(
            tool_name=tool,
            input_name=raw,
            real_name=mid,
            source=src,
            validated=True,
            warnings=list(warnings),
        )
        return r, {
            "model_display": _find_display(mid),
            "resolution_source": src,
            "resolution_reason": reason,
            "candidates": [],
            "warnings": list(warnings),
        }

    # 兜底：prefix/partial（为了兼容历史行为，例如输入 `gpt-5.2` 匹配 `gpt-5.2-codex-ttadk`）
    raw_l = _normalize_model_key(raw)
    if raw_l:
        items = _iter_descriptor_items()
        # prefix
        for mid, disp in items:
            try:
                if _normalize_model_key(mid).startswith(raw_l) or (
                    disp and _normalize_model_key(disp).startswith(raw_l)
                ):
                    r = ResolvedModelResult(
                        tool_name=tool,
                        input_name=raw,
                        real_name=mid,
                        source="prefix",
                        validated=True,
                        warnings=list(warnings),
                    )
                    return r, {
                        "model_display": disp or _find_display(mid),
                        "resolution_source": "prefix",
                        "resolution_reason": "prefix_match",
                        "candidates": [],
                        "warnings": list(warnings),
                    }
            except Exception:
                continue
        # partial
        for mid, disp in items:
            try:
                if raw_l in _normalize_model_key(mid) or (disp and raw_l in _normalize_model_key(disp)):
                    r = ResolvedModelResult(
                        tool_name=tool,
                        input_name=raw,
                        real_name=mid,
                        source="partial",
                        validated=True,
                        warnings=list(warnings),
                    )
                    return r, {
                        "model_display": disp or _find_display(mid),
                        "resolution_source": "partial",
                        "resolution_reason": "partial_match",
                        "candidates": [],
                        "warnings": list(warnings),
                    }
            except Exception:
                continue

    # unknown 分支：默认不透传，避免 display 误当 model_id
    if allow_unknown_passthrough and is_model_token(raw):
        mid = raw
        warnings2 = list(warnings)
        warnings2.append("unknown_model_passthrough")
        r = ResolvedModelResult(
            tool_name=tool,
            input_name=raw,
            real_name=mid,
            source="passthrough",
            validated=False,
            warnings=warnings2,
        )
        return r, {
            "model_display": _find_display(mid),
            "resolution_source": "passthrough",
            "resolution_reason": "token_passthrough",
            "candidates": [],
            "warnings": list(warnings2),
        }

    # unknown：返回候选，交由上层决定是否报错/是否刷新模型列表
    candidates = _build_candidates(raw)
    warnings2 = list(warnings)
    warnings2.append("unknown_model_input")
    r = ResolvedModelResult(
        tool_name=tool,
        input_name=raw,
        real_name=raw,
        source="unknown",
        validated=False,
        warnings=warnings2,
    )
    return r, {
        "model_display": "",
        "resolution_source": "unknown",
        "resolution_reason": "no_index_match",
        "candidates": list(candidates),
        "warnings": list(warnings2),
    }


@dataclass
class ToolListResult:
    tools: list[TTADKTool] = field(default_factory=list)
    cached: bool = False
    error: Optional[str] = None


@dataclass
class ModelListResult:
    models: list[TTADKModel] = field(default_factory=list)
    cached: bool = False
    error: Optional[str] = None
    source: str = ""  # cache | sync | probe | interactive | defaults | unknown
    warnings: list[str] = field(default_factory=list)
    diagnostics: Optional[dict] = None


def build_models_freshness(
    *,
    cached: bool,
    cache_ts: Optional[float],
    ttl_s: float,
    now_ts: Optional[float] = None,
) -> dict:
    """构造模型列表 freshness（稳定字段）。

    Contract:
    - 永不抛异常
    - 返回字段稳定：cached/age_s/ttl_s/is_fresh
    """
    try:
        now = float(now_ts) if now_ts is not None else None
    except Exception:
        now = None
    if now is None:
        # 避免在 models.py 顶层 import time（尽量低依赖），只在需要时动态导入
        try:
            import time as _time

            now = float(_time.time())
        except Exception:
            now = 0.0

    try:
        ts = float(cache_ts) if cache_ts is not None else 0.0
    except Exception:
        ts = 0.0
    try:
        ttl = float(ttl_s or 0.0)
    except Exception:
        ttl = 0.0
    ttl = max(0.0, ttl)

    age_s: float | None
    if ts and now and now >= ts:
        try:
            age_s = max(0.0, float(now - ts))
        except Exception:
            age_s = None
    else:
        age_s = None

    is_fresh = False
    if cached:
        if ttl <= 0:
            is_fresh = True
        elif age_s is not None and age_s <= ttl:
            is_fresh = True

    return {
        "cached": bool(cached),
        "age_s": age_s,
        "ttl_s": ttl,
        "is_fresh": bool(is_fresh),
    }


def build_model_list_diagnostics(
    *,
    source: str,
    cached: bool,
    cache_ts: Optional[float],
    ttl_s: float,
    chosen_strategy: str = "",
    attempts: Optional[list[dict]] = None,
    # 当 fetcher 直接抛异常、没有 attempts 时，可用 error_snippet 兜底填充 stderr_snippet
    error_snippet: str = "",
    now_ts: Optional[float] = None,
) -> dict:
    """构造 ModelListResult.diagnostics（稳定字段）。

    必含字段（SSOT 约束）：
    - source
    - raw_cmd
    - exit_code
    - stderr_snippet
    - freshness
    """
    atts = list(attempts or [])

    def _pick_attempt() -> dict:
        if not atts:
            return {}
        # 优先：chosen_strategy 且 ok=True
        if chosen_strategy:
            for a in atts:
                try:
                    if (a.get("strategy") == chosen_strategy) and bool(a.get("ok")):
                        return dict(a)
                except Exception:
                    continue
        # 回退：最后一次 attempt
        try:
            return dict(atts[-1])
        except Exception:
            logger.debug("_pick_attempt: return dict(atts[-1])", exc_info=True)
            return {}

    a = _pick_attempt()
    detail = {}
    try:
        if isinstance(a.get("detail"), dict):
            detail = dict(a.get("detail") or {})
    except Exception:
        detail = {}

    raw_cmd = a.get("raw_cmd")
    if raw_cmd is None:
        raw_cmd = detail.get("raw_cmd")

    exit_code = a.get("exit_code")
    if exit_code is None:
        exit_code = a.get("rc")
    if exit_code is None:
        exit_code = detail.get("exit_code")
    if exit_code is None:
        exit_code = detail.get("rc")

    stderr_snippet = a.get("stderr_snippet")
    if stderr_snippet is None:
        stderr_snippet = detail.get("stderr_snippet")
    if stderr_snippet is None:
        stderr_snippet = str(error_snippet or "")

    freshness = build_models_freshness(cached=cached, cache_ts=cache_ts, ttl_s=ttl_s, now_ts=now_ts)

    return {
        # contract fields
        "source": str(source or ""),
        "raw_cmd": raw_cmd,
        "exit_code": exit_code,
        "stderr_snippet": str(stderr_snippet or ""),
        "freshness": freshness,
        # best-effort extra fields (backward compatible)
        "chosen_strategy": str(chosen_strategy or ""),
        "attempts": atts,
    }


@dataclass
class ResolvedModelResult:
    """模型名解析结果（用于将用户输入映射到真实模型 ID，并做可用性校验）。"""

    tool_name: str
    input_name: str
    real_name: str
    source: str  # exact | friendly | prefix | partial | unknown | fallback
    validated: bool = False
    warnings: list[str] = field(default_factory=list)
