from __future__ import annotations

import shlex

from ctf_agent.agents.base import Agent, AgentContext
from ctf_agent.agents.planner import Plan, PlanCommand
from ctf_agent.core.trace import TraceEvent


class CriticAgent(Agent):
    def __init__(self) -> None:
        super().__init__("critic", "Review repeated failures and suggest an alternate safe route.")

    def run(self, context: AgentContext) -> Plan:
        failures = context.message_bus.by_kind("failure_reason") if context.message_bus else []
        code = (
            "from pathlib import Path; "
            "import re; "
            "pat=re.compile(r'(flag|FLAG|ctf|CTF)\\{[^}\\s]{1,200}\\}'); "
            "seen=set(); "
            "\nfor p in Path('.').rglob('*'):\n"
            "    if p.is_file():\n"
            "        text=p.read_bytes()[:1000000].decode('utf-8','replace')\n"
            "        for m in pat.finditer(text):\n"
            "            v=m.group(0)\n"
            "            if v not in seen:\n"
            "                seen.add(v); print(f'{p}: {v}')"
        )
        plan = Plan(
            rationale=f"Critic observed {len(failures)} failure(s); try a broad non-destructive workspace flag scan.",
            commands=[
                PlanCommand(
                    command="python3 -c " + shlex.quote(code),
                    reason="Alternative strategy after repeated failures: scan all workspace files for common flag patterns.",
                    timeout=context.timeout,
                    metadata={"source": "critic", "failure_count": len(failures)},
                )
            ],
            metadata={"source": "critic", "failure_count": len(failures), "failures": [failure.to_dict() for failure in failures]},
        )
        if context.message_bus:
            context.message_bus.add_hypothesis(self.name, plan.rationale, failure_count=len(failures))
        context.trace_store.append(
            TraceEvent(
                challenge_id=context.state.challenge.id,
                agent=self.name,
                action="critic-plan",
                stdout=plan.rationale,
                metadata={"plan": plan.to_dict()},
            )
        )
        return plan
