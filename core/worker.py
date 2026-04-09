#!/usr/bin/env python3
"""
tor2ga.ai — Agent Worker
A standalone worker that connects to the tor2ga marketplace server, claims jobs,
executes them (simulated LLM-style output), and submits results.

Usage:
    python worker.py

Environment Variables:
    TOR2GA_SERVER_URL           — Server base URL (default: http://localhost:8420)
    TOR2GA_API_KEY              — Your API key (required after first run)
    TOR2GA_AGENT_NAME           — Worker agent name (default: AutoWorker-<hostname>)
    TOR2GA_AGENT_CAPABILITIES   — Comma-separated list of capabilities
    TOR2GA_POLL_INTERVAL        — Seconds between polls (default: 5)

First run workflow:
    1. If TOR2GA_API_KEY is not set, the worker auto-registers a user account
       and saves credentials to ~/.tor2ga/worker_creds.json
    2. Registers an agent under that account
    3. Enters the polling loop
"""

from __future__ import annotations

import json
import os
import random
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    print("httpx is required: pip install httpx")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("rich is required: pip install rich")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVER_URL: str = os.getenv("TOR2GA_SERVER_URL", "http://localhost:8420").rstrip("/")
API_KEY: Optional[str] = os.getenv("TOR2GA_API_KEY")
AGENT_NAME: str = os.getenv(
    "TOR2GA_AGENT_NAME",
    f"AutoWorker-{socket.gethostname()}",
)
RAW_CAPS: str = os.getenv(
    "TOR2GA_AGENT_CAPABILITIES",
    "research,data-analysis,coding,writing,summarization",
)
CAPABILITIES: List[str] = [c.strip() for c in RAW_CAPS.split(",") if c.strip()]
POLL_INTERVAL: int = int(os.getenv("TOR2GA_POLL_INTERVAL", "5"))

CREDS_FILE = Path.home() / ".tor2ga" / "worker_creds.json"

# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# HTTP client helpers
# ---------------------------------------------------------------------------


def make_headers(api_key: str) -> Dict[str, str]:
    return {"X-API-Key": api_key, "Content-Type": "application/json"}


def api_get(path: str, params: Optional[Dict] = None, api_key: Optional[str] = None) -> Any:
    headers = make_headers(api_key) if api_key else {"Content-Type": "application/json"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{SERVER_URL}{path}", params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


def api_post(path: str, payload: Dict, api_key: Optional[str] = None) -> Any:
    headers = make_headers(api_key) if api_key else {"Content-Type": "application/json"}
    with httpx.Client(timeout=60) as client:
        resp = client.post(f"{SERVER_URL}{path}", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------


def load_creds() -> Optional[Dict[str, str]]:
    """Load saved credentials from disk."""
    if CREDS_FILE.exists():
        try:
            return json.loads(CREDS_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_creds(creds: Dict[str, str]) -> None:
    """Persist credentials to disk."""
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(json.dumps(creds, indent=2))


def ensure_credentials() -> Tuple[str, str]:
    """
    Returns (api_key, agent_id).
    Tries env vars first, then saved creds file, then auto-registers.
    """
    global API_KEY

    # 1. Check env var
    if API_KEY:
        creds = load_creds()
        if creds and creds.get("api_key") == API_KEY and creds.get("agent_id"):
            return API_KEY, creds["agent_id"]
        # API key from env but no agent registered — register agent
        agent_id = register_agent(API_KEY)
        save_creds({"api_key": API_KEY, "agent_id": agent_id})
        return API_KEY, agent_id

    # 2. Check saved creds
    creds = load_creds()
    if creds and creds.get("api_key") and creds.get("agent_id"):
        API_KEY = creds["api_key"]
        console.print(
            f"[dim]  Loaded saved credentials from [cyan]{CREDS_FILE}[/cyan][/dim]"
        )
        return creds["api_key"], creds["agent_id"]

    # 3. Auto-register user + agent
    console.print(
        Panel(
            "[yellow]No credentials found.[/yellow]\n"
            "Auto-registering a new user account and agent...",
            title="[bold]First Run Setup[/bold]",
            border_style="yellow",
        )
    )
    api_key, user_id = register_user()
    agent_id = register_agent(api_key)
    API_KEY = api_key
    save_creds({"api_key": api_key, "user_id": user_id, "agent_id": agent_id})
    console.print(
        f"[green]  Credentials saved to [cyan]{CREDS_FILE}[/cyan][/green]\n"
        f"  Set [bold]TOR2GA_API_KEY={api_key}[/bold] to reuse this account.\n"
    )
    return api_key, agent_id


def register_user() -> Tuple[str, str]:
    """Auto-register a new user. Returns (api_key, user_id)."""
    timestamp = int(time.time())
    username = f"worker_{timestamp}"
    email = f"{username}@autoworker.local"
    payload = {"username": username, "email": email, "role": "agent_owner"}
    console.print(f"  Registering user [bold]{username}[/bold]...", end=" ")
    try:
        data = api_post("/api/v1/auth/register", payload)
        console.print("[green]✓[/green]")
        return data["api_key"], data["id"]
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]✗[/red]")
        console.print(f"[red]  Registration failed: {exc.response.text}[/red]")
        sys.exit(1)


def register_agent(api_key: str) -> str:
    """Register this worker as an agent. Returns agent_id."""
    payload = {
        "name": AGENT_NAME,
        "description": (
            f"Autonomous agent worker running on {socket.gethostname()}. "
            f"Specialises in: {', '.join(CAPABILITIES)}."
        ),
        "capabilities": CAPABILITIES,
    }
    console.print(f"  Registering agent [bold]{AGENT_NAME}[/bold]...", end=" ")
    try:
        data = api_post("/api/v1/agents", payload, api_key=api_key)
        console.print("[green]✓[/green]")
        return data["id"]
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]✗[/red]")
        console.print(f"[red]  Agent registration failed: {exc.response.text}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Job execution (simulated LLM-style output)
# ---------------------------------------------------------------------------

EXECUTION_TEMPLATES: Dict[str, List[str]] = {
    "research": [
        """# Research Report: {title}

## Executive Summary
Comprehensive multi-source analysis covering {title}. Key findings synthesised from
{n_sources} sources across {n_regions} regions. Market CAGR estimated at {cagr}% with
TAM of ${tam}B for FY2025.

## Methodology
Primary data from {n_primary} sources; secondary from {n_secondary}. Statistical
regression and Monte Carlo simulation applied (confidence threshold: 85%).

## Key Findings
- Enterprise adoption grew {adoption}% YoY driven by automation mandates
- VC investment reached ${vc}B in 2024; median Series B up 34%
- {n_competitors} competitive players identified across 4 tiers
- Open-source alternatives growing at 156% annually in this vertical

## Recommendations
1. Prioritise tier-2 challengers for partnership opportunities
2. Monitor EU AI Act compliance requirements — creates barriers benefiting incumbents
3. Invest in developer community tooling to capture open-source mindshare
4. Target {adoption}%+ YoY adoption acceleration in enterprise segment

## Conclusion
The analysis confirms strong market momentum. Recommended action: immediate strategic
positioning before Q3 consolidation wave.

*Processing time: {proc_time}s | Sources analysed: {n_sources}*
""",
    ],
    "coding": [
        """# Implementation: {title}

## Solution Overview
Delivered a production-ready implementation addressing all {n_req} stated requirements
plus {n_implicit} implicit constraints identified during analysis.

## Architecture
```
├── core/
│   ├── engine.py          # Main processing logic
│   ├── validators.py      # Input validation layer
│   └── utils.py           # Helper utilities
├── tests/
│   └── test_core.py       # {coverage}% test coverage
└── README.md
```

## Implementation Details
- **Language**: Python 3.11+ with full type annotations
- **Dependencies**: Minimal — only stdlib + one optional accelerator
- **Performance**: Processes {n_records} records in {proc_time}s ({throughput} rec/s)
- **Error handling**: Structured exception hierarchy with detailed logging

## Key Design Decisions
1. Chose iterator-based processing to keep memory footprint under 50MB
2. Implemented retry logic with exponential backoff for external calls
3. All I/O operations are async-compatible via protocol abstraction
4. Zero global state — all configuration injected via constructor

## Test Results
- Unit tests: {coverage}% coverage, 0 failures
- Integration tests: All {n_req} scenarios pass
- Performance benchmark: Within SLA on 99th percentile

*Processing time: {proc_time}s | Test coverage: {coverage}%*
""",
    ],
    "writing": [
        """# {title}

## Overview
{title} requires a nuanced approach balancing technical accuracy with accessible
prose. This deliverable meets all {n_req} content requirements.

## Content

The landscape has shifted dramatically. What was once a niche consideration has
become a central strategic imperative for organisations navigating an increasingly
complex environment. Three forces are converging to make this transformation
both urgent and unavoidable.

**First**, the acceleration of underlying technology has outpaced traditional
planning cycles. Organisations that move in 18-month strategy windows are
operating at a structural disadvantage against competitors iterating in weeks.

**Second**, stakeholder expectations have evolved. Customers, investors, and
regulators now demand transparency, speed, and demonstrable value — simultaneously.

**Third**, the talent dynamics have inverted. The scarcest resource is no longer
capital or technology; it is the capacity to synthesise complex signals into
decisive action.

## Key Messages
- Lead with outcomes, not process
- Quantify impact wherever possible
- Acknowledge uncertainty — it builds credibility
- Close with a concrete next step

## SEO / Readability
- Flesch-Kincaid Grade Level: 11.2 (target audience: professional)
- Average sentence length: 18 words
- Active voice ratio: 78%

*Word count: {word_count} | Processing time: {proc_time}s*
""",
    ],
    "data": [
        """# Data Analysis Report: {title}

## Summary Statistics
Analysed dataset with {n_records} records across {n_dimensions} dimensions.

| Metric | Value |
|--------|-------|
| Records processed | {n_records:,} |
| Features analysed | {n_dimensions} |
| Outliers detected | {n_outliers} ({outlier_pct}%) |
| Missing values imputed | {n_missing} |
| Processing time | {proc_time}s |

## Distributions
- Target variable: approximately normal (skew: 0.23, kurtosis: 2.87)
- {top_feature} identified as highest-importance feature (gain: 0.31)
- Multicollinearity detected between feature groups A and C (VIF > 5)

## Anomaly Detection
{n_anomalies} anomalies flagged using IQR + z-score ensemble method.
Revenue at risk from anomalous records: ${at_risk_rev}K.
Recommended action: manual review of top-{review_count} flagged records.

## Predictive Model Performance
- Algorithm: Gradient Boosted Trees (XGBoost 2.0)
- Cross-validation AUC: 0.{auc_decimal}
- Precision: 0.{prec_decimal} | Recall: 0.{rec_decimal} | F1: 0.{f1_decimal}

## Recommendations
1. Re-train monthly to account for data drift
2. Exclude outlier cluster #3 from production scoring
3. Add {n_features} engineered features from temporal patterns

*Processed {n_records:,} records in {proc_time}s*
""",
    ],
    "default": [
        """# Task Completion: {title}

## Summary
Task completed successfully. All {n_req} requirements have been addressed.

## Work Performed
Systematic analysis and execution of the requested task, applying best-practice
methodologies appropriate to the problem domain.

### Phase 1: Analysis
- Decomposed requirements into {n_steps} actionable sub-tasks
- Identified {n_implicit} implicit constraints and edge cases
- Established success criteria and verification checkpoints

### Phase 2: Execution
- Implemented primary solution pathway
- Validated against all stated requirements
- Applied quality assurance checks at each milestone

### Phase 3: Delivery
- Output verified against specification
- Documentation included for all non-obvious decisions
- Recommendations for future iterations provided

## Quality Assurance
- All {n_req} stated requirements: [PASS]
- Edge case coverage: {coverage}%
- Performance within bounds: [PASS]

## Recommendations
1. Monitor output quality over first 30 days of deployment
2. Establish feedback loop for continuous improvement
3. Review edge case handling quarterly

*Completed in {proc_time}s*
""",
    ],
}


def generate_output(job: Dict[str, Any]) -> str:
    """Generate a realistic simulated output for a job."""
    category = (job.get("category") or "default").lower()
    templates = EXECUTION_TEMPLATES.get(category, EXECUTION_TEMPLATES["default"])
    template = random.choice(templates)

    # Fill template variables
    params = {
        "title": job.get("title", "Untitled Task"),
        "proc_time": round(random.uniform(2.1, 18.7), 2),
        "n_sources": random.randint(24, 67),
        "n_primary": random.randint(8, 20),
        "n_secondary": random.randint(15, 45),
        "n_regions": random.randint(3, 8),
        "n_competitors": random.randint(8, 28),
        "n_records": random.randint(15000, 350000),
        "n_dimensions": random.randint(12, 64),
        "n_features": random.randint(5, 20),
        "cagr": round(random.uniform(14.5, 38.2), 1),
        "tam": random.randint(12, 95),
        "adoption": random.randint(18, 67),
        "vc": round(random.uniform(2.3, 18.7), 1),
        "n_req": random.randint(6, 18),
        "n_implicit": random.randint(2, 6),
        "n_steps": random.randint(4, 9),
        "coverage": round(random.uniform(94.0, 100.0), 1),
        "n_outliers": random.randint(120, 890),
        "outlier_pct": round(random.uniform(1.5, 4.8), 1),
        "n_missing": random.randint(200, 1500),
        "n_anomalies": random.randint(12, 45),
        "at_risk_rev": random.randint(200, 900),
        "review_count": random.randint(10, 30),
        "auc_decimal": random.randint(89, 97),
        "prec_decimal": random.randint(82, 95),
        "rec_decimal": random.randint(79, 93),
        "f1_decimal": random.randint(80, 94),
        "throughput": f"{random.randint(8000, 45000):,}",
        "top_feature": random.choice(
            ["tenure_days", "session_frequency", "revenue_per_user", "churn_risk_score"]
        ),
        "word_count": random.randint(800, 2400),
    }
    try:
        return template.format(**params)
    except KeyError:
        return template


def simulate_work(job: Dict[str, Any], progress: Progress, task_id: Any) -> str:
    """Simulate work execution with progress updates. Returns output text."""
    steps = [
        "Parsing job requirements",
        "Loading knowledge base",
        "Analysing task complexity",
        "Generating solution framework",
        "Executing primary pipeline",
        "Running quality checks",
        "Formatting output",
        "Finalising deliverable",
    ]

    total = len(steps)
    for i, step in enumerate(steps):
        progress.update(task_id, description=f"[cyan]{step}...[/cyan]", completed=i)
        time.sleep(random.uniform(0.3, 1.2))

    progress.update(task_id, completed=total, description="[green]Complete[/green]")
    return generate_output(job)


# ---------------------------------------------------------------------------
# Worker loop state
# ---------------------------------------------------------------------------


class WorkerState:
    def __init__(self):
        self.jobs_completed: int = 0
        self.jobs_failed: int = 0
        self.total_earned: float = 0.0
        self.polls: int = 0
        self.start_time: float = time.monotonic()

    @property
    def uptime_str(self) -> str:
        elapsed = int(time.monotonic() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def print_banner(agent_name: str, agent_id: str, server_url: str) -> None:
    console.print()
    console.print(
        Panel(
            f"[bold cyan]tor2ga.ai[/bold cyan] [white]Agent Worker[/white]\n\n"
            f"  Agent   [bold]{agent_name}[/bold]\n"
            f"  ID      [dim]{agent_id}[/dim]\n"
            f"  Server  [blue]{server_url}[/blue]\n"
            f"  Caps    [yellow]{', '.join(CAPABILITIES)}[/yellow]\n"
            f"  Poll    [white]{POLL_INTERVAL}s[/white]",
            title="[bold]Worker Started[/bold]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


def print_poll(state: WorkerState) -> None:
    console.print(
        f"  [dim]{datetime.now(timezone.utc).strftime('%H:%M:%S')}[/dim]  "
        f"[white]Polling...[/white]  "
        f"[dim]polls={state.polls}  "
        f"done={state.jobs_completed}  "
        f"failed={state.jobs_failed}  "
        f"earned=${state.total_earned:.2f}  "
        f"up={state.uptime_str}[/dim]"
    )


def print_job_claimed(job: Dict[str, Any], message: str) -> None:
    console.print()
    console.print(Rule(style="yellow"))
    console.print(
        Panel(
            f"  [bold green]JOB CLAIMED[/bold green]\n\n"
            f"  Title    [bold]{job.get('title', 'N/A')}[/bold]\n"
            f"  ID       [dim]{job.get('id', 'N/A')}[/dim]\n"
            f"  Category [yellow]{job.get('category', 'N/A')}[/yellow]\n"
            f"  Bounty   [green bold]${job.get('bounty_usd', 0):.2f}[/green bold]\n"
            f"  Priority [cyan]{job.get('priority', 'normal')}[/cyan]\n"
            f"  [dim]{message}[/dim]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def print_submission_result(
    verification: Dict[str, Any],
    payout: Optional[Dict[str, Any]],
    state: WorkerState,
) -> None:
    passed = verification.get("passed", False)
    score = verification.get("score", 0.0)
    border = "green" if passed else "red"
    status_text = "[bold green]PASSED[/bold green]" if passed else "[bold red]FAILED[/bold red]"

    payout_line = ""
    if payout:
        earned = payout.get("agent_payout_usd", 0.0)
        payout_line = f"\n  Payout   [bold green]+${earned:.2f}[/bold green]"

    console.print(
        Panel(
            f"  Verification  {status_text}\n"
            f"  Score         [white]{score:.3f}[/white]\n"
            f"  Notes         [dim]{verification.get('notes', '')[:80]}[/dim]"
            f"{payout_line}\n\n"
            f"  Total jobs completed: [bold]{state.jobs_completed}[/bold]  "
            f"Total earned: [bold green]${state.total_earned:.2f}[/bold green]",
            title="[bold]Submission Result[/bold]",
            border_style=border,
            padding=(1, 2),
        )
    )
    console.print(Rule(style="dim"))
    console.print()


def print_error_panel(context: str, detail: str) -> None:
    console.print(
        Panel(
            f"[red]{detail}[/red]",
            title=f"[bold red]Error: {context}[/bold red]",
            border_style="red",
        )
    )


# ---------------------------------------------------------------------------
# Server health check
# ---------------------------------------------------------------------------


def wait_for_server(max_attempts: int = 20) -> None:
    """Block until the server is reachable, with visual feedback."""
    console.print(f"  Connecting to [blue]{SERVER_URL}[/blue]...", end=" ")
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=3) as client:
                resp = client.get(f"{SERVER_URL}/health")
                if resp.status_code == 200:
                    console.print("[green]✓ Connected[/green]")
                    return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(1)
        if attempt == 0:
            console.print()
        console.print(f"  Waiting for server (attempt {attempt + 1}/{max_attempts})...", end="\r")
    console.print(f"\n[red]  Could not connect to {SERVER_URL} after {max_attempts} attempts.[/red]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Core polling iteration
# ---------------------------------------------------------------------------


def poll_once(api_key: str, agent_id: str, state: WorkerState) -> None:
    """One polling cycle: heartbeat → claim → execute → submit."""
    state.polls += 1
    print_poll(state)

    # Heartbeat
    try:
        api_post(f"/api/v1/agents/{agent_id}/heartbeat", {})
    except httpx.HTTPStatusError as exc:
        console.print(f"  [dim yellow]Heartbeat warning: {exc.response.status_code}[/dim yellow]")
    except httpx.RequestError:
        console.print("  [dim yellow]Heartbeat skipped (network error)[/dim yellow]")

    # Claim a job
    try:
        claim_resp = api_post(
            "/api/v1/marketplace/claim",
            {"agent_id": agent_id},
            api_key=api_key,
        )
    except httpx.HTTPStatusError as exc:
        print_error_panel("Claim", f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        return
    except httpx.RequestError as exc:
        print_error_panel("Claim", f"Network error: {exc}")
        return

    if not claim_resp.get("claimed"):
        return  # No job available — silent, already shown in poll line

    job = claim_resp["job"]
    print_job_claimed(job, claim_resp.get("message", ""))

    # Execute (simulate work)
    with Progress(
        SpinnerColumn(spinner_name="dots", style="cyan"),
        TextColumn("  "),
        TextColumn("{task.description}"),
        BarColumn(bar_width=40, style="cyan", complete_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task_id = progress.add_task("[cyan]Starting...[/cyan]", total=8)
        output_text = simulate_work(job, progress, task_id)

    console.print(f"  [dim]Output length: {len(output_text.split())} words[/dim]")

    # Submit
    try:
        submit_resp = api_post(
            "/api/v1/marketplace/submit",
            {
                "job_id": job["id"],
                "agent_id": agent_id,
                "output_text": output_text,
                "output_files": [],
            },
            api_key=api_key,
        )
    except httpx.HTTPStatusError as exc:
        print_error_panel(
            "Submit", f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        )
        state.jobs_failed += 1
        return
    except httpx.RequestError as exc:
        print_error_panel("Submit", f"Network error: {exc}")
        state.jobs_failed += 1
        return

    verification = submit_resp.get("verification", {})
    payout = submit_resp.get("payout")
    passed = verification.get("passed", False)

    if passed:
        state.jobs_completed += 1
        if payout:
            state.total_earned += payout.get("agent_payout_usd", 0.0)
    else:
        state.jobs_failed += 1

    print_submission_result(verification, payout, state)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    console.print()
    console.print(Rule("[bold cyan]tor2ga.ai Agent Worker[/bold cyan]"))

    # Wait for server
    wait_for_server()

    # Bootstrap credentials
    api_key, agent_id = ensure_credentials()

    # Print startup banner
    print_banner(AGENT_NAME, agent_id, SERVER_URL)

    console.print(
        f"  [dim]Polling every [white]{POLL_INTERVAL}s[/white]. Press "
        f"[bold]Ctrl+C[/bold] to stop.[/dim]\n"
    )

    state = WorkerState()

    try:
        while True:
            try:
                poll_once(api_key, agent_id, state)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print_error_panel("Unexpected error in poll loop", str(exc))
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        console.print()
        console.print(Rule(style="dim"))
        console.print(
            Panel(
                f"  Jobs completed : [bold green]{state.jobs_completed}[/bold green]\n"
                f"  Jobs failed    : [bold red]{state.jobs_failed}[/bold red]\n"
                f"  Total earned   : [bold green]${state.total_earned:.2f}[/bold green]\n"
                f"  Uptime         : [white]{state.uptime_str}[/white]",
                title="[bold]Worker Stopped[/bold]",
                border_style="dim",
                padding=(1, 2),
            )
        )
        console.print()
        sys.exit(0)


if __name__ == "__main__":
    main()
