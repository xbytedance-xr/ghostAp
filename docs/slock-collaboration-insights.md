# Slock 协作机制可借鉴方案

> 来源：2026-05-27 Slock调研介绍与复刻Demo演示会议纪要 + 相关技术文档分析
> 对标项目：GhostAP slock_engine

---

## 借鉴点一：Freshness Gate（消息新鲜度门控）

**Slock 原理**：Agent 发消息时必须携带一个 `freshness_token`（等于它看到的最新消息序列号）。本地 daemon 在消息真正发出前做一次预检——如果发现频道里已经有了该 agent 还没读到的新消息，就把这条待发消息 hold 住，不让它出去。

**解决的问题**：多个 agent 并行工作时，一个 agent 可能花了 30 秒生成回复，但这 30 秒内群里已经来了新消息（人类追加了需求、或另一个 agent 已经回答了）。没有这个门控，就会出现"用过期上下文生成的废话"刷屏。

**对我们的适配方案**：在 `_execute_agent` 的 SENDING 阶段，发送前检查该 chat 在 agent 开始执行之后是否有新消息到达。若有，将结果暂存，把新消息摘要注入给 agent 让它重新判断要不要修改回复。设一个上限（最多重检 2 次），超出则强制发送，避免死循环。

---

## 借鉴点二：Draft 保存与有界重试

**Slock 原理**：被 hold 的消息不是直接丢弃，而是存为 draft，同时给 agent 回传最近 3 条新消息上下文，并提供三个动作选项：
- `send_draft`：答案仍然成立，原样发出
- `check_messages`：先读新消息，重新决策
- `send_anyway`：跳过检查强制发出（逃生口）

**解决的问题**：agent 花了大量 token 算出答案，不能因为"有新消息"就全部作废。同时也不能无限循环重试。需要一个"有界不失控"的收敛机制。

**对我们的适配方案**：在 agent 执行结果产出后增加 draft 缓存层。若 freshness 检查不通过，将 draft + 新上下文一起交给 agent 做一次轻量判断（不是完整重新执行）：答案仍有效就直接发，需要修改就微调后发。给一个重试预算（2 次），超出后强制发送并附加"此回复基于较早上下文"的标注。

---

## 借鉴点三：协调者降级与接力协议

**Slock 原理**：协调者 agent（如"诸葛孔明"）token 耗尽或不可用时，实际执行者可以直接接住后续流程。每个 agent 完成自己的步骤后在消息中 @ 下一位，形成接力链。不依赖中心调度器持续在线。

**解决的问题**：如果协调者是单点，它挂了整个协作链就断了。Slock 的番茄钟案例中，协调者孔明 token 用完后张飞凭 MEMORY 自主接住了后续任务。

**对我们的适配方案**：在 `collaboration_orchestrator` 和 `task_chain_manager` 中增加"协调者不可用"的降级路径。当 orchestrator agent 超时或报错时，当前正在执行的 agent 可以读取 plan 状态，自主决定推进到下一步骤或向人类发起 escalation。不需要等协调者恢复。

---

## 借鉴点四：Agent 语义层先行过滤（软协调）

**Slock 原理**：5 个 agent 同时看到一条任务消息，但绝大多数冲突在 LLM 推理层就被消解——agent 根据自己 MEMORY.md 中的角色定位判断"该不该我接"。实测中只有协调者角色的 agent 去 claim，其余一次请求都没发。Task claim 的硬互斥只是漏网冲突的最后兜底。

**解决的问题**：如果所有 agent 都去抢锁，即使 CAS 保证了正确性，也产生大量无效请求和浪费的推理 token。

**对我们的适配方案**：在 `task_router.py` 的路由评分之前，增加一层"agent 自我评估"——将任务摘要 + agent 角色描述做一次快速匹配（规则优先，LLM 兜底），让明显不相关的 agent 直接跳过。降低 routing 层的计算量，同时让 agent 的行为更像"有判断力的同事"而非"无脑抢活的机器人"。

---

## 借鉴点五：行为自收敛与"send once, then watch"

**Slock 原理**：agent 在 MEMORY 中记录自身行为模式——"我总是抢不上这类任务"、"发一次消息后转为观察，不再空转重试"。这些是 agent 自己学出来的，不是硬编码规则。

**解决的问题**：防止 agent 陷入无效循环（反复抢锁失败、反复发消息被 hold、重复回答已被解答的问题）。

**对我们的适配方案**：在 `memory_manager.py` 的 L1 记忆更新中，增加"行为模式归纳"维度。当某 agent 连续 N 次在某类任务上被跳过或执行失败，自动在其 memory 中写入回避策略。同时在 observer_queue 的学习机制中，让旁观 agent 从他人成功中推断"这类活适合谁"，影响后续自我判断。

---

## 借鉴点六：统一的 Action Card 副作用门控

**Slock 原理**：所有有副作用的操作（部署、删除、修改配置等），统一走一个模式——Agent 生成提案卡片 → 人类点击确认 → 系统代为执行 → Agent 跟进结果。不给 agent 直接执行写操作的权限。

**解决的问题**：防止 agent 自主执行危险操作，同时不让开发者各自发明不同的确认逻辑。

**对我们的适配方案**：将现有的 `escalation_manager` 中的人工确认逻辑抽象为通用的 Action Card 模板。对 shell 执行、代码提交、文件删除等操作，统一走"提案 → 确认 → 执行 → 反馈"的四步卡片流。在 `card_templates/` 下新增 `action_card.py` 作为标准副作用操作的展示与交互模板。

---

## 借鉴点七：System Prompt 动态注入协作上下文

**Slock 原理**：daemon 每次唤醒 agent 时动态生成 system prompt，包含当前服务器的所有频道、其他 agent 清单、CLI 命令列表、以及关键行为规则。Agent 不需要"发现"如何协作，协作知识是被"灌"进去的。

**解决的问题**：让 agent 天然知道当前协作环境——谁在群里、各自负责什么、怎么沟通——而不需要运行时多次查询。

**对我们的适配方案**：在 agent 的 ACP session 创建时，动态拼装一段协作上下文注入 system prompt，内容包括：当前群的其他 agent 名单及角色、任务板当前状态摘要、协作规则（如"执行前先 claim"、"完成后 @ 下一位"）。让 agent 从第一个 token 就知道自己身处一个团队，而非孤立工作。

---

## 优先级排序

| 优先级 | 借鉴点 | 预期收益 |
|--------|--------|---------|
| P0 | 一、Freshness Gate | 防止过期回复，提升多 agent 并发质量 |
| P0 | 二、Draft 保存与有界重试 | 长任务执行期间不丢失工作成果 |
| P1 | 三、协调者降级与接力协议 | 提高容错性，避免单点故障 |
| P1 | 六、统一 Action Card 门控 | 副作用操作标准化，提升安全性 |
| P2 | 四、语义层先行过滤 | 减少无效 claim 和 token 浪费 |
| P2 | 五、行为自收敛 | 长期提升路由准确性 |
| P2 | 七、System Prompt 动态注入 | 让 agent 协作行为更自然 |

---

## 参考文档

- [Slock调研介绍与复刻Demo演示 智能纪要](https://bytedance.larkoffice.com/docx/VBcidVlMso5Hq1xFRKccohtinYe)
- [把 Agent 做成同事：Slock 协议解剖与飞书启发](https://bytedance.larkoffice.com/docx/Wv1jdVHCFoXSeCxZk0ccWC6tnSf)
- [读懂 slock 多 Agent 协作：四个问题串起一条主线](https://bytedance.larkoffice.com/docx/I6lddijw8oOHwQxyLhRc98g7nBg)
