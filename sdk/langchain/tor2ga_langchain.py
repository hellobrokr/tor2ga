"""
tor2ga LangChain SDK — TorTugaTool + Agent Integration
=======================================================
The 1-line hook:  agent_executor.run("work idle")

This module wraps the tor2ga idle worker as a native LangChain Tool
so any LangChain agent can pick up marketplace jobs autonomously.

Installation
------------
    pip install langchain langchain-openai tor2ga psutil requests

Usage (1-liner with a pre-built agent)
---------------------------------------
    from tor2ga_langchain import build_tor2ga_agent
    agent_executor = build_tor2ga_agent()
    agent_executor.run("Check for idle work")

Or use the tool in your own agent:
    from tor2ga_langchain import TorTugaTool
    tools = [TorTugaTool(), ...your_other_tools...]
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional, Type

# LangChain imports — works with langchain >= 0.1.0
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.tools import BaseTool
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

# tor2ga SDK (same directory or installed)
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))
from tor2ga_hook import (  # noqa: E402
    IdleWorker,
    IdleDetector,
    ExecutionEngine,
    Tor2GAClient,
    configure as tor2ga_configure,
    idle_work,
)

log = logging.getLogger("tor2ga.langchain")

# ---------------------------------------------------------------------------
# Pydantic input schema for the tool
# ---------------------------------------------------------------------------
class TorTugaToolInput(BaseModel):
    action: str = Field(
        default="work",
        description=(
            "Action to perform. Options: "
            "'work' — check for idle state and execute one job; "
            "'status' — return idle/busy status with CPU/mem stats; "
            "'loop' — start continuous poll loop (blocks until interrupted)."
        ),
    )
    force: bool = Field(
        default=False,
        description="If True, skip the idle check and attempt to claim a job regardless.",
    )


# ---------------------------------------------------------------------------
# TorTugaTool — the LangChain Tool
# ---------------------------------------------------------------------------
class TorTugaTool(BaseTool):
    """
    LangChain Tool: tor2ga Idle Work

    When invoked, checks if the host is idle and, if so, claims one job
    from the tor2ga marketplace, executes it, submits the result, and
    returns a JSON summary. The agent can then decide whether to call
    this tool again or move on.

    Attributes
    ----------
    name : str
        Tool name visible to the LangChain agent.
    description : str
        Natural language description the LLM uses to decide when to call it.
    """

    name: str = "tor2ga_idle_work"
    description: str = (
        "Use this tool when you have spare compute capacity and want to earn "
        "passive income by running AI jobs from the tor2ga marketplace. "
        "Returns the result of the job and the bounty earned. "
        "Call with action='status' to check CPU/memory without running a job. "
        "Call with action='work' (default) to claim and execute one marketplace job. "
        "Call with action='loop' to run continuously."
    )
    args_schema: Type[BaseModel] = TorTugaToolInput
    return_direct: bool = False

    # Internal state (not pydantic fields — use __init__ overrides)
    _worker: Optional[IdleWorker] = None

    def __init__(self, worker: Optional[IdleWorker] = None, **kwargs):
        super().__init__(**kwargs)
        self._worker = worker or IdleWorker()

    def _run(
        self,
        action: str = "work",
        force: bool = False,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        """Synchronous execution (required by BaseTool)."""
        worker = self._worker

        if action == "status":
            stats = worker.detector.stats()
            idle  = worker.detector.is_idle()
            return json.dumps({
                "idle":             idle,
                "cpu_pct":          round(stats["cpu_pct"], 1),
                "mem_available_pct": round(stats["mem_available_pct"], 1),
                "recommendation":   "claim a job" if idle else "wait — host is busy",
            }, indent=2)

        if action == "loop":
            log.info("TorTugaTool: starting loop mode — this will block the agent.")
            worker.run_loop()
            return json.dumps({"result": "loop_ended"})

        # action == "work" (default)
        if not force:
            idle_result = worker.detector.is_idle()
            if not idle_result:
                stats = worker.detector.stats()
                return json.dumps({
                    "result":  "skipped",
                    "reason":  "host_busy",
                    "cpu_pct": round(stats["cpu_pct"], 1),
                    "msg":     "Host is too busy. Try again later.",
                })

        # Claim + execute + submit
        start = time.monotonic()
        stats = worker.detector.stats()
        job   = worker.client.claim_job(stats)

        if job is None:
            return json.dumps({
                "result": "no_job",
                "msg":    "No matching jobs on the marketplace right now.",
            })

        result = worker.engine.execute(job)
        submitted = worker.client.submit_result(result)

        return json.dumps({
            "result":       result.status,
            "job_id":       job.job_id,
            "job_title":    job.title,
            "bounty_usd":   job.bounty_usd,
            "payout_usd":   round(job.bounty_usd * 0.8, 4),
            "runtime_secs": result.runtime_secs,
            "submitted":    submitted,
            "output_preview": result.output[:300] if result.output else "",
        }, indent=2)

    async def _arun(self, action: str = "work", force: bool = False, **kwargs) -> str:
        """Async execution — delegates to sync for now."""
        return self._run(action=action, force=force)


# ---------------------------------------------------------------------------
# Pre-built agent factory — the fastest integration path
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an autonomous AI agent with access to the tor2ga marketplace.
Your job is to:
1. Monitor your compute capacity using the tor2ga_idle_work tool (action='status').
2. When idle, claim and execute marketplace jobs (action='work').
3. Report earnings and job outcomes.
4. Keep running until told to stop.

Be proactive. If you are idle, immediately pick up work. If busy, wait and check again.
Always report what job you completed and how much was earned."""

def build_tor2ga_agent(
    openai_model: str = "gpt-4o",
    temperature: float = 0.0,
    extra_tools: list | None = None,
    verbose: bool = True,
) -> AgentExecutor:
    """
    Build and return a ready-to-run LangChain AgentExecutor with the
    TorTugaTool pre-loaded.

    Parameters
    ----------
    openai_model : str
        OpenAI model name. Requires OPENAI_API_KEY env var.
    temperature : float
        LLM temperature. 0.0 = deterministic.
    extra_tools : list
        Additional LangChain tools to include alongside TorTugaTool.
    verbose : bool
        Whether to print agent thought process.

    Returns
    -------
    AgentExecutor
        Ready to call with agent_executor.run("Check for idle work")

    Example
    -------
        agent_executor = build_tor2ga_agent()
        agent_executor.run("Check for idle work")   # THE 1-LINE HOOK
    """
    llm = ChatOpenAI(model=openai_model, temperature=temperature)

    tools = [TorTugaTool()] + (extra_tools or [])

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder("chat_history", optional=True),
        ("human",  "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_openai_tools_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=verbose, handle_parsing_errors=True)


# ---------------------------------------------------------------------------
# Standalone tool usage (no full agent)
# ---------------------------------------------------------------------------
def tor2ga_tool_call(action: str = "work", force: bool = False) -> dict:
    """
    Direct programmatic tool call — no agent overhead needed.

    Example
    -------
        result = tor2ga_tool_call()
        print(result["payout_usd"])
    """
    tool = TorTugaTool()
    raw  = tool._run(action=action, force=force)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("  tor2ga LangChain SDK — TorTugaTool Demo")
    print("=" * 60)

    # ── Option A: Full agent (1-line hook) ───────────────────────
    # Requires OPENAI_API_KEY + TOR2GA_API_KEY
    print("\n── Option A: AgentExecutor (1-line hook) ──")
    print("Code:")
    print("  agent_executor = build_tor2ga_agent()")
    print("  agent_executor.run('Check for idle work')  # THE 1-LINER")

    # Uncomment to actually run (needs API keys):
    # agent_executor = build_tor2ga_agent()
    # result = agent_executor.run("Check for idle work")
    # print(result)

    # ── Option B: Tool-only (no LLM overhead) ────────────────────
    print("\n── Option B: Direct tool call ──")
    print("Code:")
    print("  result = tor2ga_tool_call()")
    print("  print(result)")

    # Standalone tool status check (works without API key in demo mode):
    tool = TorTugaTool(worker=IdleWorker(
        client=Tor2GAClient.__new__(Tor2GAClient),  # mock
    ))

    # Show status without making real API calls
    print("\n── Option C: Embed TorTugaTool in your own agent ──")
    print("Code:")
    print("  from tor2ga_langchain import TorTugaTool")
    print("  tools = [TorTugaTool(), search_tool, calculator_tool]")
    print("  agent = create_openai_tools_agent(llm, tools, prompt)")
    print("  executor = AgentExecutor(agent=agent, tools=tools)")
    print("  executor.run('Do idle work then search for today news')")
