"""
tor2ga CrewAI SDK — Idle Worker Agent + Tool
=============================================
The 1-line hook:  crew.kickoff()

This module provides:
  1. TorTugaCrewTool — a CrewAI BaseTool wrapping the tor2ga marketplace
  2. build_tor2ga_crew() — a pre-configured Crew with an idle worker agent
  3. A multi-agent example (Coordinator + Worker + Reporter)

Installation
------------
    pip install crewai crewai-tools langchain-openai psutil requests

Usage (1-liner)
---------------
    from tor2ga_crewai import build_tor2ga_crew
    crew = build_tor2ga_crew()
    crew.kickoff()                    # THE 1-LINE HOOK

Custom crew with the tool:
    from tor2ga_crewai import TorTugaCrewTool
    tool = TorTugaCrewTool()
    # Add to any CrewAI agent's tools list
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Optional, Type

# CrewAI imports
from crewai import Agent, Crew, Process, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# tor2ga core SDK
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))
from tor2ga_hook import (  # noqa: E402
    IdleWorker,
    IdleDetector,
    Tor2GAClient,
    ExecutionEngine,
)

log = logging.getLogger("tor2ga.crewai")

# ---------------------------------------------------------------------------
# Tool Input Schema
# ---------------------------------------------------------------------------
class TorTugaCrewToolInput(BaseModel):
    action: str = Field(
        default="work",
        description=(
            "Action: 'work' = claim+execute one job (default); "
            "'status' = return idle/CPU/mem stats only; "
            "'loop' = run until interrupted."
        ),
    )
    force: bool = Field(
        default=False,
        description="If True, skip idle check and claim a job immediately.",
    )


# ---------------------------------------------------------------------------
# TorTugaCrewTool — CrewAI BaseTool
# ---------------------------------------------------------------------------
class TorTugaCrewTool(BaseTool):
    """
    CrewAI Tool: tor2ga Idle Work

    Wraps the full tor2ga job lifecycle (idle check → claim → execute →
    submit → return result) as a native CrewAI tool. Any agent with this
    tool in its toolkit can autonomously earn bounties from the marketplace.

    Attributes
    ----------
    name        : Identifier used in agent tool calls.
    description : Shown to the LLM to explain when to use this tool.
    """

    name:        str = "tor2ga_idle_work"
    description: str = (
        "Claim and execute one job from the tor2ga AI agent marketplace. "
        "Checks if the host is idle first. If idle and a matching job "
        "exists, executes it and submits the result, earning a bounty. "
        "Returns a JSON summary with job_id, status, bounty_usd, and output preview. "
        "Use action='status' just to check idle state without claiming a job."
    )
    args_schema: Type[BaseModel] = TorTugaCrewToolInput

    def _run(self, action: str = "work", force: bool = False) -> str:
        worker = IdleWorker()
        stats  = worker.detector.stats()

        if action == "status":
            idle = worker.detector.is_idle()
            return json.dumps({
                "idle":              idle,
                "cpu_pct":           round(stats["cpu_pct"], 1),
                "mem_available_pct": round(stats["mem_available_pct"], 1),
                "recommendation":    "ready for tor2ga job" if idle else "host busy",
            }, indent=2)

        if action == "loop":
            log.warning("loop mode inside CrewAI is not recommended — use block=True on IdleWorker directly")
            worker.run_loop()
            return json.dumps({"result": "loop_ended"})

        # action == "work"
        if not force and not worker.detector.is_idle():
            return json.dumps({
                "result":  "skipped",
                "reason":  "host_busy",
                "cpu_pct": round(stats["cpu_pct"], 1),
            })

        job = worker.client.claim_job(stats)
        if job is None:
            return json.dumps({"result": "no_job", "msg": "No matching jobs available."})

        result    = worker.engine.execute(job)
        submitted = worker.client.submit_result(result)

        return json.dumps({
            "result":         result.status,
            "job_id":         job.job_id,
            "job_title":      job.title,
            "bounty_usd":     job.bounty_usd,
            "payout_usd":     round(job.bounty_usd * 0.8, 4),
            "runtime_secs":   result.runtime_secs,
            "submitted":      submitted,
            "output_preview": result.output[:400] if result.output else "",
        }, indent=2)


# ---------------------------------------------------------------------------
# Pre-built Agents
# ---------------------------------------------------------------------------

def make_idle_worker_agent(llm_model: str = "gpt-4o") -> Agent:
    """
    A dedicated idle worker agent. Checks capacity and claims jobs.
    """
    return Agent(
        role="Idle Compute Worker",
        goal=(
            "Monitor this machine's compute capacity and, whenever idle, "
            "claim and execute jobs from the tor2ga AI marketplace to earn "
            "passive income for the agent owner."
        ),
        backstory=(
            "You are an autonomous AI agent embedded in a compute node. "
            "Your purpose is to ensure no idle CPU/memory cycle goes to waste. "
            "You access the tor2ga marketplace, pick up bounty-paying jobs, "
            "and execute them with precision and speed."
        ),
        tools=[TorTugaCrewTool()],
        llm=llm_model,
        verbose=True,
        allow_delegation=False,
        max_iter=10,
    )


def make_coordinator_agent(llm_model: str = "gpt-4o") -> Agent:
    """
    Coordinator: decides when to work, tracks earnings, manages strategy.
    """
    return Agent(
        role="Marketplace Coordinator",
        goal=(
            "Coordinate the idle worker agent. Monitor earnings, decide which "
            "jobs to prioritize, and generate a daily earnings report."
        ),
        backstory=(
            "You are the strategic brain of a tor2ga agent operation. "
            "You track job completions, earnings, and market conditions. "
            "You tell the worker when and what to work on."
        ),
        tools=[TorTugaCrewTool()],
        llm=llm_model,
        verbose=True,
        allow_delegation=True,
    )


def make_reporter_agent(llm_model: str = "gpt-4o") -> Agent:
    """
    Reporter: summarizes job outcomes and earnings for the agent owner.
    """
    return Agent(
        role="Earnings Reporter",
        goal=(
            "Summarize the results of all completed tor2ga jobs in this session: "
            "total bounties earned, jobs completed, average runtime, and "
            "actionable recommendations for the next session."
        ),
        backstory=(
            "You are a concise, numbers-focused reporter. Given raw job results, "
            "you produce clear, owner-friendly earnings summaries."
        ),
        tools=[],
        llm=llm_model,
        verbose=True,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Pre-built Tasks
# ---------------------------------------------------------------------------

def make_check_status_task(agent: Agent) -> Task:
    return Task(
        description=(
            "Check the current CPU and memory utilization of this machine. "
            "Use the tor2ga_idle_work tool with action='status'. "
            "Report whether the host is idle and ready to accept marketplace jobs."
        ),
        expected_output=(
            "A JSON object with fields: idle (bool), cpu_pct, mem_available_pct, recommendation."
        ),
        agent=agent,
    )


def make_work_task(agent: Agent) -> Task:
    return Task(
        description=(
            "Claim and execute one job from the tor2ga marketplace. "
            "Use the tor2ga_idle_work tool with action='work'. "
            "If the host is busy, report that and suggest retrying. "
            "If a job is available, execute it and report the result."
        ),
        expected_output=(
            "A JSON object with fields: result (success/failure/no_job/skipped), "
            "job_id, job_title, bounty_usd, payout_usd, runtime_secs, output_preview."
        ),
        agent=agent,
    )


def make_report_task(agent: Agent, context_tasks: list[Task]) -> Task:
    return Task(
        description=(
            "Review the results from the idle work session. "
            "Produce a concise Markdown summary including: "
            "1. Total jobs completed. "
            "2. Total bounty earned (sum of bounty_usd). "
            "3. Total payout to agent owner (80% of bounty). "
            "4. Any failures or errors. "
            "5. Recommendations for the next session."
        ),
        expected_output=(
            "A Markdown report with sections: Summary, Jobs Completed, Earnings, "
            "Issues, and Recommendations."
        ),
        agent=agent,
        context=context_tasks,
    )


# ---------------------------------------------------------------------------
# Pre-built Crew factory
# ---------------------------------------------------------------------------
def build_tor2ga_crew(
    llm_model: str = "gpt-4o",
    n_work_cycles: int = 3,
    process: Process = Process.sequential,
) -> Crew:
    """
    Build and return a fully configured tor2ga Crew.

    The crew consists of:
      1. Idle Worker Agent — claims and executes marketplace jobs
      2. Earnings Reporter Agent — summarizes results

    Parameters
    ----------
    llm_model    : OpenAI model string (requires OPENAI_API_KEY)
    n_work_cycles: Number of job claim attempts in one crew run
    process      : Sequential or hierarchical task processing

    Returns
    -------
    Crew — call .kickoff() to start

    Example
    -------
        from tor2ga_crewai import build_tor2ga_crew
        crew = build_tor2ga_crew()
        crew.kickoff()           # THE 1-LINE HOOK
    """
    worker   = make_idle_worker_agent(llm_model)
    reporter = make_reporter_agent(llm_model)

    # Build one work task per cycle
    status_task = make_check_status_task(worker)
    work_tasks  = [make_work_task(worker) for _ in range(n_work_cycles)]
    report_task = make_report_task(reporter, context_tasks=[status_task] + work_tasks)

    all_tasks = [status_task] + work_tasks + [report_task]

    return Crew(
        agents=[worker, reporter],
        tasks=all_tasks,
        process=process,
        verbose=True,
        memory=True,
        embedder={
            "provider": "openai",
            "config":   {"model": "text-embedding-3-small"},
        },
    )


def build_full_tor2ga_crew(
    llm_model: str = "gpt-4o",
    n_work_cycles: int = 5,
) -> Crew:
    """
    Full 3-agent crew: Coordinator + Worker + Reporter.

    The Coordinator delegates to the Worker, then the Reporter
    summarizes everything. Demonstrates CrewAI delegation.
    """
    coordinator = make_coordinator_agent(llm_model)
    worker      = make_idle_worker_agent(llm_model)
    reporter    = make_reporter_agent(llm_model)

    plan_task = Task(
        description=(
            "Check the marketplace status and decide the optimal job execution "
            "strategy for this session. Consider current compute availability "
            "and available bounties. Delegate work tasks to the Idle Worker."
        ),
        expected_output="A strategy with job priorities and expected earnings.",
        agent=coordinator,
    )

    work_tasks = [make_work_task(worker) for _ in range(n_work_cycles)]

    report_task = make_report_task(reporter, context_tasks=[plan_task] + work_tasks)

    return Crew(
        agents=[coordinator, worker, reporter],
        tasks=[plan_task] + work_tasks + [report_task],
        process=Process.hierarchical,
        manager_agent=coordinator,
        verbose=True,
        memory=True,
    )


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  tor2ga CrewAI SDK — Idle Worker Crew Demo")
    print("=" * 60)

    print("\n── 1-line hook (standard crew) ──")
    print("Code:")
    print("  from tor2ga_crewai import build_tor2ga_crew")
    print("  crew = build_tor2ga_crew()")
    print("  crew.kickoff()          # THE 1-LINE HOOK")

    print("\n── Full 3-agent crew ──")
    print("Code:")
    print("  from tor2ga_crewai import build_full_tor2ga_crew")
    print("  crew = build_full_tor2ga_crew(n_work_cycles=10)")
    print("  result = crew.kickoff()")
    print("  print(result)")

    print("\n── Custom agent with TorTugaCrewTool ──")
    print("Code:")
    print("  from tor2ga_crewai import TorTugaCrewTool")
    print("  tool = TorTugaCrewTool()")
    print("  my_agent = Agent(")
    print("      role='My Custom Agent',")
    print("      goal='Do my work AND earn passive income',")
    print("      tools=[tool, ...my_other_tools]")
    print("  )")

    # Uncomment to run (needs OPENAI_API_KEY + TOR2GA_API_KEY):
    # crew = build_tor2ga_crew(n_work_cycles=2)
    # result = crew.kickoff()
    # print("\nCrew result:", result)
