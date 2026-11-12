from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentResult:
    role: str
    message: str
    tool_calls: list[str]


class AgentOrchestrator:
    """Central, bounded orchestration boundary; services remain deterministic."""

    def __init__(self, max_tool_calls: int = 4) -> None:
        self.max_tool_calls = max_tool_calls

    def chat(self, message: str, context: dict[str, Any]) -> AgentResult:
        lowered = message.lower()
        if "owe" in lowered or "settle" in lowered:
            return AgentResult("settlement", "I can calculate a verified settlement plan from the group balances.", ["get_balances", "optimize_settlement"])
        if "failed" in lowered or "payment" in lowered:
            return AgentResult("reconciliation", "I can inspect verified payment states and prepare recovery.", ["get_payment_status"])
        return AgentResult("expense", "I can prepare an expense draft, then validate it before saving.", ["parse_expense"])
