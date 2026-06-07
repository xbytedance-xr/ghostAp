# 基于视觉的 UI 自动化方案设计

> **Version**: 1.0  
> **Date**: 2026-06-03  
> **Author**: 孟川 (writer agent)  
> **Status**: Design Proposal

---

## 1. 核心理念

**"所见即所操"** — 不依赖 DOM/Accessibility Tree/View Hierarchy 等结构化接口，纯粹通过屏幕截图 + 多模态大模型理解 UI，实现跨平台、跨设备的通用自动化。

### 1.1 为什么选择纯视觉方案？

| 传统方案 | 视觉方案 |
|---------|---------|
| 依赖 Accessibility API、DOM、View ID | 仅需截图能力 |
| 每种平台单独适配 | 一套感知逻辑通吃所有平台 |
| UI 结构变动即失效 | 对 UI 改版天然鲁棒 |
| 无法处理 Canvas/游戏/远程桌面 | 有像素即可操作 |
| 需要应用配合（exported=true） | 无侵入，黑盒操作 |

### 1.2 设计目标

- **设备无关**：Android / iOS / Windows / macOS / Linux / Web / 远程桌面
- **零适配成本**：接入新应用无需编写选择器/定位器
- **自然语言驱动**：用户用自然语言描述任务，系统自主规划执行
- **可观测**：每一步有截图 + 决策日志，可回溯审计
- **可纠错**：执行失败自动检测并尝试恢复

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        User / Orchestrator                       │
│                  (自然语言任务描述 / API 调用)                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                      🧠 Brain Layer (大脑层)                      │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Task Planner │  │ Action Agent │  │   Reflection Engine    │ │
│  │  任务规划器   │  │  动作代理     │  │    反思/纠错引擎        │ │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬────────────┘ │
│         │                  │                      │               │
│         └──────────────────┼──────────────────────┘               │
│                            │                                      │
└────────────────────────────┼──────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     👁 Vision Layer (视觉层)                      │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Screen Cap  │  │  UI Parser   │  │   Grounding Engine     │ │
│  │  屏幕采集     │  │  UI 解析器   │  │    元素定位引擎         │ │
│  └──────────────┘  └──────────────┘  └────────────────────────┘ │
│                                                                   │
└────────────────────────────┼──────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   🎮 Control Layer (控制层)                       │
│                                                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ Android  │ │   iOS    │ │ Desktop  │ │   Web    │           │
│  │  (ADB)   │ │(WebDrvr) │ │(PyAuto)  │ │(Playwrt) │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 分层详细设计

### 3.1 Control Layer — 设备控制抽象层

所有设备统一为一个 `DeviceDriver` 接口：

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from PIL import Image


@dataclass
class Point:
    x: int
    y: int


@dataclass
class DeviceInfo:
    platform: str          # android | ios | windows | macos | linux | web
    screen_width: int
    screen_height: int
    device_name: str
    os_version: str


class DeviceDriver(ABC):
    """设备驱动统一接口 — 所有平台必须实现"""

    @abstractmethod
    async def screenshot(self) -> Image.Image:
        """截取当前屏幕"""
        ...

    @abstractmethod
    async def tap(self, point: Point) -> bool:
        """点击指定坐标"""
        ...

    @abstractmethod
    async def long_press(self, point: Point, duration_ms: int = 1000) -> bool:
        """长按"""
        ...

    @abstractmethod
    async def swipe(self, start: Point, end: Point, duration_ms: int = 300) -> bool:
        """滑动"""
        ...

    @abstractmethod
    async def type_text(self, text: str) -> bool:
        """输入文本（在当前焦点位置）"""
        ...

    @abstractmethod
    async def press_key(self, key: str) -> bool:
        """按键（Enter, Back, Home, etc.）"""
        ...

    @abstractmethod
    async def get_device_info(self) -> DeviceInfo:
        """获取设备信息"""
        ...

    # --- 可选高级能力 ---

    async def install_app(self, path: str) -> bool:
        raise NotImplementedError

    async def launch_app(self, package: str) -> bool:
        raise NotImplementedError

    async def get_clipboard(self) -> str:
        raise NotImplementedError

    async def set_clipboard(self, text: str) -> bool:
        raise NotImplementedError
```

#### 各平台实现方案

| 平台 | 截图方案 | 操作方案 | 依赖 |
|------|---------|---------|------|
| **Android** | `adb exec-out screencap -p` | `adb shell input tap/swipe/text` | adb |
| **iOS** | `idevicescreenshot` / WebDriverAgent | WDA HTTP API | libimobiledevice / WDA |
| **Windows** | `mss` / Win32 API | `pyautogui` / Win32 SendInput | pyautogui, pywin32 |
| **macOS** | `screencapture` / CGWindowListCreateImage | `cliclick` / CGEvent | cliclick, pyobjc |
| **Linux** | `xdotool` / Wayland screencopy | `xdotool` / `ydotool` | xdotool |
| **Web** | Playwright `page.screenshot()` | Playwright `page.click()` / `locator` | playwright |
| **远程桌面** | VNC/RDP framebuffer | VNC input protocol | asyncvnc |

### 3.2 Vision Layer — 视觉感知层

#### 3.2.1 Screen Capture Pipeline

```python
@dataclass
class ScreenState:
    image: Image.Image              # 原始截图
    timestamp: float                # 采集时间
    annotated_image: Optional[Image.Image] = None  # 标注后的图（带编号）
    elements: list["UIElement"] = None              # 解析出的 UI 元素
    ocr_text: Optional[str] = None                  # OCR 全文


@dataclass
class UIElement:
    id: int                    # 元素编号（标注在图上）
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2)
    center: Point              # 中心点坐标
    text: str                  # OCR 识别出的文字
    element_type: str          # button | input | text | icon | image | toggle | ...
    confidence: float          # 检测置信度
```

#### 3.2.2 UI Parsing Strategy — 三级感知策略

采用 **渐进式感知**，从低成本到高精度逐级调用：

```
Level 1: 纯 LLM 视觉（最简，适合明确任务）
    → 直接把截图发给多模态模型，让它输出动作坐标
    → 优点：零额外依赖；缺点：坐标精度有限

Level 2: OCR + 元素检测增强（推荐默认）
    → 先用轻量模型检测 UI 元素边界框 + OCR
    → 在截图上标注元素编号
    → 把标注图 + 元素列表一起给 LLM 决策
    → 优点：精度高、LLM 只需说"点击元素 #7"；缺点：多一步处理

Level 3: 结构化辅助（可选加速）
    → 在 Level 2 基础上，可选融合 Accessibility Tree（如果可用）
    → 仅作为补充信号，不依赖
```

#### 3.2.3 Grounding Engine — 元素定位引擎

核心问题：**LLM 说"点击登录按钮"，如何精确定位到屏幕坐标？**

```python
class GroundingEngine:
    """将 LLM 的语义指令映射到精确屏幕坐标"""

    async def ground(
        self,
        instruction: str,           # "点击登录按钮"
        screen_state: ScreenState,  # 当前屏幕状态
        method: str = "set_of_marks" # 定位策略
    ) -> Point:
        ...
```

**三种定位策略：**

| 策略 | 原理 | 精度 | 速度 |
|------|------|------|------|
| **Set-of-Marks (SoM)** | 在截图上标注编号框，LLM 输出编号 | ★★★★★ | ★★★★ |
| **Direct Coordinate** | LLM 直接输出 (x%, y%) 归一化坐标 | ★★★ | ★★★★★ |
| **Text Anchor** | OCR 找到目标文字，取其中心点 | ★★★★ | ★★★★ |

**推荐默认使用 Set-of-Marks**：在截图上绘制编号标注框，LLM 只需输出编号即可，避免坐标漂移。

### 3.3 Brain Layer — 智能决策层

#### 3.3.1 Task Planner — 任务规划器

将高层任务分解为可执行的步骤序列：

```python
class TaskPlanner:
    """
    输入: "帮我在淘宝搜索 iPhone 16 并加入购物车"
    输出: [
        Step("打开淘宝 App"),
        Step("点击搜索框"),
        Step("输入 'iPhone 16'"),
        Step("点击搜索"),
        Step("选择第一个商品"),
        Step("点击加入购物车"),
        Step("确认添加")
    ]
    """

    async def plan(self, task: str, context: TaskContext) -> list[Step]:
        # 使用 LLM 进行任务分解
        # 注入设备信息、当前屏幕状态、历史步骤
        ...
```

#### 3.3.2 Action Agent — 动作代理（核心循环）

```python
class ActionAgent:
    """
    核心 Observe-Think-Act 循环
    每一轮：截图 → 理解 → 决策 → 执行 → 验证
    """

    async def execute_step(self, step: Step, device: DeviceDriver) -> StepResult:
        for attempt in range(self.max_retries):
            # 1. OBSERVE: 获取当前屏幕状态
            screen = await self.vision.capture_and_parse(device)

            # 2. THINK: LLM 决策下一步动作
            action = await self.decide_action(step, screen)

            # 3. ACT: 执行动作
            success = await self.execute_action(action, device)

            # 4. VERIFY: 等待 UI 响应，再次截图验证
            await asyncio.sleep(action.wait_after_ms / 1000)
            new_screen = await self.vision.capture_and_parse(device)

            # 5. CHECK: 验证是否达到预期状态
            if await self.verify_step_complete(step, new_screen):
                return StepResult(success=True, screenshots=[screen, new_screen])

            # 未完成则进入下一次尝试（Reflection Engine 会分析原因）
            ...

        return StepResult(success=False, error="Max retries exceeded")
```

#### 3.3.3 Reflection Engine — 反思纠错引擎

```python
class ReflectionEngine:
    """
    当动作执行后状态不符合预期时：
    1. 分析失败原因（弹窗遮挡？元素不可见？需要滚动？）
    2. 生成恢复策略
    3. 更新任务计划
    """

    async def analyze_failure(
        self,
        expected: str,          # "应该看到搜索结果页"
        actual: ScreenState,    # 实际看到的
        history: list[Action]   # 执行历史
    ) -> RecoveryPlan:
        # 常见恢复模式：
        # - 弹窗/广告: 关闭弹窗后重试
        # - 加载中: 等待更长时间
        # - 元素不在视野: 滚动后重试
        # - 页面跳转异常: 返回上一步
        # - 权限弹窗: 点击允许
        ...
```

---

## 4. Prompt 工程设计

### 4.1 Action Agent System Prompt

```markdown
你是一个 UI 自动化 Agent。你通过观察屏幕截图来理解当前界面状态，并决定下一步操作。

## 输入
- 当前屏幕截图（已标注元素编号）
- 元素列表（编号、文字、类型、坐标）
- 当前任务目标
- 已执行步骤历史

## 输出格式（严格 JSON）
{
  "thought": "分析当前屏幕状态，思考下一步",
  "action": {
    "type": "tap|type|swipe|press_key|wait|done|fail",
    "target": 7,              // 元素编号（tap/long_press 时必填）
    "text": "hello",          // type 时必填
    "direction": "up",        // swipe 时可选
    "key": "back",            // press_key 时必填
    "wait_after_ms": 1000     // 动作后等待时间
  },
  "expected_result": "预期执行后屏幕应该出现什么变化"
}

## 规则
1. 每次只输出一个动作
2. 优先使用元素编号定位，不要猜测坐标
3. 如果目标元素不在当前屏幕，先滚动
4. 遇到弹窗先处理弹窗
5. 不确定时选择 "wait" 观察
6. 任务完成时输出 type="done"
7. 确认无法完成时输出 type="fail" 并说明原因
```

### 4.2 Task Planner Prompt

```markdown
你是一个任务规划 Agent。用户给你一个高层目标，你需要分解为具体的 UI 操作步骤。

## 设备信息
- 平台: {platform}
- 屏幕: {width}x{height}
- 当前 App: {current_app}

## 规则
1. 每步描述要具体、可验证
2. 考虑可能的异常情况（加载慢、弹窗、权限）
3. 不要假设界面布局，保持步骤的灵活性
4. 输出 JSON 数组格式
```

---

## 5. 关键技术决策

### 5.1 模型选型策略

| 环节 | 推荐模型 | 备选 | 理由 |
|------|---------|------|------|
| **UI 解析 (SoM)** | OmniParser v2 / GroundingDINO | Florence-2 | 专为 UI 元素检测训练，轻量高效 |
| **OCR** | PaddleOCR / EasyOCR | Tesseract | 中英文混合场景表现好 |
| **Action 决策** | GPT-5.5 / Claude Opus 4.7 | Qwen-VL-Max | 需要强推理+视觉理解 |
| **Task Planning** | GPT-5.5 / Claude Opus 4.7 | DeepSeek-R1 | 需要强逻辑推理 |
| **轻量验证** | Qwen-VL-Chat / InternVL | — | 仅判断是否成功，小模型够用 |

### 5.2 性能优化策略

```
┌─────────────────────────────────────────┐
│           性能关键路径优化                 │
├─────────────────────────────────────────┤
│                                          │
│  1. 截图压缩：1080p → 720p（保持可读性） │
│     → 减少 LLM token 消耗 50%+           │
│                                          │
│  2. 增量感知：仅当屏幕变化超过阈值时       │
│     重新解析 UI 元素                      │
│     → SSIM 对比，变化 < 5% 跳过解析       │
│                                          │
│  3. 动作缓存：相同屏幕 + 相同目标          │
│     → 直接复用上次决策                    │
│                                          │
│  4. 并行处理：截图的同时预处理上一帧       │
│     → Pipeline 并行减少等待               │
│                                          │
│  5. 本地小模型：UI 检测/OCR 本地运行       │
│     → 仅决策走云端大模型                  │
│                                          │
└─────────────────────────────────────────┘
```

### 5.3 安全与隐私设计

| 风险 | 缓解措施 |
|------|---------|
| 截图包含敏感信息 | 可配置敏感区域遮罩；截图不持久化或加密存储 |
| 自动操作误操作 | 危险操作（支付/删除/发送）前强制人工确认 |
| 密码输入 | 通过 SecureInput 通道传入，不经过截图 + LLM |
| 账号安全 | 不截取/存储登录凭据；使用设备本地 Keychain |
| 操作越权 | 任务粒度权限控制；敏感 App 白名单机制 |

---

## 6. 完整执行流程

```
用户: "帮我在美团上点一杯拿铁外卖送到公司"

                    ┌─────────────────────┐
                    │   Task Planner      │
                    │   分解为 8 个步骤     │
                    └──────────┬──────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
   Step 1: 打开美团      Step 2: 进入外卖      Step 3: 搜索"拿铁"
          │                    │                    │
          ▼                    ▼                    ▼
   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
   │ Screenshot  │     │ Screenshot  │     │ Screenshot  │
   │ → Parse UI  │     │ → Parse UI  │     │ → Parse UI  │
   │ → LLM 决策  │     │ → LLM 决策  │     │ → LLM 决策  │
   │ → 执行 Tap  │     │ → 执行 Tap  │     │ → Type+Tap  │
   │ → 验证成功  │     │ → 验证成功  │     │ → 验证成功  │
   └─────────────┘     └─────────────┘     └─────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
   Step 4: 选择店铺      Step 5: 选拿铁       Step 6: 加入购物车
          │                    │                    │
          ▼                    ▼                    ▼
   Step 7: 确认地址       Step 8: 提交订单 ← ⚠️ 人工确认 (支付操作)
                                    │
                                    ▼
                            ✅ 任务完成，返回结果
```

---

## 7. 技术栈推荐

```yaml
# 核心框架
language: Python 3.11+
async_runtime: asyncio + uvloop
package_manager: uv

# 视觉处理
screenshot: mss (desktop) / adb (android) / playwright (web)
ui_detection: OmniParser-v2 (ONNX Runtime)
ocr: PaddleOCR
image_processing: Pillow + OpenCV

# LLM 集成
llm_client: litellm (统一多模型接口)
supported_models:
  - openai/gpt-5.5
  - anthropic/claude-opus-4.7
  - qwen/qwen-vl-max
  - local/internvl2-8b (本地备选)

# 设备控制
android: adb-shell + scrcpy (实时流)
ios: tidevice + WebDriverAgent
desktop_win: pyautogui + pywin32
desktop_mac: pyobjc + cliclick
web: playwright

# 工程支撑
config: pydantic-settings
logging: structlog
testing: pytest + pytest-asyncio
state_machine: transitions (任务状态管理)
queue: asyncio.Queue (步骤队列)
```

---

## 8. 项目结构设计

```
vision_automation/
├── __init__.py
├── core/
│   ├── __init__.py
│   ├── agent.py              # ActionAgent 主循环
│   ├── planner.py            # TaskPlanner 任务规划
│   ├── reflection.py         # ReflectionEngine 纠错
│   └── types.py              # 核心数据结构
├── vision/
│   ├── __init__.py
│   ├── capture.py            # ScreenCapture 统一采集
│   ├── parser.py             # UIParser 元素解析
│   ├── grounding.py          # GroundingEngine 定位
│   ├── annotator.py          # Set-of-Marks 标注器
│   └── ocr.py                # OCR 引擎封装
├── drivers/
│   ├── __init__.py
│   ├── base.py               # DeviceDriver ABC
│   ├── android.py            # ADB Driver
│   ├── ios.py                # iOS/WDA Driver
│   ├── desktop_win.py        # Windows Driver
│   ├── desktop_mac.py        # macOS Driver
│   ├── desktop_linux.py      # Linux Driver
│   ├── web.py                # Playwright Driver
│   └── vnc.py                # VNC Remote Driver
├── llm/
│   ├── __init__.py
│   ├── client.py             # LLM 统一接口
│   ├── prompts.py            # Prompt 模板管理
│   └── parsers.py            # LLM 输出解析器
├── safety/
│   ├── __init__.py
│   ├── sensitive_guard.py    # 敏感操作拦截
│   ├── privacy_mask.py       # 隐私区域遮罩
│   └── rate_limiter.py       # 操作频率限制
├── memory/
│   ├── __init__.py
│   ├── session.py            # 会话状态管理
│   ├── history.py            # 步骤历史记录
│   └── pattern_cache.py      # 常见模式缓存
├── server/
│   ├── __init__.py
│   ├── api.py                # HTTP API 入口
│   └── ws.py                 # WebSocket 实时流
└── config.py                 # 配置管理
```

---

## 9. MVP 路线图

### Phase 1: Android 单设备 (2-3 周)

- [x] DeviceDriver 接口定义
- [ ] ADB Driver 实现（截图 + 基础操作）
- [ ] PaddleOCR 集成
- [ ] 基础 ActionAgent 循环（截图 → LLM → 执行）
- [ ] 文本锚点定位（不依赖 UI 检测模型）
- [ ] 3 个端到端用例验证

### Phase 2: 视觉增强 (2 周)

- [ ] OmniParser UI 元素检测集成
- [ ] Set-of-Marks 标注器
- [ ] GroundingEngine 多策略支持
- [ ] ReflectionEngine 基础纠错
- [ ] 成功率从 ~60% → ~85%

### Phase 3: 多平台扩展 (3 周)

- [ ] Web (Playwright) Driver
- [ ] Desktop (Windows/macOS) Driver
- [ ] TaskPlanner 复杂任务分解
- [ ] 会话记忆与模式缓存

### Phase 4: 生产化 (2 周)

- [ ] 安全防护层完善
- [ ] HTTP/WS API 服务化
- [ ] 与 GhostAP 飞书 Bot 集成
- [ ] 监控、日志、可观测性
- [ ] 性能优化（增量感知、并行 Pipeline）

---

## 10. 与 GhostAP 集成方案

作为 GhostAP 的新执行策略 (`UIAutomationEngine`)，通过飞书 Bot 接收指令：

```
用户在飞书发送: /ui 帮我在手机上打开设置，连接蓝牙耳机

GhostAP 路由:
  → UIAutomationEngine.start(task="打开设置，连接蓝牙耳机", device="android")
  → 实时推送执行截图到飞书卡片
  → 遇到确认点请求用户审批
  → 完成后发送结果摘要
```

飞书卡片实时展示：
- 当前步骤进度条
- 每步执行截图（缩略图）
- 当前 Agent 思考过程
- 确认按钮（敏感操作时）

---

## 11. 竞品对比与差异化

| 特性 | 本方案 | Anthropic Computer Use | OpenAI CUA | Microsoft UFO |
|------|--------|----------------------|------------|---------------|
| 设备覆盖 | 全平台 | 仅桌面 | 仅桌面+Web | 仅 Windows |
| 开源 | ✅ | ❌ | ❌ | ✅ |
| 移动端 | ✅ Android+iOS | ❌ | ❌ | ❌ |
| 模型无关 | ✅ 任意模型 | 仅 Claude | 仅 GPT | 仅 GPT |
| 本地部署 | ✅ | ❌ | ❌ | ✅ |
| 中文优化 | ✅ 原生 | 一般 | 一般 | 一般 |
| 即时通讯集成 | ✅ 飞书 | ❌ | ❌ | ❌ |

**核心差异化：**
1. **移动设备原生支持** — 竞品几乎都不支持手机操控
2. **模型无关** — 不绑定任何单一 LLM 供应商
3. **飞书集成** — 通过 IM 即可远程操控任何设备
4. **中文场景深度优化** — OCR、Prompt、常见 App 模式库

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解策略 |
|------|------|---------|
| LLM 坐标输出不精确 | 点击偏移 | 默认使用 SoM 编号方案避免坐标问题 |
| 动画/加载导致截图时机不对 | 误判 UI 状态 | 截图前等待 + 屏幕稳定检测（连续2帧 SSIM > 0.95） |
| API 成本高（每步都调用 LLM） | 成本失控 | 本地小模型处理简单场景；缓存重复模式；限制单任务最大步数 |
| 复杂任务步数过多超时 | 任务失败 | 动态规划 + 跳步优化；设置合理超时并保存断点 |
| 设备连接不稳定 | 执行中断 | 心跳检测 + 自动重连 + 断点续执 |

---

## 总结

本方案的核心创新在于：

1. **纯视觉驱动** — 不依赖任何平台特有 API，真正实现"有屏幕就能操作"
2. **三级感知架构** — 从轻量到精确渐进式调用，平衡成本与精度
3. **Observe-Think-Act-Verify 闭环** — 每步自验证，失败自纠错
4. **全设备覆盖** — 统一抽象层 + 平台特化驱动，一套逻辑跑所有设备
5. **安全优先** — 敏感操作门控、隐私遮罩、操作审计日志

MVP 建议从 **Android + 单一大模型** 起步，验证核心循环可行后再横向扩展平台、纵向优化精度。
