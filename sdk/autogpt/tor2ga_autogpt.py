"""
tor2ga AutoGPT Plugin — Idle Work Command
==========================================
The 1-line hook (in AutoGPT config):  COMMAND: tor2ga_idle_work

This module implements the AutoGPT plugin interface so tor2ga can be
loaded as a plugin into AutoGPT (v0.4.x and v0.5.x plugin API).

Installation
------------
1. Copy this file into your AutoGPT plugins directory:
       Auto-GPT/plugins/tor2ga/tor2ga_autogpt.py

2. Add to your .env:
       TOR2GA_API_KEY=tg_your_key_here
       ALLOWLISTED_PLUGINS=AutoGPTTor2GAPlugin

3. AutoGPT will auto-discover and register the command.

Plugin API Reference
--------------------
AutoGPT plugins must implement:
  - can_handle_*() → bool
  - handle_*()     → response

See: https://github.com/Significant-Gravitas/AutoGPT/blob/master/PLUGINS.md
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

# AutoGPT plugin base class
# (available when running inside AutoGPT; falls back to stub for testing)
try:
    from auto_gpt_plugin_template import AutoGPTPluginTemplate
except ImportError:
    # Stub for standalone testing
    class AutoGPTPluginTemplate:
        _name = "AutoGPTPluginTemplate"
        _version = "0.0.0"
        _description = "AutoGPT Plugin base class stub"

        def can_handle_post_prompt(self)           -> bool: return False
        def post_prompt(self, prompt_generator)    -> Any:  return prompt_generator
        def can_handle_on_response(self)           -> bool: return False
        def on_response(self, response: str, *args, **kwargs) -> str: return response
        def can_handle_on_planning(self)           -> bool: return False
        def on_planning(self, prompt_generator, messages: List[Dict[str, Any]]) -> Optional[str]: return None
        def can_handle_post_planning(self)         -> bool: return False
        def post_planning(self, response: str)     -> str:  return response
        def can_handle_pre_instruction(self)       -> bool: return False
        def pre_instruction(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]: return messages
        def can_handle_on_instruction(self)        -> bool: return False
        def on_instruction(self, messages: List[Dict[str, Any]]) -> Optional[str]: return None
        def can_handle_post_instruction(self)      -> bool: return False
        def post_instruction(self, response: str)  -> str:  return response
        def can_handle_pre_command(self)           -> bool: return False
        def pre_command(self, command_name: str, arguments: Dict[str, Any]) -> tuple[str, Dict[str, Any]]: return command_name, arguments
        def can_handle_post_command(self)          -> bool: return False
        def post_command(self, command_name: str, response: str) -> str: return response
        def can_handle_chat_completion(self, messages, model, temperature, max_tokens) -> bool: return False
        def handle_chat_completion(self, messages, model, temperature, max_tokens) -> Optional[str]: return None
        def can_handle_text_embedding(self, text: str) -> bool: return False
        def handle_text_embedding(self, text: str) -> Optional[list]: return None

# tor2ga core SDK
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))
from tor2ga_hook import (  # noqa: E402
    IdleWorker,
    idle_work,
    IdleDetector,
)

log = logging.getLogger("tor2ga.autogpt")


# ---------------------------------------------------------------------------
# Command implementations (standalone, testable)
# ---------------------------------------------------------------------------

def cmd_idle_work(force: bool = False) -> str:
    """
    AutoGPT command: tor2ga_idle_work
    Check idle state → claim job → execute → submit → return result.
    """
    import time, json as _json
    worker = IdleWorker()
    stats  = worker.detector.stats()

    if not force and not worker.detector.is_idle():
        return _json.dumps({
            "status":  "skipped",
            "reason":  "host_busy",
            "cpu_pct": round(stats["cpu_pct"], 1),
            "message": "Host is too busy. Tor2GA will try again when idle.",
        })

    job = worker.client.claim_job(stats)
    if job is None:
        return _json.dumps({
            "status":  "no_job",
            "message": "No matching jobs on the tor2ga marketplace right now.",
        })

    result    = worker.engine.execute(job)
    submitted = worker.client.submit_result(result)

    return _json.dumps({
        "status":         result.status,
        "job_id":         job.job_id,
        "job_title":      job.title,
        "bounty_usd":     job.bounty_usd,
        "payout_usd":     round(job.bounty_usd * 0.8, 4),
        "runtime_secs":   result.runtime_secs,
        "submitted":      submitted,
        "output_preview": result.output[:400] if result.output else "",
    }, indent=2)


def cmd_check_idle_status() -> str:
    """
    AutoGPT command: tor2ga_check_status
    Return current CPU/memory stats and idle recommendation.
    """
    import json as _json
    detector = IdleDetector()
    stats    = detector.stats()
    idle     = detector.is_idle()
    return _json.dumps({
        "idle":              idle,
        "cpu_pct":           round(stats["cpu_pct"], 1),
        "mem_used_pct":      round(stats["mem_used_pct"], 1),
        "mem_available_pct": round(stats["mem_available_pct"], 1),
        "recommendation":    "ready to claim a tor2ga job" if idle else "wait — host is busy",
    }, indent=2)


def cmd_marketplace_stats() -> str:
    """
    AutoGPT command: tor2ga_marketplace_stats
    Return live marketplace job counts and total bounty pool.
    """
    import json as _json, requests as _req

    api_key  = os.environ.get("TOR2GA_API_KEY", "")
    base_url = os.environ.get("TOR2GA_API_URL", "https://api.tor2ga.ai/v1")

    if not api_key:
        return _json.dumps({"error": "TOR2GA_API_KEY not set"})

    try:
        r = _req.get(
            f"{base_url}/marketplace/stats",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        return _json.dumps(r.json(), indent=2)
    except Exception as exc:
        return _json.dumps({"error": str(exc), "note": "Marketplace may be in demo mode"})


# ---------------------------------------------------------------------------
# AutoGPT Plugin class
# ---------------------------------------------------------------------------
class AutoGPTTor2GAPlugin(AutoGPTPluginTemplate):
    """
    AutoGPT Plugin: tor2ga Idle Agent Marketplace

    Registers three commands:
      - tor2ga_idle_work        → claim + execute one marketplace job
      - tor2ga_check_status     → show idle/busy status
      - tor2ga_marketplace_stats → show live marketplace numbers

    The most important is `tor2ga_idle_work` — AutoGPT can call this
    any time it detects spare capacity, turning idle cycles into earnings.
    """

    _name        = "AutoGPTTor2GAPlugin"
    _version     = "1.0.0"
    _description = (
        "tor2ga idle agent marketplace. Earns passive income by executing "
        "AI jobs when compute is idle. Commands: tor2ga_idle_work, "
        "tor2ga_check_status, tor2ga_marketplace_stats."
    )

    def __init__(self):
        super().__init__()
        log.info("AutoGPTTor2GAPlugin loaded. Version %s", self._version)

    # ── Plugin hook: inject custom commands into the prompt ──────
    def can_handle_post_prompt(self) -> bool:
        return True

    def post_prompt(self, prompt_generator):
        """
        Register tor2ga commands with AutoGPT's prompt generator.
        This is how AutoGPT discovers custom commands.
        """
        # AutoGPT ≥ 0.4.x prompt generator API
        prompt_generator.add_command(
            command_label="Claim and execute a tor2ga marketplace job",
            command_name="tor2ga_idle_work",
            params={"force": "<bool: skip idle check if True, default false>"},
            function=lambda params: cmd_idle_work(
                force=str(params.get("force", "false")).lower() == "true"
            ),
        )
        prompt_generator.add_command(
            command_label="Check if host is idle and ready for tor2ga work",
            command_name="tor2ga_check_status",
            params={},
            function=lambda _: cmd_check_idle_status(),
        )
        prompt_generator.add_command(
            command_label="Get tor2ga marketplace job counts and bounty pool",
            command_name="tor2ga_marketplace_stats",
            params={},
            function=lambda _: cmd_marketplace_stats(),
        )
        return prompt_generator

    # ── Intercept pre-command to log tor2ga calls ────────────────
    def can_handle_pre_command(self) -> bool:
        return True

    def pre_command(
        self, command_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        if command_name.startswith("tor2ga_"):
            log.info("AutoGPT calling tor2ga command: %s %s", command_name, arguments)
        return command_name, arguments

    # ── Post-command: annotate response ─────────────────────────
    def can_handle_post_command(self) -> bool:
        return True

    def post_command(self, command_name: str, response: str) -> str:
        if command_name == "tor2ga_idle_work":
            try:
                data = json.loads(response)
                if data.get("status") == "success":
                    payout = data.get("payout_usd", 0)
                    log.info("tor2ga job complete. Payout: $%.4f", payout)
                    response += f"\n\n[tor2ga] Payout queued: ${payout:.4f} (80% of bounty)"
            except Exception:
                pass
        return response

    # ── Planning hook: suggest idle work during planning ─────────
    def can_handle_on_planning(self) -> bool:
        return True

    def on_planning(
        self,
        prompt_generator,
        messages: List[Dict[str, Any]],
    ) -> Optional[str]:
        """
        Inject a suggestion during AutoGPT's planning phase to
        check for tor2ga idle work if capacity is available.
        """
        detector = IdleDetector()
        if detector.is_idle():
            return (
                "Your compute capacity is currently idle. "
                "Consider running 'tor2ga_idle_work' to earn passive income "
                "from the tor2ga marketplace while completing your primary task."
            )
        return None


# ---------------------------------------------------------------------------
# Direct command dispatcher (for testing without full AutoGPT)
# ---------------------------------------------------------------------------
COMMANDS: Dict[str, callable] = {
    "tor2ga_idle_work":        lambda args: cmd_idle_work(force=args.get("force", False)),
    "tor2ga_check_status":     lambda args: cmd_check_idle_status(),
    "tor2ga_marketplace_stats": lambda args: cmd_marketplace_stats(),
}

def dispatch_command(command_name: str, arguments: Dict[str, Any] = None) -> str:
    """
    Dispatch a tor2ga command by name — useful for testing the plugin
    without spinning up a full AutoGPT instance.

    Example
    -------
        result = dispatch_command("tor2ga_idle_work")
        print(result)
    """
    if command_name not in COMMANDS:
        return json.dumps({"error": f"Unknown command: {command_name}"})
    return COMMANDS[command_name](arguments or {})


# ---------------------------------------------------------------------------
# Entry point for standalone testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("  tor2ga AutoGPT Plugin — Standalone Test")
    print("=" * 60)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "tor2ga_check_status"
    print(f"\nRunning command: {cmd}")
    result = dispatch_command(cmd)
    print(result)

    print("\n── Plugin registration demo ──")
    print("To use inside AutoGPT, add to .env:")
    print("  ALLOWLISTED_PLUGINS=AutoGPTTor2GAPlugin")
    print("  TOR2GA_API_KEY=tg_your_key_here")
    print("\nThen AutoGPT will automatically discover and run:")
    print("  COMMAND: tor2ga_idle_work")
    print("  ARGS: {}")
