# AI Agent 产品调研报告：OpenClaw、Hermes 及同类系统

日期：2026-05-23

## 1. 结论摘要

本报告把“同类 AI 产品”限定为：能以自然语言接收任务、调用工具、读写文件或运行命令、持续维护上下文，并能通过 CLI、IDE、消息平台、云端任务队列或工作流系统交付结果的 agent 产品。按这个定义，OpenClaw 和 Hermes 不是单纯的“代码助手”，而是更接近 agent 运行时、消息入口和个人/团队自动化控制面的产品。

核心判断：

1. OpenClaw 的核心差异是“多渠道个人 AI 助理 Gateway”。它最强的是 WhatsApp、Telegram、Slack、Discord、Feishu、WeChat、Teams、Signal、iMessage 等入口统一、移动端/语音/Canvas/会话路由以及本地优先部署。它适合做个人或小团队的 AI 操作系统入口，但安全面和运维面都很大。
2. Hermes 的核心差异是“CLI/TUI-first + 长期记忆 + 技能自改进”。它更像一个会在终端里长期工作的开发/研究 agent，同时提供消息网关、技能、记忆、计划任务和多后端运行环境。它不追求 OpenClaw 那种最宽渠道覆盖，而是强调个人 agent 随使用变强。
3. Codex、Claude Code、Gemini CLI、OpenCode、Aider、Cline 是更典型的“开发者 coding agent”。其中 Codex 和 Claude Code 更适合生产级复杂代码任务，Gemini CLI/OpenCode/Aider 更适合低门槛、本地、BYOK、可脚本化或开源场景，Cline 更适合 IDE 内 human-in-the-loop。
4. Devin、GitHub Copilot cloud agent、Jules、Codex Web/Cloud 属于“云端异步 PR agent”。它们更适合团队 backlog、issue、PR、迁移和重复工程任务，但可控性、成本、数据边界和平台绑定要重点评估。
5. Cursor、Windsurf、Kiro、Replit Agent 属于“IDE/应用构建平台”。它们对个人开发效率和从 0 到 1 原型很强，但通常不像 OpenClaw/Hermes 那样适合做独立 agent Gateway，也不像 OpenHands 那样适合被当成底层 agent SDK。
6. OpenHands、Cline SDK、部分 OpenCode 能作为 agent 基础设施参考。若目标是构建 GhostAP 这类 Feishu/Lark bot agent 平台，最值得借鉴的是 OpenClaw 的多渠道 Gateway 与安全配置、Hermes 的记忆/技能生命周期、Codex/Claude 的执行质量、Copilot/Devin/Jules 的异步任务与 PR 审核流。

一句话选型：

- 要“手机上随时操控个人 AI 助理”：优先看 OpenClaw。
- 要“长期陪伴、会沉淀技能和记忆的开发 agent”：优先看 Hermes。
- 要“生产代码修改质量”：优先看 Codex 或 Claude Code。
- 要“开源、BYOK、能嵌入或改造”：看 OpenCode、Cline SDK、OpenHands、Aider。
- 要“团队异步消化 backlog/issue/PR”：看 Devin、GitHub Copilot cloud agent、Jules、Codex Cloud。
- 要“非工程用户快速出应用”：看 Replit Agent；要“结构化 spec-driven IDE”：看 Kiro。

## 2. 市场分层

### 2.1 多渠道个人/团队 agent Gateway

代表：OpenClaw、Hermes、GhostAP。

这类产品的重点不是单次代码补全，而是把 LLM、工具、记忆、会话状态和消息入口组合成一个常驻服务。典型能力包括：消息平台接入、用户/群组授权、任务会话、工具调用、代码/命令执行、长期上下文、技能系统、远程访问和审计。

核心挑战：入口越多，prompt injection、身份冒用、权限误配、secret 泄露、工具逃逸和运维错误越容易发生。

### 2.2 本地 CLI/TUI coding agent

代表：Codex CLI、Claude Code、Gemini CLI、OpenCode、Aider、Hermes。

这类产品适合开发者直接在 repo 中使用，优势是接近真实工程环境，能跑测试、改文件、读 git diff，也便于与 CI、脚本和 shell 工作流结合。

核心挑战：长任务可靠性、上下文管理、权限审批、跨文件正确性、测试验证和成本控制。

### 2.3 IDE-native agent

代表：Cursor、Windsurf Cascade、Cline、Roo Code、Kiro、GitHub Copilot agent mode。

优势是低摩擦、可视 diff、上下文来自编辑器、适合边写边审。弱点是容易被 IDE 产品形态束缚，难做真正跨环境、跨消息平台的 always-on agent。

### 2.4 云端异步 PR/任务 agent

代表：Devin、GitHub Copilot cloud agent、Jules、Codex Web/Cloud、Claude Code Web/Cloud。

它们更像“把任务交给云端工程师”，重点是 clone repo、开分支、跑测试、产出 PR 或 review。适合团队 backlog 和重复工程任务，弱点是平台绑定、数据出境、CI 权限、成本和人工审核依赖。

### 2.5 Agent 框架/SDK

代表：OpenHands、Cline SDK、部分 OpenCode/Codex SDK 能力。

适合被二次开发或嵌入到已有平台。选型重点是 API 边界、可观测性、可替换模型、执行沙箱、权限模型、状态模型和测试工具链。

## 3. 评估维度

本报告使用以下维度评估：

| 维度 | 说明 |
|---|---|
| 产品定位 | 面向个人、开发者、团队、企业，还是框架开发者 |
| 入口形态 | CLI、TUI、IDE、Web、移动端、消息平台、API |
| 执行环境 | 本机、Docker/SSH sandbox、云 VM、托管 IDE、浏览器环境 |
| 任务能力 | 代码修改、测试、浏览器、文档、PR、计划任务、多 agent |
| 模型策略 | 固定模型、订阅绑定、BYOK、多模型、多 provider、本地模型 |
| 记忆与技能 | 是否支持长期记忆、项目规则、技能市场、可复用工作流 |
| 安全治理 | 身份授权、沙箱、审批、allowlist、secret 管理、审计 |
| 可扩展性 | MCP、插件、SDK、技能、脚本、API |
| 团队协作 | issue/PR、Slack/Teams/Feishu、多人任务、权限和账单 |
| 成本结构 | 订阅、token、自托管运维、云端 runner、GitHub Actions minutes |
| 成熟度 | 文档、release、社区、已知风险、企业功能 |

评分说明：后文评分是基于公开文档和产品形态的工程判断，不是基准测试结果。

## 4. 重点产品深度分析

## 4.1 OpenClaw

### 定位

OpenClaw 是本地优先、自托管的个人 AI 助理和多渠道 Gateway。官方 GitHub README 把它描述为运行在自己设备上的 personal AI assistant，Gateway 是控制面，产品本体是 assistant。官方文档强调支持大量消息渠道、模型无关、技能系统、移动节点、语音和 Canvas。

### 关键能力

- 多渠道入口：WhatsApp、Telegram、Slack、Discord、Google Chat、Signal、iMessage、Microsoft Teams、Matrix、Feishu、LINE、Mattermost、Nextcloud Talk、Nostr、Twitch、Zalo、WeChat、QQ、WebChat 等。
- 本地 Gateway：单进程/daemon 维护会话、路由、工具和事件。
- 多 agent 路由：按 channel、account、peer、workspace 或 agent 隔离会话。
- 模型无关：官方资料称可使用 Claude、GPT、Gemini、Llama、Mistral、Ollama 等。
- 技能系统：ClawHub/skills，Markdown 格式技能，适合把重复流程产品化。
- 移动/语音/Canvas：移动节点、wake word、talk mode、live Canvas、系统节点。
- 安全配置：DM pairing、allowlist、sandbox mode、tool policy、doctor/explain。

### 优点

1. 入口覆盖极强。它把“用户已经使用的聊天工具”变成 agent UI，这点比单纯 CLI/IDE 更接近个人 AI 助理。
2. 本地优先，数据控制权强。对希望自管 API key、会话和工具权限的个人/团队有吸引力。
3. 平台化程度高。Gateway、channel plugin、skills、sessions、nodes、Canvas、cron 组合后，产品边界比 coding agent 更宽。
4. 对 GhostAP 类 Feishu bot 很有参考价值。尤其是多渠道抽象、DM pairing、session routing、sandbox explain、tool policy 这些设计。
5. 生态声量大。GitHub 页面在本次调研时显示非常高的 star/fork 数量，说明关注度和社区供给充足。

### 缺点

1. 攻击面大。多渠道消息、技能市场、浏览器、系统节点、cron、shell、文件读写都叠加在一起，默认配置和运维错误的风险显著高于单一 CLI。
2. 主会话默认 host 权限强。官方 README 明确提醒 main session 工具默认跑在 host 上，用户需要主动配置 sandbox 和 tool policy。
3. 自托管运维成本高。Node 版本、daemon、channel token、移动节点、Docker sandbox、远程访问、消息平台 webhook/权限都需要维护。
4. 技能供应链风险高。任何“社区技能市场 + 工具执行”都会面临恶意技能、prompt injection 和 secret 泄露风险，需要企业级治理。
5. 产品复杂度可能过高。对于只想写代码的开发者，OpenClaw 的 channel/node/canvas/voice 能力可能是负担。

### 适用场景

- 个人 AI 助理：消息、日程、邮件、代码、浏览器、远程机器操作集中入口。
- 小团队内部 bot：统一接入 Slack/Feishu/Discord/Telegram，串起常见开发任务。
- 需要多渠道 Gateway 的平台型产品：OpenClaw 是最直接的竞品/参考对象。

### 不适用场景

- 只需要 IDE 补全或本地代码修改。
- 高合规企业且无法接受自托管 agent 直接接触大量工具和 secret。
- 缺少平台运维能力的小团队。

## 4.2 Hermes Agent

### 定位

Hermes 是 Nous Research 推出的自改进 agent。官方 README 的重点是：内置学习闭环、从经验创建技能、使用中改进技能、长期记忆、跨会话搜索、自我建模，以及 CLI/TUI、消息网关、计划任务、子 agent、远程/云端 terminal backend。

### 关键能力

- CLI/TUI：Hermes 的 TUI 是官方推荐交互方式，支持多行编辑、slash 命令补全、历史会话、interrupt、streaming tool output。
- 消息网关：Telegram、Discord、Slack、WhatsApp、Signal、Email 等。
- 长期记忆：agent-curated memory、用户画像、FTS5 会话搜索、摘要召回。
- 技能系统：任务后自动沉淀技能，技能可在使用中改进，兼容 agentskills.io 标准。
- 多模型：Nous Portal、OpenRouter、NovitaAI、NVIDIA NIM、Moonshot/Kimi、MiniMax、Hugging Face、OpenAI、自定义 endpoint 等。
- 自动化：内置 cron scheduler，把自然语言任务定期投递到任意平台。
- 多执行后端：local、Docker、SSH、Singularity、Modal、Daytona、Vercel Sandbox。
- OpenClaw 迁移：官方支持导入 OpenClaw 的 settings、memories、skills、API keys、workspace instructions 等。

### 优点

1. 记忆和技能设计强。Hermes 的差异不是“又一个终端 agent”，而是把长期使用中的经验沉淀成可复用技能。
2. 开发/研究工作流更自然。CLI/TUI、session、slash commands、interrupt、tool output 比聊天入口更适合工程师持续工作。
3. 可迁移、可部署范围广。从本机到 VPS、Docker、SSH、Modal、Daytona、Vercel Sandbox，覆盖个人和轻量云端场景。
4. 模型开放度高。多 provider + 自定义 endpoint，避免单一模型锁定。
5. 与 OpenClaw 形成互补。OpenClaw 偏 channel gateway，Hermes 偏 agent cognition/memory/skills。

### 缺点

1. 自改进能力需要长期验证。自动生成/改写技能可能带来漂移、错误固化、隐性 prompt injection 和维护成本。
2. 消息渠道覆盖弱于 OpenClaw。Hermes 有 gateway，但不是主打 50+ channel 的全入口平台。
3. 复杂度仍高。记忆、技能、tools、model、gateway、cron、terminal backend 同时存在，需要良好的审计和备份。
4. Windows 原生能力仍有 beta/兼容性边界。官方建议更稳妥的 Windows 路径仍是 WSL2。
5. 企业治理能力需要核验。公开资料更偏开发者/个人使用，企业级 RBAC、审计、策略中心、合规数据边界需要实测。

### 适用场景

- 长期使用的个人开发 agent。
- 研究/自动化/知识工作 agent，需要记忆和技能沉淀。
- VPS/云端常驻 agent，通过 Telegram/Slack 远程交互。
- 想从 OpenClaw 迁移到更“终端/记忆/技能”导向系统的用户。

### 不适用场景

- 主要诉求是接入尽可能多的聊天渠道。
- 需要稳定企业托管和强合规 SLA。
- 不希望 agent 自动修改自身技能或长期记忆的场景。

## 4.3 OpenClaw vs Hermes

| 维度 | OpenClaw | Hermes |
|---|---|---|
| 一句话定位 | 多渠道本地个人 AI Gateway | 自改进 CLI/TUI 开发 agent |
| 第一入口 | 消息平台、移动端、控制 UI | CLI/TUI，另有消息网关 |
| 最强能力 | 渠道覆盖、个人助理、Gateway、Canvas/voice/nodes | 长期记忆、技能演化、终端工作流、远程 backends |
| 模型策略 | Model-agnostic，支持主流云和本地模型 | 多 provider、自定义 endpoint、OpenRouter 等 |
| 技能系统 | ClawHub + Markdown skills | 自动创建/改进 skills + skills hub |
| 运行时 | Node 24/22.19+，Gateway daemon | Python 3.11+/uv，CLI/TUI + gateway |
| 安全重点 | DM pairing、allowlist、sandbox、tool policy | command approval、DM pairing、container isolation |
| 主要风险 | 多渠道和工具权限面过大；host 权限和 skill supply chain | 自改进漂移；记忆/技能污染；多 backend 管理 |
| 最适合 | AI 个人助理控制面、消息平台 agent | 长期开发/研究 assistant、技能化自动化 |
| 对 GhostAP 的启发 | Feishu/channel 抽象、安全默认、route/session/gateway | 记忆/技能生命周期、agent profile、cron、远程后端 |

综合判断：OpenClaw 更像“入口和控制面”，Hermes 更像“执行者和记忆体”。如果构建一个 GhostAP 类系统，OpenClaw 是直接竞品，Hermes 是能力模块参考。

## 5. 其他同类产品分析

## 5.1 OpenAI Codex

### 定位

Codex 是 OpenAI 的 coding agent 产品线，覆盖 CLI、本地 IDE 扩展、桌面 app、Web/Cloud、ChatGPT 入口和自动化能力。GitHub `openai/codex` README 称 Codex CLI 是运行在本机的 coding agent；OpenAI 的 Codex app 页面强调 macOS/Windows app、CLI、Web、IDE extension 和技能/自动化。

### 优点

- 生产 coding 能力强，适合真实仓库修改、测试、review、文档、迁移。
- 多入口：CLI、IDE、桌面、Web、ChatGPT、移动端入口逐步整合。
- Skills 和 Automations 使重复工作可产品化。
- OpenAI 模型生态和工具能力强，尤其适合需要代码 + 文档 + 图片 + 浏览/应用操作组合的任务。
- CLI 开源，适合研究 harness 设计、权限模型和本地执行体验。

### 缺点

- 核心模型和云端产品闭源，深度定制有限。
- 企业数据边界、权限、审计需要按 OpenAI/ChatGPT Enterprise 等方案评估。
- 高质量模型和长任务成本可能较高。
- 对非 OpenAI 模型的自由切换不如 BYOK 工具。

### 适用

生产代码任务、复杂重构、测试修复、文档更新、设计转 UI、自动化工程例行任务。

## 5.2 Claude Code

### 定位

Claude Code 是 Anthropic 的 agentic coding system。官方文档描述它能在终端、IDE、桌面 app、Web/Cloud、CI/CD 等入口工作，能计划、跨文件改代码、验证、提交、开 PR，并通过 MCP 连接外部工具。

### 优点

- 在代码理解、规划、重构、测试修复上表现强，适合复杂工程任务。
- MCP 生态成熟，适合接 Jira、Google Drive、Slack、内部工具。
- CLI/IDE/Web/Cloud/CI 入口覆盖完整。
- 工程协作能力强：commit、PR、review、issue triage。

### 缺点

- 模型/provider 绑定 Anthropic。
- 成本和 rate limit 受订阅/企业计划影响。
- 源码开放度不如开源 CLI agent。
- Agent 权限越大，prompt injection 和工具误用风险越需要策略控制。

### 适用

复杂已有代码库任务、企业工程团队、MCP 集成场景、代码 review 和 CI 自动化。

## 5.3 Gemini CLI

### 定位

Google 的开源终端 AI agent。官方文档强调开源、Gemini 2.5 Pro、1M context、Google Search grounding、文件操作、shell、web fetch、MCP 和较高免费额度。

### 优点

- 免费额度和 1M context 对探索/理解大型上下文很有吸引力。
- Apache 2.0 开源，适合本地和社区改造。
- 内置 Google Search grounding，适合需要检索辅助的任务。
- Node/npm 安装门槛低。

### 缺点

- 深度 coding 质量和稳定性要按任务实测，不应只看 context window。
- Google 账号/API/模型限制带来平台依赖。
- 终端-first 产品，不提供 OpenClaw 类多渠道 Gateway。

### 适用

本地开发者、成本敏感、大上下文阅读、需要开源 CLI agent 的场景。

## 5.4 Aider

### 定位

Aider 是经典 terminal pair programmer，直接在 git repo 内工作，支持多语言、多模型、自动 commit、lint/test 循环、图片/web 页面上下文、语音输入。

### 优点

- Git 工作流简单可靠，自动提交便于 diff/rollback。
- BYOK、多模型、轻量，适合 SSH/tmux/远程机器。
- 对“让 agent 做小到中等代码修改”非常实用。
- 成熟时间长，社区经验多。

### 缺点

- 不如 Codex/Claude/Hermes 那样强调多 agent、消息网关、长期记忆或云端任务。
- UI 朴素，非开发者友好度低。
- 复杂跨模块任务仍需要人拆解和监督。

### 适用

终端开发者、已有 git repo、可控小步提交、低平台绑定。

## 5.5 OpenCode

### 定位

OpenCode 是开源 terminal-native coding agent，官方文档称它可作为 terminal interface、desktop app 或 IDE extension 使用。

### 优点

- 开源、终端原生，适合 shell 用户。
- 多安装方式，配置 provider 后即可在项目中 `/init` 生成 AGENTS.md。
- 适合被集成到脚本和本地工作流。

### 缺点

- 生态和复杂任务表现要和 Codex/Claude/Aider 实测对比。
- 比 OpenClaw/Hermes 少了多渠道个人助理和长期记忆主叙事。

### 适用

希望使用开源 terminal agent、需要 AGENTS.md 项目规则、希望避免厂商锁定的团队。

## 5.6 Cline

### 定位

Cline 是开源 AI coding agent，主要在编辑器和终端中工作。官方文档强调读写文件、运行命令、浏览器使用，并且每个动作都需要显式批准；同时提供 SDK、CLI、Kanban、VS Code、JetBrains，并支持 Cursor/Windsurf/Antigravity/Zed/Neovim 等编辑器形态。

### 优点

- human-in-the-loop 安全体验强，适合 IDE 内可视化审批。
- BYOK 和多 provider，自主成本控制。
- SDK/Kanban/CLI 说明它正在从扩展走向 agent platform。
- 对需要透明工具动作的团队更友好。

### 缺点

- 自动化程度和后台长任务体验不如云端 agent。
- 真正大规模团队治理需要企业功能支持。
- IDE/workspace 形态强，做多渠道消息 bot 不如 OpenClaw。

### 适用

开发者 IDE agent、需要每步审批、开源扩展、BYOK 团队。

## 5.7 Roo Code

### 定位与当前状态

Roo Code 曾是 VS Code 的强 agent 扩展，强调 model-agnostic、文件系统访问、终端控制、多步 workflow、Modes/Orchestrator。但官方文档显示 Roo Code Extension 已于 2026-05-15 关闭，并推荐社区 fork ZooCode 或 Cline。

### 结论

不建议作为新选型。可以参考它的 modes/orchestrator/auto-approve 思路，但新项目应看 Cline 或社区 fork。

## 5.8 OpenHands

### 定位

OpenHands 是开源软件开发 agent 平台和 SDK。官方 SDK 文档强调统一、类型安全、从本地实验到生产部署、statelessness、composability、清晰 research/deployment 边界。

### 优点

- 更适合作为二次开发基础设施，而非单纯终端工具。
- Python/REST API 便于构建自定义 agent 服务。
- 开源、可研究、可嵌入，适合平台团队。

### 缺点

- 对最终用户开箱体验通常不如 Codex/Claude/Cursor。
- 需要工程团队理解 SDK 架构和运行模型。

### 适用

构建自有软件开发 agent、做研究平台、需要可控 agent SDK 的团队。

## 5.9 Devin

### 定位

Devin 是 Cognition 的托管 AI software engineer。官方文档称它可以写、运行、测试代码，适合 Linear/Jira tickets、新功能、bug 复现修复、内部工具、迁移、重构、PR review、文档维护等，并支持 Web app、CLI、Slack/Teams/GitHub/GitLab/Bitbucket/Linear/Jira 等集成。

### 优点

- 团队异步任务能力强，适合 backlog 消化。
- 托管环境减少本地配置问题。
- 对并行多任务、迁移、重构、测试、内部工具比较契合。
- 有团队协作、反馈、知识和集成体系。

### 缺点

- 托管产品，数据、代码、secret、成本和权限依赖供应商。
- 不适合高度敏感或无法外发代码的场景。
- 对模糊任务仍需要清晰验收标准，否则失败成本高。

### 适用

有明确 ticket/CI/PR 规范的工程团队，尤其是中低复杂度、高重复度 backlog。

## 5.10 GitHub Copilot cloud agent

### 定位

Copilot cloud agent 是 GitHub 内置的云端 coding agent。官方文档强调它不同于 IDE agent mode：它在 GitHub Actions-powered 环境中处理 GitHub issue 或 Copilot Chat 指派的任务，可以研究 repo、计划、改代码、创建分支并可选择开 PR。

### 优点

- GitHub 原生，issue/branch/PR/Actions 集成最顺。
- 适合团队把小任务直接分配给 agent。
- 支持 custom instructions、MCP、custom agents、hooks、skills。
- 权限和审计与 GitHub Enterprise 体系结合。

### 缺点

- 只能处理 GitHub 上的 repo。
- 默认每个任务一个 repo、一条 branch、一个 PR，跨 repo 能力受限。
- 使用 GitHub Actions minutes 和 Copilot premium requests。
- 文档明确指出它不遵守某些 content exclusions，安全策略需单独审查。

### 适用

GitHub 企业用户、issue-to-PR 流程、小到中等明确任务、强 PR 审核团队。

## 5.11 Jules

### 定位

Jules 是 Google 的异步 coding agent。官方站点强调它会在 Cloud VM 中 clone 代码、验证修改，并支持 quick fixes 到异步多 agent 开发。

### 优点

- Google/Gemini 生态，适合已有 Google Cloud/Workspace 用户。
- 异步任务形态清晰：在云 VM 中执行，完成后交付变更。
- 与 Gemini CLI/Google Code Assist 形成互补。

### 缺点

- 实验/产品边界仍需持续关注。
- 数据边界和权限取决于 Google 平台。
- 对非 Google 生态用户吸引力弱于 Codex/Claude/Copilot。

### 适用

Google 生态团队、异步修 bug/补文档/升级依赖。

## 5.12 Cursor

### 定位

Cursor 是 AI-first IDE。官方产品页强调一个 agent 覆盖 Desktop、CLI、GitHub、Slack、Linear、JetBrains 等入口，桌面端支持从手动到 agentic coding，CLI 可在 terminal/script/editor 中运行 agent。

### 优点

- IDE 体验强，开发者日常使用摩擦低。
- Agent + editor 紧密结合，适合在可视 diff 和代码上下文中协作。
- 多入口和团队工作流正在增强。

### 缺点

- VS Code fork/闭源产品依赖强。
- 不适合作为自托管消息 Gateway。
- 大型复杂任务仍需要人类 review 和测试兜底。

### 适用

日常开发者主 IDE、快速原型、前端/全栈迭代、轻团队协作。

## 5.13 Windsurf Cascade

### 定位

Cascade 是 Windsurf 的 agentic AI assistant。官方文档列出 Code/Chat 模式、tool calling、voice input、checkpoints、real-time awareness、linter integration、web search、memories/rules、MCP、terminal、workflows、app deploys。

### 优点

- IDE 内上下文感知和 linter/checkpoint 体验强。
- Chat/Code 模式区分清楚，适合从问答到修改。
- MCP、terminal、workflow、deploy 能覆盖常见工程任务。

### 缺点

- 依赖 Windsurf IDE/商业产品路线。
- 多渠道 Gateway、长期技能演化、开放后端不如 OpenClaw/Hermes。

### 适用

IDE-first 开发团队、前端/应用开发、希望 agent 在编辑器内保持上下文的用户。

## 5.14 Kiro

### 定位

Kiro 是 agentic IDE/CLI/Web，强调 specs、steering、hooks 和 spec-driven development。它试图解决 vibe coding 里的“没规格就生成代码”问题。

### 优点

- Spec-driven 适合生产工程：先需求/设计/任务，再执行。
- Steering/hooks 有利于把团队规范固化到流程。
- IDE/CLI/Web 多入口。

### 缺点

- 生态成熟度需要实测。
- 更偏 IDE 和规范流程，不是消息 Gateway。

### 适用

重视需求/设计/验收的团队，尤其是从 prompt-to-code 转向 spec-to-code 的场景。

## 5.15 Replit Agent

### 定位

Replit Agent 面向从自然语言构建 web app、mobile app、dashboard、AI tools 等。它的优势是 IDE、hosting、database、deployment 一体化。

### 优点

- 从 0 到 1 应用原型速度快。
- 对非专业工程用户友好。
- 云端开发环境和部署结合紧密。

### 缺点

- 不适合直接接触生产数据或高风险权限。
- 复杂已有代码库和企业工程治理不是强项。
- 公开事件显示 agent 误操作生产数据的风险必须严肃对待。

### 适用

原型、demo、内部小工具、教学、低风险应用生成。

## 6. 横向评分

评分：5 = 很强，3 = 可用，1 = 弱或不适合。该表是工程选型视角，不是模型能力 benchmark。

| 产品 | 类型 | 代码执行质量 | 多渠道入口 | 记忆/技能 | 安全治理 | 开放/可扩展 | 团队异步 | 最适合 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| OpenClaw | Gateway/个人助理 | 3 | 5 | 4 | 3 | 4 | 3 | 多消息入口个人/团队 agent |
| Hermes | CLI/TUI + Gateway | 4 | 3 | 5 | 3 | 4 | 4 | 长期开发/研究 agent |
| Codex | Coding agent | 5 | 3 | 4 | 4 | 3 | 5 | 生产代码任务与自动化 |
| Claude Code | Coding agent | 5 | 3 | 4 | 4 | 3 | 5 | 复杂代码理解和工程协作 |
| Gemini CLI | CLI agent | 3 | 1 | 2 | 3 | 4 | 2 | 低成本大上下文终端任务 |
| Aider | CLI pair programmer | 4 | 1 | 2 | 3 | 4 | 2 | Git repo 内小步代码修改 |
| OpenCode | CLI/IDE agent | 3 | 1 | 3 | 3 | 4 | 2 | 开源 terminal agent |
| Cline | IDE/CLI agent | 4 | 1 | 3 | 4 | 5 | 3 | IDE 内透明审批 coding |
| OpenHands | SDK/platform | 3 | 1 | 3 | 4 | 5 | 3 | 自研软件 agent 平台 |
| Devin | 托管云端 engineer | 4 | 3 | 4 | 4 | 2 | 5 | 团队 backlog/PR 异步执行 |
| Copilot cloud agent | GitHub 云端 agent | 4 | 2 | 3 | 4 | 3 | 5 | GitHub issue-to-PR |
| Jules | Google 异步 agent | 3 | 1 | 2 | 4 | 2 | 4 | Google 生态异步代码任务 |
| Cursor | AI IDE | 4 | 3 | 3 | 3 | 2 | 4 | 日常 IDE 开发 |
| Windsurf | AI IDE | 4 | 2 | 3 | 3 | 2 | 3 | IDE 内 agentic 开发 |
| Kiro | Spec-driven IDE | 3 | 2 | 3 | 4 | 3 | 3 | 规范驱动开发 |
| Replit Agent | App builder | 3 | 2 | 2 | 2 | 2 | 3 | 快速应用原型 |
| Roo Code | IDE agent | 3 | 1 | 3 | 3 | 4 | 2 | 已关闭，不建议新选型 |

## 7. 安全与治理风险

### 7.1 Prompt injection 和 tool misuse

所有能读取网页、issue、PR、邮件、聊天记录、文档或代码注释的 agent 都会把不可信输入带进上下文。只要 agent 有 shell、浏览器、文件、secret、云 API、GitHub token 等工具权限，prompt injection 就可能升级成实际操作风险。

建议：

- 把“不可信输入处理”和“有权限工具调用”隔离。
- 对写文件、shell、网络、secret 读取、发消息、开 PR、部署等高风险动作做审批。
- 对外部消息/issue/网页默认降权处理。
- 高权限工具只允许特定用户、特定 chat、特定 repo、特定会话开启。

### 7.2 Skill/plugin supply chain

OpenClaw、Hermes、Codex、Claude Code、Cursor/Cline 类产品都在走“skills/plugins/MCP”方向。技能本质上是 prompt + 脚本 + 资源 + 权限边界，风险接近 npm/PyPI 供应链，但更隐蔽，因为恶意行为可能藏在自然语言 instructions 中。

建议：

- 技能安装需要来源信任、签名或内部镜像。
- 技能权限最小化，默认无 secret、无网络、无 host 写权限。
- 技能更新要审计 diff。
- 禁止从聊天内容直接安装/执行未知技能。

### 7.3 Sandbox 不等于绝对安全

OpenClaw 文档明确指出 sandbox 不是完美边界，但能降低 blast radius。Docker/SSH/OpenShell/云 VM 只能限制部分文件系统、进程、网络和环境变量风险，仍需 tool policy、secret 管理和审批。

建议：

- 默认 sandbox all 或至少 non-main。
- 默认网络关闭，按任务临时放行。
- workspace 挂载默认 read-only 或 none。
- 禁止 docker.sock、SSH key、cloud credential、home config 目录挂载。
- 对 elevated/escape hatch 做双人审批或禁用。

### 7.4 长期记忆污染

Hermes/OpenClaw/Codex/Claude/Copilot 都在向记忆、skills、instructions、workspace context 发展。长期记忆一旦被错误事实、恶意指令或过期策略污染，会在后续任务中反复生效。

建议：

- 记忆分级：事实、偏好、项目规则、临时结论分开存。
- 记忆写入要有来源、时间、作用域、过期策略。
- 高风险记忆需要人工确认。
- 支持 memory diff、rollback、review。

### 7.5 成本失控

Agent 长任务、并行子 agent、自动化、技能调用、浏览器和多轮测试都会放大 token 与云 runner 成本。OpenClaw/Codex/Hermes/Devin/Copilot 都可能在“看起来只是一个请求”的情况下触发大量后台工作。

建议：

- 每任务设置 token、时间、工具调用、子 agent 数、网络访问和重试上限。
- 在卡片或 UI 中显示实时成本/用量。
- 长任务默认需要明确目标、退出条件和验收命令。

## 8. 对 GhostAP / Feishu bot 平台的启发

GhostAP 的产品形态和 OpenClaw/Hermes 有明显重叠：都把消息入口、远程 shell、项目上下文、coding tools 和长任务执行组合起来。可借鉴方向如下：

### 8.1 从 OpenClaw 借鉴

1. 多渠道抽象：channel、sender、workspace、session、agent 的分层值得参考，但 GhostAP 应优先做好 Feishu/Lark，再扩展其他 channel。
2. DM pairing/allowlist：未知 sender 默认不可执行任务，尤其是 shell、coding、repo 操作。
3. Sandbox explain：用户需要知道某次任务为什么被允许/拒绝，当前 sandbox/tool policy 是什么。
4. Group/channel safety：群聊默认低权限，主用户/管理员 DM 才能提权。
5. Gateway exposure runbook：远程暴露前必须有安全检查清单。
6. Skills marketplace 风险治理：若 GhostAP 引入技能市场，必须先有签名、权限、审计、回滚。

### 8.2 从 Hermes 借鉴

1. 长期记忆要产品化：把用户偏好、项目规则、工具经验、失败案例、验证命令分层存储。
2. 技能自动沉淀：当 agent 多次执行相似流程后，建议生成可审查 skill，而不是直接静默改系统 prompt。
3. 会话搜索：FTS + LLM summary 适合“找之前怎么解决过这个项目问题”。
4. 多后端执行：local/Docker/SSH/cloud sandbox 的抽象可参考，用统一 session protocol 屏蔽差异。
5. Cron/计划任务：Feishu bot 很适合定时日报、CI 摘要、依赖升级巡检、代码健康扫描。

### 8.3 从 Codex / Claude Code 借鉴

1. 生产质量闭环：计划、编辑、运行测试、修复失败、总结 diff 必须成为默认执行协议。
2. 技能和 repo instructions：把团队流程写入可版本化文件，而不是只靠聊天上下文。
3. 多入口一致：CLI/IDE/Web/Feishu 如果都接入，行为应该共享同一个任务协议。
4. 自动化 review queue：长任务完成后先进入 review，不应直接部署或合并。

### 8.4 从 Copilot / Devin / Jules 借鉴

1. issue-to-PR 模式：任务应天然产出 branch、commit、PR 和验证结果。
2. 一任务一 PR：限制 blast radius，降低 review 成本。
3. 明确完成标准：prompt 必须包含验收命令、测试范围和不做什么。
4. 背景任务看板：长任务需要状态、日志、成本、失败原因、接管入口。

## 9. 推荐选型策略

### 9.1 个人使用

- 主入口在聊天软件、手机和语音：OpenClaw。
- 主入口在终端、希望 agent 长期记住你：Hermes。
- 主任务是改代码：Codex 或 Claude Code。
- 成本敏感和开源优先：Gemini CLI、OpenCode、Aider、Cline。

### 9.2 小团队

- GitHub-heavy：Copilot cloud agent + Codex/Claude Code。
- Slack/Teams/Jira/Linear backlog：Devin 或 Claude/Codex cloud。
- Feishu/Lark-heavy：GhostAP 自研路线更合适，OpenClaw 作为架构参考。
- 开源和私有化：OpenHands + Cline/OpenCode/Aider，必要时接本地模型或私有 API。

### 9.3 企业

优先级应从“模型效果”转向“治理能力”：

1. 身份、权限、审计、secret 管理。
2. 数据边界和部署位置。
3. sandbox 和网络隔离。
4. PR/review/CI 强制门禁。
5. 技能、MCP、插件供应链管理。
6. 失败回滚和人工接管。

对企业而言，Devin/Copilot/Codex/Claude 更容易纳入供应商管理；OpenClaw/Hermes/OpenHands 更容易私有化和改造，但工程治理责任也更多。

## 10. 建议的实测方案

如果要真正选型，不建议只看文档和 benchmark。建议拿同一个真实 repo 做 6 类任务：

1. 小 bug：给错误日志，要求定位并补测试。
2. 中型功能：改 3-8 个文件，要求不破坏现有 API。
3. 重构：清理重复逻辑，保持行为不变。
4. 文档：更新 README/API 文档，要求引用代码事实。
5. 安全：修一个权限或 secret handling 问题。
6. 前端：从截图或 spec 改 UI，要求视觉验证。

记录指标：

- 首次成功率。
- 是否主动读项目规则。
- 是否跑了正确测试。
- 是否能解释失败原因。
- diff 是否小而准。
- 是否引入无关重构。
- 是否尊重权限边界。
- token/时间/云 runner 成本。
- 人工 review 修改量。
- 回滚和接管体验。

最低通过标准：

- 不能绕过测试。
- 不能在没有授权时读取 secret 或外发代码。
- 不能在生产资源上直接执行破坏性命令。
- 必须产出可 review 的 diff 和验证摘要。

## 11. 采购/引入建议

### 短期

1. 把 Codex 和 Claude Code 作为生产 coding agent 主力评估对象。
2. 把 OpenClaw 和 Hermes 作为 GhostAP 产品方向和架构参考，而不是立即替换。
3. 对开源本地方案，保留 Aider、OpenCode、Cline 作为补充。
4. Roo Code 不再作为新选型。

### 中期

1. 为 GhostAP 建立 agent 安全基线：sender allowlist、chat scope、sandbox、tool policy、secret denylist、cost cap。
2. 引入“任务完成标准”协议：目标、边界、测试、回滚、交付物。
3. 做长期记忆/技能沉淀，但必须可审查、可回滚、可过期。
4. 做 Feishu 内任务看板和 PR/review queue。

### 长期

1. 把 GhostAP 定位成“团队消息入口上的 agent orchestration platform”，而不是单个 coding backend。
2. 后端工具保持可替换：Codex、Claude、Gemini、Coco、Aiden、TTADK 都应是 transport/provider，而不是产品状态本身。
3. 形成自有 skill registry，并内置安全扫描和权限声明。
4. 建立多 agent 协作、匿名评审、审计回放和自动验收体系。

## 12. 来源

主要使用官方文档、GitHub README 和少量安全研究/行业资料。星标、release、价格和可用性会变化，本文以 2026-05-23 可访问公开资料为准。

- OpenClaw GitHub README: https://github.com/openclaw/openclaw
- OpenClaw docs home: https://docs.openclaw.ai/
- OpenClaw sandboxing: https://docs.openclaw.ai/gateway/sandboxing
- OpenClaw landing/docs mirror: https://openclawdoc.com/
- Hermes Agent GitHub README: https://github.com/NousResearch/hermes-agent
- Hermes TUI docs: https://hermes-agent.nousresearch.com/docs/user-guide/tui
- Claude Code docs: https://code.claude.com/docs/en/overview
- OpenAI Codex app: https://openai.com/index/introducing-the-codex-app/
- OpenAI Codex GitHub: https://github.com/openai/codex
- Gemini CLI docs: https://google-gemini.github.io/gemini-cli/
- Aider GitHub: https://github.com/Aider-AI/aider
- OpenCode docs: https://dev.opencode.ai/docs
- Cline docs: https://docs.cline.bot/cline-overview
- Roo Code docs: https://roocodeinc.github.io/Roo-Code/
- OpenHands SDK docs: https://docs.openhands.dev/sdk/arch/overview
- Devin docs: https://docs.devin.ai/get-started/devin-intro
- GitHub Copilot cloud agent docs: https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent
- Windsurf Cascade docs: https://docs.windsurf.com/windsurf/cascade/cascade
- Cursor product page: https://cursor.com/en-US/product
- Jules: https://jules.google/
- Replit Agent docs: https://docs.replit.com/core-concepts/agent/
- Kiro docs: https://kiro.dev/docs/
- OWASP Prompt Injection: https://owasp.org/www-community/attacks/PromptInjection
- OWASP Agentic Skills Top 10: https://owasp.org/www-project-agentic-skills-top-10/
- Snyk ToxicSkills research: https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/
