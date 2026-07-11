"""GhostAp Autonomous Work System - Phase 1 Core.

Five-layer architecture:
- Goal Control: understand goals, generate plans, continuous replanning
- Durable Runtime: persistent scheduling, leases, checkpoints, recovery
- Agent Runtime: model turns, tool results, continue/stop decisions
- Safety Control: permissions, approvals, budget, isolation, audit, kill switch
- Feishu Interaction: Manager Bot, optional employee bots, groups
"""
