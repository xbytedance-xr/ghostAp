# ACP Image Card Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make images produced by ACP agents and image/screenshot tools appear inline in GhostAP's Feishu task cards while the task is running.

**Architecture:** Preserve ACP media as a typed `ACPImageInfo` event, publish its validated bytes through the existing official `lark-oapi` client, and dispatch only the returned Feishu `image_key` into immutable card state. A shared media bridge is used by ordinary programming mode and Deep/Spec stream processors, so render code stays pure and transport credentials or binary payloads never enter `CardState`.

**Tech Stack:** Python 3.13, `agent-client-protocol`, `lark-oapi`, immutable CardSession reducers, Feishu Card Schema 2.0, pytest.

## Global Constraints

- Use only `uv`; do not use pip or conda.
- Use the official `lark-oapi` image upload API; do not hand-write HTTP calls.
- Keep the dependency direction `handler -> session -> render` and `session -> delivery`.
- Never store base64 image data in card state, rendered JSON, logs, or `.Memory`.
- Accept only validated raster image MIME types and at most 10 MiB decoded data.
- Do not fetch remote resource URLs; local resource links must resolve inside the ACP session root.
- Preserve unrelated dirty worktree changes.

---

### Task 1: Preserve ACP image output as typed events

**Files:**
- Modify: `src/acp/models.py`
- Modify: `src/acp/client.py`
- Modify: `src/acp/__init__.py`
- Test: `tests/test_acp_client.py`

**Interfaces:**
- Produces: `ACPImageInfo(image_id, mime_type, data, name, source_uri)` and `ACPEventType.IMAGE_CHUNK`.
- Produces: one image event for `AgentMessageChunk`/`AgentThoughtChunk` image content and each image nested in tool-call content.
- Consumes: ACP `ImageContentBlock`, image `BlobResourceContents`, and local image `ResourceContentBlock`.

- [x] **Step 1: Write failing direct-image and tool-image tests**

```python
async def test_agent_image_chunk_emits_typed_image_event():
    events = []
    client = GhostAPClient(events.append, root_dir=".")
    await client.session_update("s1", AgentMessageChunk(
        sessionUpdate="agent_message_chunk",
        content=ImageContentBlock(type="image", data=PNG_B64, mimeType="image/png"),
    ))
    assert events[0].event_type is ACPEventType.IMAGE_CHUNK
    assert events[0].image.mime_type == "image/png"

async def test_tool_content_image_emits_image_after_tool_event():
    ...
    assert [event.event_type for event in events] == [
        ACPEventType.TOOL_CALL_DONE,
        ACPEventType.IMAGE_CHUNK,
    ]
```

- [x] **Step 2: Run the tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_acp_client.py -q`

Expected: FAIL because `ACPEventType.IMAGE_CHUNK` and `ACPImageInfo` do not exist.

- [x] **Step 3: Implement bounded ACP media parsing**

```python
@dataclass(frozen=True)
class ACPImageInfo:
    image_id: str
    mime_type: str
    data: str
    name: str = "任务图片"
    source_uri: str | None = None

class ACPEventType(Enum):
    IMAGE_CHUNK = "image_chunk"
```

Normalize and validate base64 before emitting the event. Resolve only `file:` or plain local resource paths with `_safe_resolve_path`; reject remote URLs and unsupported MIME types.

- [x] **Step 4: Run the tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_acp_client.py -q`

Expected: PASS.

### Task 2: Upload validated image bytes through the official Feishu SDK

**Files:**
- Modify: `src/feishu/im_client.py`
- Modify: `src/feishu/handlers/base.py`
- Test: `tests/test_im_client_sanitize.py`

**Interfaces:**
- Consumes: `ACPImageInfo.data` and `ACPImageInfo.mime_type`.
- Produces: `FeishuIMClient.upload_image_bytes(image_bytes, image_type="message") -> str | None`.
- Produces: `BaseHandler.upload_acp_image(image: ACPImageInfo) -> str | None`.

- [x] **Step 1: Write the failing SDK request test**

```python
def test_upload_image_bytes_returns_image_key():
    image_api = MagicMock()
    image_api.create.return_value = _Response(
        data=MagicMock(image_key="img_generated")
    )
    ...
    assert client.upload_image_bytes(PNG_BYTES) == "img_generated"
    request = image_api.create.call_args.args[0]
    assert request.request_body.image_type == "message"
```

- [x] **Step 2: Run the test and verify RED**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_im_client_sanitize.py -q`

Expected: FAIL because `upload_image_bytes` does not exist.

- [x] **Step 3: Implement the official SDK upload**

```python
with io.BytesIO(image_bytes) as image_file:
    request = CreateImageRequest.builder().request_body(
        CreateImageRequestBody.builder()
        .image_type(image_type)
        .image(image_file)
        .build()
    ).build()
    response = self._execute_with_retry(
        lambda: client.im.v1.image.create(request),
        "上传图片",
        max_retries,
    )
```

- [x] **Step 4: Run the test and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_im_client_sanitize.py -q`

Expected: PASS.

### Task 3: Add immutable image state and pure Card Schema rendering

**Files:**
- Modify: `src/card/events/types.py`
- Modify: `src/card/events/payloads.py`
- Modify: `src/card/events/factories.py`
- Modify: `src/card/events/__init__.py`
- Create: `src/card/state/reducers/image.py`
- Modify: `src/card/state/models.py`
- Modify: `src/card/state/reducer.py`
- Modify: `src/card/render/atoms.py`
- Modify: `src/card/render/renderer.py`
- Test: `tests/test_card_reducers.py`
- Test: `tests/test_card_renderer.py`

**Interfaces:**
- Produces: `CardEvent.image_added(image_id, image_key, alt)` and `CardEvent.image_failed(image_id, alt)`.
- Produces: frozen `ImageBlock(kind="image", image_key, alt, status)`.
- Produces: Feishu Schema 2.0 `{"tag": "img", "img_key": ..., "alt": ...}`.

- [x] **Step 1: Write reducer and renderer tests**

```python
def test_image_added_creates_one_deduplicated_image_block():
    state = reduce_card_state(None, CardEvent.image_added("sha256:1", "img_1", "截图"))
    state = reduce_card_state(state, CardEvent.image_added("sha256:1", "img_1", "截图"))
    assert len([block for block in state.blocks if block.kind == "image"]) == 1

def test_image_block_renders_inline_feishu_image():
    ...
    assert {"tag": "img", "img_key": "img_1", ...} in body
```

- [x] **Step 2: Run the tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_card_reducers.py tests/test_card_renderer.py -q`

Expected: FAIL because image events and `ImageBlock` are missing.

- [x] **Step 3: Implement the reducer and render atom**

```python
@dataclass(frozen=True)
class ImageBlock:
    _atom_kind: ClassVar[str] = "image"
    kind: Literal["image"] = "image"
    block_id: str = ""
    image_key: str | None = None
    alt: str = "任务图片"
    status: BlockStatus = "completed"
```

Render successful blocks as `img`; render failed blocks as a short non-sensitive Markdown fallback. Treat image events as structural so CardKit performs an entity update.

- [x] **Step 4: Run the tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_card_reducers.py tests/test_card_renderer.py -q`

Expected: PASS.

### Task 4: Bridge media into ordinary programming and Deep/Spec streams

**Files:**
- Create: `src/card/media_bridge.py`
- Modify: `src/card/programming_adapter.py`
- Modify: `src/card/stream_bridge.py`
- Modify: `src/feishu/handlers/programming.py`
- Modify: `src/feishu/renderers/_base_stream_processor.py`
- Test: `tests/test_programming_card_session.py`
- Test: `tests/test_stream_bridge.py`

**Interfaces:**
- Produces: `ACPImagePublisher(dispatchable, image_uploader)` with `handle(event) -> bool` and `bind(dispatchable)`.
- Consumes: `Callable[[ACPImageInfo], str | None]`.
- Guarantees: image IDs are uploaded and dispatched at most once per task stream.

- [x] **Step 1: Write failing ordinary and engine bridge tests**

```python
def test_programming_session_uploads_and_renders_image_event_once():
    uploader = MagicMock(return_value="img_1")
    session = ProgrammingCardSession(card_session, image_uploader=uploader)
    session.on_event(ACPEvent(event_type=ACPEventType.IMAGE_CHUNK, image=IMAGE))
    session.on_event(ACPEvent(event_type=ACPEventType.IMAGE_CHUNK, image=IMAGE))
    uploader.assert_called_once_with(IMAGE)

def test_stream_bridge_dispatches_uploaded_image():
    bridge = ACPStreamBridge(dispatchable, image_uploader=lambda _: "img_1")
    bridge.on_event(image_event)
    assert dispatchable.events[-1].type is CardEventType.IMAGE_ADDED
```

- [x] **Step 2: Run the tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_programming_card_session.py tests/test_stream_bridge.py -q`

Expected: FAIL because the upload callback and publisher do not exist.

- [x] **Step 3: Implement and wire the shared media publisher**

```python
class ACPImagePublisher:
    def handle(self, event: ACPEvent) -> bool:
        if event.event_type is not ACPEventType.IMAGE_CHUNK:
            return False
        image_key = self._image_uploader(event.image) if self._image_uploader else None
        self._dispatchable.dispatch(
            CardEvent.image_added(event.image.image_id, image_key, event.image.name)
            if image_key else CardEvent.image_failed(event.image.image_id, event.image.name)
        )
        return True
```

Use a lock plus seen/in-flight IDs to prevent duplicate uploads. Flush pending text before ordinary programming image publication. Rebind the publisher when an engine card rotates.

- [x] **Step 4: Run the tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest tests/test_programming_card_session.py tests/test_stream_bridge.py -q`

Expected: PASS.

### Task 5: Validate, document, and review

**Files:**
- Create: `ux/programming-image-artifacts.html`
- Modify: `.Memory/2026-07-24.md`
- Modify: `.Memory/Abstract.md`

**Interfaces:**
- Produces: a visual preview for running, completed, and upload-failure states.
- Produces: durable project decision and verification evidence.

- [x] **Step 1: Run focused regression tests**

Run:

```bash
UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest \
  tests/test_acp_client.py \
  tests/test_im_client_sanitize.py \
  tests/test_card_events.py \
  tests/test_card_reducers.py \
  tests/test_card_renderer.py \
  tests/test_programming_card_session.py \
  tests/test_stream_bridge.py -q
```

Expected: all pass with no new warnings.

- [x] **Step 2: Run expanded card and handler tests**

Run:

```bash
UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m pytest \
  tests/test_card_session.py \
  tests/test_handlers.py \
  tests/test_lark_channel_card_api_client.py \
  tests/test_deep_renderer.py \
  tests/test_spec_renderer.py -q
```

Expected: all pass.

- [x] **Step 3: Run static validation**

Run:

```bash
UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run ruff check src/acp src/card src/feishu tests
UV_CACHE_DIR=/tmp/ghostap-uv-cache uv run python -m src.main --validate
git diff --check
```

Expected: all commands succeed.

- [x] **Step 4: Self-review the final diff**

Verify:

- No base64 payload or image bytes are logged, stored in CardState, or emitted in card JSON.
- Images remain bounded, local resource resolution cannot escape the ACP root, and remote URLs are not fetched.
- Existing dirty execution-flow changes remain intact.
- Upload failure is visible but does not fail the parent task.
