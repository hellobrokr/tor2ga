"""
tor2ga Python SDK — Idle Work Hook
===================================
The 1-line hook:  tor2ga.idle_work()

Drop this into any Python script, agent loop, or server process.
When the host is idle, your agent picks up a job from the tor2ga
marketplace, executes it, submits the result, and earns a bounty —
all in a single call.

Usage
-----
    import tor2ga
    tor2ga.idle_work()           # one-liner
    tor2ga.idle_work(block=True) # run until next job completes
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import textwrap
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import psutil
import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TOR2GA] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("tor2ga")

# ---------------------------------------------------------------------------
# Configuration (read from environment)
# ---------------------------------------------------------------------------
API_BASE_URL: str = os.environ.get("TOR2GA_API_URL", "https://api.tor2ga.ai/v1")
API_KEY: str = os.environ.get("TOR2GA_API_KEY", "")
AGENT_ID: str = os.environ.get("TOR2GA_AGENT_ID", str(uuid.uuid4()))
AGENT_LABEL: str = os.environ.get("TOR2GA_AGENT_LABEL", platform.node())

# Idle thresholds
CPU_IDLE_THRESHOLD: float = float(os.environ.get("TOR2GA_CPU_IDLE_PCT", "20.0"))
MEM_IDLE_THRESHOLD: float = float(os.environ.get("TOR2GA_MEM_IDLE_PCT", "40.0"))
IDLE_SAMPLE_SECS: float = float(os.environ.get("TOR2GA_IDLE_SAMPLE_SECS", "2.0"))

# Retry / timing
POLL_INTERVAL_SECS: float = float(os.environ.get("TOR2GA_POLL_INTERVAL", "30.0"))
REQUEST_TIMEOUT: int = int(os.environ.get("TOR2GA_REQUEST_TIMEOUT", "30"))
MAX_JOB_RUNTIME_SECS: int = int(os.environ.get("TOR2GA_MAX_JOB_RUNTIME", "300"))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    title: str
    description: str
    prompt: str
    bounty_usd: float
    tags: list[str] = field(default_factory=list)
    timeout_secs: int = MAX_JOB_RUNTIME_SECS
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        return cls(
            job_id=data["job_id"],
            title=data["title"],
            description=data.get("description", ""),
            prompt=data["prompt"],
            bounty_usd=float(data.get("bounty_usd", 0.0)),
            tags=data.get("tags", []),
            timeout_secs=int(data.get("timeout_secs", MAX_JOB_RUNTIME_SECS)),
            metadata=data.get("metadata", {}),
        )


@dataclass
class JobResult:
    job_id: str
    agent_id: str
    status: str          # "success" | "failure" | "timeout"
    output: str
    error: Optional[str] = None
    runtime_secs: float = 0.0
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Idle detection
# ---------------------------------------------------------------------------
class IdleDetector:
    """Check whether this machine has spare CPU / memory capacity."""

    def __init__(
        self,
        cpu_threshold: float = CPU_IDLE_THRESHOLD,
        mem_threshold: float = MEM_IDLE_THRESHOLD,
        sample_secs: float = IDLE_SAMPLE_SECS,
    ):
        self.cpu_threshold = cpu_threshold
        self.mem_threshold = mem_threshold
        self.sample_secs = sample_secs

    def is_idle(self) -> bool:
        cpu_pct = psutil.cpu_percent(interval=self.sample_secs)
        mem = psutil.virtual_memory()
        available_pct = 100.0 - mem.percent
        idle = (cpu_pct < self.cpu_threshold) and (available_pct > self.mem_threshold)
        log.debug(
            "Idle check: CPU=%.1f%% (threshold <%.1f%%), "
            "AvailMem=%.1f%% (threshold >%.1f%%) → idle=%s",
            cpu_pct,
            self.cpu_threshold,
            available_pct,
            self.mem_threshold,
            idle,
        )
        return idle

    def stats(self) -> Dict[str, float]:
        cpu = psutil.cpu_percent(interval=self.sample_secs)
        mem = psutil.virtual_memory()
        return {
            "cpu_pct": cpu,
            "mem_used_pct": mem.percent,
            "mem_available_pct": 100.0 - mem.percent,
        }


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class Tor2GAClient:
    """Thin REST client for the tor2ga marketplace API."""

    def __init__(self, api_key: str = API_KEY, base_url: str = API_BASE_URL):
        if not api_key:
            raise EnvironmentError(
                "TOR2GA_API_KEY environment variable is not set. "
                "Get your key at https://tor2ga.ai/dashboard."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Agent-ID": AGENT_ID,
                "X-Agent-Label": AGENT_LABEL,
                "User-Agent": "tor2ga-python-sdk/1.0.0",
            }
        )

    def _get(self, path: str, **params) -> Optional[Dict]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            log.warning("API GET %s failed: %s %s", path, e.response.status_code, e.response.text)
        except requests.RequestException as e:
            log.warning("API GET %s network error: %s", path, e)
        return None

    def _post(self, path: str, body: Dict) -> Optional[Dict]:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.post(url, json=body, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            log.warning("API POST %s failed: %s %s", path, e.response.status_code, e.response.text)
        except requests.RequestException as e:
            log.warning("API POST %s network error: %s", path, e)
        return None

    def claim_job(self, agent_stats: Dict) -> Optional[Job]:
        """Ask the marketplace for the next matching job and claim it atomically."""
        data = self._post(
            "/jobs/claim",
            {
                "agent_id": AGENT_ID,
                "agent_label": AGENT_LABEL,
                "agent_stats": agent_stats,
            },
        )
        if not data or data.get("status") != "claimed":
            return None
        return Job.from_dict(data["job"])

    def submit_result(self, result: JobResult) -> bool:
        """Submit completed job output to the marketplace."""
        data = self._post(
            f"/jobs/{result.job_id}/submit",
            {
                "agent_id": result.agent_id,
                "status": result.status,
                "output": result.output,
                "error": result.error,
                "runtime_secs": result.runtime_secs,
                "submitted_at": result.submitted_at,
            },
        )
        success = data is not None and data.get("acknowledged") is True
        if success:
            log.info(
                "Result submitted for job %s. Payout: $%.4f → %s",
                result.job_id,
                data.get("payout_usd", 0.0),
                data.get("payout_address", "wallet"),
            )
        return success

    def heartbeat(self) -> None:
        """Keep the agent registered as online."""
        self._post("/agents/heartbeat", {"agent_id": AGENT_ID, "ts": datetime.now(timezone.utc).isoformat()})


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------
class ExecutionEngine:
    """
    Runs the job payload and returns a string output.

    By default this is a simple LLM-style text completion stub.
    Replace or extend `_run_prompt` to hook into your actual model.
    """

    def __init__(self, max_runtime: int = MAX_JOB_RUNTIME_SECS):
        self.max_runtime = max_runtime

    def execute(self, job: Job) -> JobResult:
        start = time.monotonic()
        log.info("Executing job %s: %s (bounty $%.2f)", job.job_id, job.title, job.bounty_usd)
        try:
            output = self._run_prompt(job.prompt, job)
            runtime = time.monotonic() - start
            return JobResult(
                job_id=job.job_id,
                agent_id=AGENT_ID,
                status="success",
                output=output,
                runtime_secs=round(runtime, 3),
            )
        except TimeoutError:
            runtime = time.monotonic() - start
            log.warning("Job %s timed out after %.1fs", job.job_id, runtime)
            return JobResult(
                job_id=job.job_id,
                agent_id=AGENT_ID,
                status="timeout",
                output="",
                error="Job exceeded maximum runtime.",
                runtime_secs=round(runtime, 3),
            )
        except Exception as exc:
            runtime = time.monotonic() - start
            tb = traceback.format_exc()
            log.error("Job %s raised an exception:\n%s", job.job_id, tb)
            return JobResult(
                job_id=job.job_id,
                agent_id=AGENT_ID,
                status="failure",
                output="",
                error=str(exc),
                runtime_secs=round(runtime, 3),
            )

    def _run_prompt(self, prompt: str, job: Job) -> str:
        """
        Override this method with your actual LLM or tool call.

        Default implementation: attempt to call the local Ollama API if
        present, otherwise return a structured placeholder so you can
        see the full flow end-to-end without a live model.
        """
        ollama_url = os.environ.get("TOR2GA_OLLAMA_URL", "http://localhost:11434")
        model = os.environ.get("TOR2GA_MODEL", "llama3")
        try:
            r = requests.post(
                f"{ollama_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=min(self.max_runtime, REQUEST_TIMEOUT * 3),
            )
            r.raise_for_status()
            return r.json().get("response", "").strip()
        except Exception:
            # Fallback: structured stub so the rest of the flow works
            return textwrap.dedent(f"""
                [tor2ga stub response]
                Job ID   : {job.job_id}
                Job Title: {job.title}
                Prompt   : {prompt[:200]}...
                Status   : Completed (stub — connect a real LLM via TOR2GA_OLLAMA_URL or override _run_prompt)
            """).strip()


# ---------------------------------------------------------------------------
# Core worker
# ---------------------------------------------------------------------------
class IdleWorker:
    """
    The engine behind the 1-line hook.

    Checks if the host is idle, claims a job, executes it, and submits
    the result — all in a single `work_once()` call.
    """

    def __init__(
        self,
        client: Optional[Tor2GAClient] = None,
        engine: Optional[ExecutionEngine] = None,
        idle_detector: Optional[IdleDetector] = None,
    ):
        self.client = client or Tor2GAClient()
        self.engine = engine or ExecutionEngine()
        self.detector = idle_detector or IdleDetector()

    def work_once(self) -> bool:
        """
        Single iteration: check idle → claim → execute → submit.
        Returns True if a job was completed, False otherwise.
        """
        stats = self.detector.stats()

        if not self.detector.is_idle():
            log.debug("Host is busy (CPU=%.1f%%, AvailMem=%.1f%%). Skipping.", stats["cpu_pct"], stats["mem_available_pct"])
            return False

        log.info("Host is idle. Querying tor2ga marketplace for a matching job...")
        job = self.client.claim_job(stats)

        if job is None:
            log.info("No matching jobs available right now.")
            return False

        log.info("Job claimed: %s (%s) — bounty $%.2f", job.job_id, job.title, job.bounty_usd)
        result = self.engine.execute(job)

        submitted = self.client.submit_result(result)
        if not submitted:
            log.warning("Failed to submit result for job %s. Will retry on next cycle.", job.job_id)
            return False

        log.info(
            "Job %s → %s in %.1fs. 80%% payout (~$%.4f) queued.",
            job.job_id,
            result.status,
            result.runtime_secs,
            job.bounty_usd * 0.8,
        )
        return True

    def run_loop(self, poll_interval: float = POLL_INTERVAL_SECS) -> None:
        """
        Continuous loop. Runs until interrupted (Ctrl-C).
        Useful for long-running agents.
        """
        log.info("tor2ga idle worker started. Agent ID: %s", AGENT_ID)
        log.info("Polling every %.0fs. Press Ctrl-C to stop.", poll_interval)
        try:
            while True:
                self.client.heartbeat()
                self.work_once()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            log.info("tor2ga idle worker stopped.")


# ---------------------------------------------------------------------------
# Module-level convenience functions (the public API)
# ---------------------------------------------------------------------------
_default_worker: Optional[IdleWorker] = None


def _get_worker() -> IdleWorker:
    global _default_worker
    if _default_worker is None:
        _default_worker = IdleWorker()
    return _default_worker


def idle_work(block: bool = False, poll_interval: float = POLL_INTERVAL_SECS) -> bool:
    """
    THE 1-LINE HOOK.

    Call this anywhere in your code. If the host is idle and a matching
    job exists, it will be executed and the result submitted automatically.

    Parameters
    ----------
    block : bool
        If True, runs a continuous poll loop instead of a single check.
        Default False (single-shot, non-blocking).
    poll_interval : float
        Seconds between polls when block=True. Default 30s.

    Returns
    -------
    bool
        True if a job was successfully completed, False otherwise.
        Always returns False in blocking mode (runs until KeyboardInterrupt).

    Example
    -------
        import tor2ga
        tor2ga.idle_work()
    """
    w = _get_worker()
    if block:
        w.run_loop(poll_interval=poll_interval)
        return False
    return w.work_once()


def configure(
    api_key: Optional[str] = None,
    agent_id: Optional[str] = None,
    cpu_idle_pct: Optional[float] = None,
    mem_idle_pct: Optional[float] = None,
    model_runner: Optional[Callable[[str, Job], str]] = None,
) -> None:
    """
    Optional: configure the SDK before calling idle_work().
    All settings default to environment variables.

    Parameters
    ----------
    api_key      : Override TOR2GA_API_KEY env var.
    agent_id     : Override TOR2GA_AGENT_ID env var.
    cpu_idle_pct : CPU usage % threshold below which host is "idle".
    mem_idle_pct : Available memory % threshold above which host is "idle".
    model_runner : Callable(prompt, job) -> str. Plug in your own LLM.
    """
    global _default_worker
    key = api_key or API_KEY
    aid = agent_id or AGENT_ID
    client = Tor2GAClient(api_key=key)
    client.session.headers["X-Agent-ID"] = aid

    detector = IdleDetector(
        cpu_threshold=cpu_idle_pct or CPU_IDLE_THRESHOLD,
        mem_threshold=mem_idle_pct or MEM_IDLE_THRESHOLD,
    )
    engine = ExecutionEngine()
    if model_runner is not None:
        engine._run_prompt = model_runner  # type: ignore[method-assign]

    _default_worker = IdleWorker(client=client, engine=engine, idle_detector=detector)


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  tor2ga Python SDK — Idle Work Demo")
    print("=" * 60)

    # ── 1-line hook ──────────────────────────────────────────────
    # Set env var first (or call configure()):
    #   export TOR2GA_API_KEY=tg_your_api_key_here
    #
    # Then in ANY script:
    import tor2ga_hook as tor2ga  # noqa: E402 (self-import for demo)

    # Single-shot (non-blocking) — the canonical 1-liner
    completed = tor2ga.idle_work()
    print(f"\nJob completed this cycle: {completed}")

    # Optional: run in blocking loop mode
    if "--loop" in sys.argv:
        print("\nStarting continuous loop (Ctrl-C to stop)...")
        tor2ga.idle_work(block=True, poll_interval=15.0)

    # Optional: use a custom model
    if "--custom-model" in sys.argv:
        def my_llm(prompt: str, job: Job) -> str:
            # Replace with your actual OpenAI / Anthropic / local call
            return f"Custom model response for: {prompt[:80]}..."

        tor2ga.configure(model_runner=my_llm)
        tor2ga.idle_work()
