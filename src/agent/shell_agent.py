from typing import Optional
from dataclasses import dataclass
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage
from ..config import get_settings


@dataclass
class SafetyCheckResult:
    is_safe: bool
    reason: Optional[str] = None
    risk_level: str = "low"


class ShellAgent:
    SYSTEM_PROMPT = """你是一个Shell命令安全审查助手。你的任务是分析用户提供的shell命令，判断其是否安全。

安全检查规则：
1. 危险命令（必须拒绝）：
   - 删除系统关键目录（如 rm -rf /、rm -rf /*）
   - 格式化磁盘（mkfs、dd if=）
   - 系统关机/重启（shutdown、reboot、halt、poweroff）
   - Fork炸弹或无限循环
   - 修改系统关键权限（chmod 777 /、chown root /）

2. 高风险命令（需要警告）：
   - 删除大量文件（rm -rf）
   - 修改系统配置文件
   - 安装未知软件
   - 网络相关的危险操作

3. 安全命令（允许执行）：
   - 查看文件/目录（ls、cat、head、tail）
   - 系统信息（uname、whoami、date、df、free）
   - 进程信息（ps、top）
   - 网络信息（ifconfig、ip、netstat）
   - 文本处理（grep、awk、sed）

请用JSON格式回复，包含以下字段：
- is_safe: boolean，命令是否可以安全执行
- risk_level: string，风险等级（low/medium/high/critical）
- reason: string，判断理由（简短说明）

只输出JSON，不要有其他内容。"""

    def __init__(self):
        self.settings = get_settings()
        self._llm: Optional[ChatOllama] = None

    def _get_llm(self) -> ChatOllama:
        if self._llm is None:
            self._llm = ChatOllama(
                base_url=self.settings.ollama_base_url,
                model=self.settings.ollama_model,
                temperature=0,
            )
        return self._llm

    async def check_command_safety(self, command: str) -> SafetyCheckResult:
        try:
            llm = self._get_llm()
            messages = [
                SystemMessage(content=self.SYSTEM_PROMPT),
                HumanMessage(content=f"请分析以下命令的安全性：\n```\n{command}\n```"),
            ]

            response = await llm.ainvoke(messages)
            content = response.content.strip()

            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

            import json
            try:
                result = json.loads(content)
                return SafetyCheckResult(
                    is_safe=result.get("is_safe", False),
                    reason=result.get("reason", ""),
                    risk_level=result.get("risk_level", "unknown"),
                )
            except json.JSONDecodeError:
                return SafetyCheckResult(
                    is_safe=True,
                    reason="AI响应解析失败，默认允许执行",
                    risk_level="unknown",
                )

        except Exception as e:
            return SafetyCheckResult(
                is_safe=True,
                reason=f"AI检查失败: {str(e)}，默认允许执行",
                risk_level="unknown",
            )

    async def translate_to_command(self, natural_language: str) -> Optional[str]:
        try:
            llm = self._get_llm()
            messages = [
                SystemMessage(content="""你是一个Shell命令生成助手。用户会用自然语言描述他们想要执行的操作，你需要生成对应的shell命令。

规则：
1. 只输出一条shell命令，不要有任何解释
2. 如果无法理解用户意图，输出 "UNKNOWN"
3. 不要生成危险命令
4. 优先使用常见的、安全的命令"""),
                HumanMessage(content=natural_language),
            ]

            response = await llm.ainvoke(messages)
            content = response.content.strip()

            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

            if content == "UNKNOWN" or not content:
                return None

            return content

        except Exception:
            return None
