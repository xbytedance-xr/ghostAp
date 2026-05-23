# Security and UX Review Report: Slock Engine

## 1. Security Audit Findings

### 1.1 Authentication & Authorization
**Finding: Inconsistent Permission Checks**
- **Location:** `src/feishu/handlers/slock.py`, `src/slock_engine/engine.py`, `src/slock_engine/slash_commands.py`
- **Issue:** Permission checks are inconsistently applied. `handle_slock_stop` and `handle_slock_kill` check against `admin_user_ids`, but many other handlers (e.g., `handle_card_action`) and engine operations (e.g., `create_agent`, `execute_agent_task`, `start_discussion`) lack any authorization logic. The `SlashCommandParser` performs purely syntactic parsing without permission validation. `force_assign()` in `TaskRouter` allows admin override without any audit logging.
- **Risk:** High. Unauthorized users could potentially invoke sensitive operations, create/destroy agents, assign tasks forcefully, or trigger discussions, leading to resource exhaustion or unauthorized data access.

### 1.2 Sensitive Information Leaks
**Finding: Missing Redaction in Cards and Memory**
- **Location:** `src/slock_engine/card_templates.py`, `src/slock_engine/memory_manager.py`
- **Issue:** While `redact_sensitive()` is used in council, escalation, and error suggestion cards, it is missing from `build_memory_display_card`, `build_discussion_card`, and `build_agent_message_card`. Furthermore, `MemoryManager` imports `redact_sensitive` but does not apply it during read/write operations.
- **Risk:** High. Raw memory, raw discussion messages, and raw agent outputs may inadvertently display sensitive information (e.g., API keys, tokens) to users via the Feishu interface or log files.

**Finding: Plaintext Secrets Storage**
- **Location:** `src/slock_engine/memory_manager.py`
- **Issue:** Memory files (L1, L2, L3) are stored as plaintext JSON without encryption.
- **Risk:** Medium-High. If an attacker gains access to the filesystem, they can read all historical context, which may contain sensitive configuration details or secrets.

### 1.3 Input Validation & Privilege Escalation
**Finding: Lack of Input Sanitization**
- **Location:** `src/slock_engine/discussion_manager.py`, `src/slock_engine/engine.py`
- **Issue:** There is no content validation on generated discussion messages before appending them. The `_execute_task` method in `SlockEngine` calls the ACP session without a `try/finally` block to ensure agent status is reset to `IDLE` on exception.
- **Risk:** Medium. Unsanitized input could lead to prompt injection or malformed data polluting the memory/discussion. The missing `try/finally` can cause a "status leak," leaving agents permanently in a `RUNNING` state, effectively deadlocking them.

## 2. UX and Interaction Logic Recommendations

### 2.1 Management Commands
- **Optimization:** Implement universal authentication middleware for all `/slock` commands and card actions. This ensures consistent permission enforcement before parsing or executing commands.
- **Card Design:** Update `build_command_panel_card` to include visual indicators of permission requirements (e.g., a lock icon for admin-only actions). Ensure all command inputs are validated client-side (within the Feishu card) where possible, before server-side processing.

### 2.2 Inter-agent Task Communication
- **Implementation:** The `DiscussionManager` and `TaskRouter` provide a good foundation. Enhance `start_discussion` and `route_message` by incorporating explicit triggers for discussion rounds based on task complexity or specific NLI intents.
- **Feedback Loop:** Integrate a mechanism for the user to monitor or intervene in inter-agent discussions if they stall or veer off-topic, perhaps via an interactive card summarizing the discussion progress.

## 3. Memory System Improvements

### 3.1 Data Security
- **Encryption:** Implement encryption at rest for the `MemoryManager` storage. Use a secure key management strategy to encrypt/decrypt L1, L2, and L3 JSON files during read/write operations.
- **Redaction Enforcement:** Apply `redact_sensitive()` uniformly across all memory read paths and within all card builders that display raw text (`build_memory_display_card`, `build_discussion_card`, `build_agent_message_card`).

### 3.2 Robustness
- **Status Management:** In `SlockEngine._execute_task`, wrap the execution logic in a `try/finally` block to guarantee the agent's status is reset (e.g., to `IDLE` or `ERROR`) regardless of execution success or failure.
