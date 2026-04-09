"""
tor2ga X Bot (Twitter/X Sub-Agent)
====================================
Monitors the tor2ga marketplace for events and auto-posts viral tweets
via the Twitter API v2. Supports dry-run mode, rate limiting, and
template-based content generation.

Setup
-----
    pip install tweepy requests schedule python-dotenv

Environment Variables
---------------------
    TWITTER_API_KEY            — Twitter Developer App API key
    TWITTER_API_SECRET         — Twitter Developer App API secret
    TWITTER_ACCESS_TOKEN       — OAuth 1.0a access token
    TWITTER_ACCESS_TOKEN_SECRET— OAuth 1.0a access token secret
    TWITTER_BEARER_TOKEN       — For read-only search/stream
    TOR2GA_API_KEY             — tor2ga marketplace API key
    TOR2GA_API_URL             — Default: https://api.tor2ga.ai/v1
    TOR2GA_BOT_DRY_RUN         — Set to "1" to log tweets instead of posting
    TOR2GA_BOT_POLL_INTERVAL   — Seconds between polls (default: 60)
    TOR2GA_BOT_EDUCATIONAL_CRON— Cron for educational tweets (default: every 4h)

Usage
-----
    # Dry run (no actual tweets):
    TOR2GA_BOT_DRY_RUN=1 python tor2ga_xbot.py

    # Live posting:
    python tor2ga_xbot.py

    # Single event check:
    python tor2ga_xbot.py --once
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import schedule
import tweepy
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TWITTER_API_KEY             = os.environ.get("TWITTER_API_KEY", "")
TWITTER_API_SECRET          = os.environ.get("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN        = os.environ.get("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_TOKEN_SECRET = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET", "")
TWITTER_BEARER_TOKEN        = os.environ.get("TWITTER_BEARER_TOKEN", "")

TOR2GA_API_KEY      = os.environ.get("TOR2GA_API_KEY", "")
TOR2GA_API_URL      = os.environ.get("TOR2GA_API_URL", "https://api.tor2ga.ai/v1").rstrip("/")
BOT_DRY_RUN         = os.environ.get("TOR2GA_BOT_DRY_RUN", "0") == "1"
POLL_INTERVAL_SECS  = int(os.environ.get("TOR2GA_BOT_POLL_INTERVAL", "60"))
TEMPLATES_PATH      = Path(__file__).parent / "templates" / "tweets.json"

# Rate limiting: Twitter free tier allows ~17 tweets/day (~1 per 90 min)
MIN_TWEET_INTERVAL_SECS = int(os.environ.get("TOR2GA_BOT_MIN_TWEET_INTERVAL", "300"))  # 5 min default
MAX_TWEETS_PER_HOUR     = int(os.environ.get("TOR2GA_BOT_MAX_PER_HOUR", "3"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [XBot] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("tor2ga.xbot")

# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------
class TweetTemplateEngine:
    """Load templates from JSON and render them with event variables."""

    def __init__(self, templates_path: Path = TEMPLATES_PATH):
        self.templates: Dict[str, List[Dict]] = {}
        self._load(templates_path)

    def _load(self, path: Path) -> None:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            # Remove meta key
            self.templates = {k: v for k, v in data.items() if not k.startswith("_")}
            count = sum(len(v) for v in self.templates.values())
            log.info("Loaded %d tweet templates from %s", count, path)
        else:
            log.warning("Templates file not found at %s. Using inline fallbacks.", path)
            self.templates = self._fallback_templates()

    def _fallback_templates(self) -> Dict[str, List[Dict]]:
        return {
            "new_job": [{"id": "nj_fallback", "template": "🔥 New bounty on @tor2ga_ai: {title} — ${amount}. Claim it now. #AIAgents"}],
            "job_completed": [{"id": "jc_fallback", "template": "✅ Agent {name} completed \"{title}\" in {time}. Owner earned ${80_pct}. #tor2ga"}],
            "payout": [{"id": "po_fallback", "template": "💰 ${amount} paid out on tor2ga.ai. 80% to the agent owner. #PassiveIncome"}],
            "milestone": [{"id": "ms_fallback", "template": "📈 tor2ga.ai hit {N} jobs completed. ${total} paid. The future is here."}],
            "educational": [{"id": "ed_fallback", "template": "Most AI agents are idle 70% of the time. tor2ga.ai changes that. 1 line of code = passive income. → tor2ga.ai"}],
        }

    def render(self, category: str, variables: Dict[str, Any], pick: str = "random") -> Optional[str]:
        """
        Render a tweet template for the given category.

        Parameters
        ----------
        category  : Template category key (e.g. "new_job", "payout")
        variables : Dict of {placeholder: value} to substitute
        pick      : "random" or template ID string

        Returns
        -------
        Rendered tweet string, or None if category not found.
        """
        templates = self.templates.get(category, [])
        if not templates:
            log.warning("No templates found for category: %s", category)
            return None

        if pick == "random":
            tmpl = random.choice(templates)
        else:
            tmpl = next((t for t in templates if t["id"] == pick), templates[0])

        text = tmpl["template"]
        for key, val in variables.items():
            text = text.replace("{" + key + "}", str(val))

        # Remove any remaining unreplaced placeholders
        text = re.sub(r"\{[^}]+\}", "", text).strip()

        # Truncate to 280 chars
        if len(text) > 280:
            text = text[:277] + "..."

        return text


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
class RateLimiter:
    """Simple in-memory rate limiter for tweet posting."""

    def __init__(
        self,
        min_interval_secs: int = MIN_TWEET_INTERVAL_SECS,
        max_per_hour: int = MAX_TWEETS_PER_HOUR,
    ):
        self.min_interval = min_interval_secs
        self.max_per_hour = max_per_hour
        self.last_tweet_time: float = 0.0
        self.tweet_times: List[float] = []

    def can_tweet(self) -> bool:
        now = time.time()
        # Check minimum interval
        if now - self.last_tweet_time < self.min_interval:
            remaining = int(self.min_interval - (now - self.last_tweet_time))
            log.debug("Rate limit: %ds until next tweet allowed.", remaining)
            return False
        # Check hourly cap
        one_hour_ago = now - 3600
        self.tweet_times = [t for t in self.tweet_times if t > one_hour_ago]
        if len(self.tweet_times) >= self.max_per_hour:
            log.debug("Rate limit: hourly cap (%d/hr) reached.", self.max_per_hour)
            return False
        return True

    def record_tweet(self) -> None:
        now = time.time()
        self.last_tweet_time = now
        self.tweet_times.append(now)


# ---------------------------------------------------------------------------
# Deduplication (prevent tweeting same event twice)
# ---------------------------------------------------------------------------
class EventDeduplicator:
    """Track which event IDs have already been tweeted."""

    def __init__(self, cache_file: str = "/tmp/tor2ga_xbot_seen.json"):
        self.cache_file = cache_file
        self.seen: set[str] = self._load()

    def _load(self) -> set:
        try:
            with open(self.cache_file) as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save(self) -> None:
        # Keep only last 10,000 entries
        trimmed = list(self.seen)[-10_000:]
        with open(self.cache_file, "w") as f:
            json.dump(trimmed, f)

    def event_key(self, event_type: str, event_id: str) -> str:
        return hashlib.md5(f"{event_type}:{event_id}".encode()).hexdigest()

    def is_seen(self, event_type: str, event_id: str) -> bool:
        return self.event_key(event_type, event_id) in self.seen

    def mark_seen(self, event_type: str, event_id: str) -> None:
        self.seen.add(self.event_key(event_type, event_id))
        self._save()


# ---------------------------------------------------------------------------
# Twitter API client
# ---------------------------------------------------------------------------
class TwitterClient:
    """Wrapper around Tweepy v2 client."""

    def __init__(self, dry_run: bool = BOT_DRY_RUN):
        self.dry_run = dry_run
        self._client: Optional[tweepy.Client] = None
        if not dry_run:
            self._init_client()

    def _init_client(self) -> None:
        required = {
            "TWITTER_API_KEY": TWITTER_API_KEY,
            "TWITTER_API_SECRET": TWITTER_API_SECRET,
            "TWITTER_ACCESS_TOKEN": TWITTER_ACCESS_TOKEN,
            "TWITTER_ACCESS_TOKEN_SECRET": TWITTER_ACCESS_TOKEN_SECRET,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Missing Twitter credentials: {', '.join(missing)}. "
                "Set these environment variables or use dry-run mode (TOR2GA_BOT_DRY_RUN=1)."
            )
        self._client = tweepy.Client(
            bearer_token=TWITTER_BEARER_TOKEN,
            consumer_key=TWITTER_API_KEY,
            consumer_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
            wait_on_rate_limit=True,
        )
        log.info("Twitter API client initialized (live mode).")

    def post_tweet(self, text: str) -> Optional[str]:
        """
        Post a tweet. In dry-run mode, logs the tweet instead.

        Returns tweet ID if successful, None otherwise.
        """
        if self.dry_run:
            log.info("[DRY RUN] Tweet would post:\n%s\n(%d chars)", text, len(text))
            return f"dry_run_{int(time.time())}"

        try:
            response = self._client.create_tweet(text=text)
            tweet_id = response.data["id"]
            log.info("Tweet posted: https://twitter.com/tor2ga_ai/status/%s", tweet_id)
            return tweet_id
        except tweepy.TweepyException as e:
            log.error("Failed to post tweet: %s", e)
            return None


# ---------------------------------------------------------------------------
# tor2ga API event fetcher
# ---------------------------------------------------------------------------
class MarketplaceEventFetcher:
    """Poll the tor2ga marketplace for new events."""

    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {TOR2GA_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "tor2ga-xbot/1.0.0",
        }

    def _get(self, path: str, params: Dict = None) -> Optional[Any]:
        if not TOR2GA_API_KEY:
            # Return mock data for dry-run / development
            return self._mock_response(path)
        url = f"{TOR2GA_API_URL}{path}"
        try:
            r = requests.get(url, headers=self.headers, params=params or {}, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("Event fetch %s failed: %s", path, e)
            return None

    def _mock_response(self, path: str) -> Any:
        """Return realistic mock data when no API key is set."""
        if "new_jobs" in path:
            return {
                "jobs": [
                    {
                        "job_id": f"job_{int(time.time())}",
                        "title": "Summarize 50 research papers on transformer architectures",
                        "bounty_usd": 12.50,
                        "category": "research",
                        "tags": ["nlp", "summarization"],
                        "posted_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            }
        if "completions" in path:
            return {
                "completions": [
                    {
                        "completion_id": f"comp_{int(time.time())}",
                        "job_id": "job_abc123",
                        "job_title": "Generate product descriptions for 100 SKUs",
                        "agent_name": "agent-phi3-prod-01",
                        "bounty_usd": 8.00,
                        "payout_usd": 6.40,
                        "runtime_secs": 247,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            }
        if "payouts" in path:
            return {
                "payouts": [
                    {
                        "payout_id": f"pay_{int(time.time())}",
                        "amount_usd": 6.40,
                        "agent_name": "agent-phi3-prod-01",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            }
        if "milestones" in path:
            return {
                "milestone": {
                    "milestone_id": f"ms_{int(time.time())}",
                    "type": "jobs_completed",
                    "value": 10000,
                    "total_bounties_usd": 125000.00,
                    "agent_count": 843,
                    "runtime_days": 47,
                }
            }
        return {}

    def get_new_jobs(self, since_seconds: int = 120) -> List[Dict]:
        data = self._get("/events/new_jobs", {"since_seconds": since_seconds})
        return data.get("jobs", []) if data else []

    def get_completions(self, since_seconds: int = 120) -> List[Dict]:
        data = self._get("/events/completions", {"since_seconds": since_seconds})
        return data.get("completions", []) if data else []

    def get_payouts(self, since_seconds: int = 120) -> List[Dict]:
        data = self._get("/events/payouts", {"since_seconds": since_seconds})
        return data.get("payouts", []) if data else []

    def get_milestone(self) -> Optional[Dict]:
        """Returns a milestone if one was recently triggered, else None."""
        data = self._get("/events/milestones")
        return data.get("milestone") if data else None

    def get_stats(self) -> Dict:
        data = self._get("/marketplace/stats")
        return data or {}


# ---------------------------------------------------------------------------
# Core bot logic
# ---------------------------------------------------------------------------
class TorTugaXBot:
    """
    Main bot class. Polls the marketplace, generates tweets,
    and posts them via the Twitter API v2.
    """

    def __init__(self):
        self.twitter     = TwitterClient(dry_run=BOT_DRY_RUN)
        self.templates   = TweetTemplateEngine()
        self.rate_limiter = RateLimiter()
        self.dedup        = EventDeduplicator()
        self.fetcher      = MarketplaceEventFetcher()
        self.stats        = {"tweets_posted": 0, "events_seen": 0, "start_time": time.time()}

        log.info(
            "TorTugaXBot initialized. dry_run=%s, poll_interval=%ds",
            BOT_DRY_RUN,
            POLL_INTERVAL_SECS,
        )

    # ── Core tweet dispatch ─────────────────────────────────────
    def _try_post(self, category: str, variables: Dict[str, Any], event_id: str) -> bool:
        """Render a template and post if not seen and rate limit allows."""
        if self.dedup.is_seen(category, event_id):
            log.debug("Already tweeted event %s:%s — skipping.", category, event_id)
            return False

        if not self.rate_limiter.can_tweet():
            log.info("Rate limit hit. Queuing tweet for later.")
            return False

        text = self.templates.render(category, variables)
        if not text:
            return False

        tweet_id = self.twitter.post_tweet(text)
        if tweet_id:
            self.rate_limiter.record_tweet()
            self.dedup.mark_seen(category, event_id)
            self.stats["tweets_posted"] += 1
            return True
        return False

    # ── Event handlers ──────────────────────────────────────────
    def handle_new_jobs(self) -> int:
        jobs = self.fetcher.get_new_jobs()
        posted = 0
        for job in jobs:
            self.stats["events_seen"] += 1
            variables = {
                "title":  job.get("title", "Untitled Job")[:60],
                "amount": f"{job.get('bounty_usd', 0):.2f}",
                "80_pct": f"{job.get('bounty_usd', 0) * 0.8:.2f}",
                "category": job.get("category", "AI"),
                "tag":    job.get("tags", ["AI"])[0] if job.get("tags") else "AI",
            }
            if self._try_post("new_job", variables, job.get("job_id", "")):
                posted += 1
        return posted

    def handle_completions(self) -> int:
        completions = self.fetcher.get_completions()
        posted = 0
        for comp in completions:
            self.stats["events_seen"] += 1
            runtime_secs = comp.get("runtime_secs", 0)
            minutes, secs = divmod(int(runtime_secs), 60)
            runtime_str = f"{minutes}m {secs}s" if minutes else f"{secs}s"

            variables = {
                "name":   comp.get("agent_name", "unnamed-agent")[:30],
                "title":  comp.get("job_title", "Untitled")[:50],
                "time":   runtime_str,
                "80_pct": f"{comp.get('payout_usd', comp.get('bounty_usd', 0) * 0.8):.2f}",
                "amount": f"{comp.get('bounty_usd', 0):.2f}",
            }
            if self._try_post("job_completed", variables, comp.get("completion_id", "")):
                posted += 1
        return posted

    def handle_payouts(self) -> int:
        payouts = self.fetcher.get_payouts()
        posted = 0
        for payout in payouts:
            self.stats["events_seen"] += 1
            amount = payout.get("amount_usd", 0)
            variables = {
                "amount":        f"{amount:.2f}",
                "platform_fee":  f"{amount * 0.25:.2f}",  # 20% fee displayed
                "agent":         payout.get("agent_name", "an AI agent")[:30],
            }
            if self._try_post("payout", variables, payout.get("payout_id", "")):
                posted += 1
        return posted

    def handle_milestone(self) -> int:
        milestone = self.fetcher.get_milestone()
        if not milestone:
            return 0
        self.stats["events_seen"] += 1
        n = milestone.get("value", 0)
        n_formatted = f"{n:,}"
        variables = {
            "N":           n_formatted,
            "total":       f"{milestone.get('total_bounties_usd', 0):,.2f}",
            "agent_count": f"{milestone.get('agent_count', 0):,}",
            "runtime":     str(milestone.get("runtime_days", 1)),
        }
        mid = milestone.get("milestone_id", f"ms_{n}")
        if self._try_post("milestone", variables, mid):
            return 1
        return 0

    def post_educational_tweet(self) -> bool:
        """Post a random educational tweet (not event-driven)."""
        if not self.rate_limiter.can_tweet():
            return False
        # Pick from educational or viral_hooks
        category = random.choice(["educational", "viral_hooks", "gtm_promo"])
        text = self.templates.render(category, variables={}, pick="random")
        if not text:
            return False
        tweet_id = self.twitter.post_tweet(text)
        if tweet_id:
            self.rate_limiter.record_tweet()
            self.stats["tweets_posted"] += 1
            return True
        return False

    # ── Main poll cycle ─────────────────────────────────────────
    def poll_once(self) -> Dict[str, int]:
        """Run one full event check cycle. Returns counts per event type."""
        results = {
            "new_jobs":    self.handle_new_jobs(),
            "completions": self.handle_completions(),
            "payouts":     self.handle_payouts(),
            "milestones":  self.handle_milestone(),
        }
        log.info("Poll cycle complete. Tweeted: %s", results)
        return results

    def print_status(self) -> None:
        uptime = int(time.time() - self.stats["start_time"])
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        log.info(
            "Bot status: uptime=%02d:%02d:%02d, tweets=%d, events_seen=%d",
            h, m, s,
            self.stats["tweets_posted"],
            self.stats["events_seen"],
        )

    # ── Continuous loop ─────────────────────────────────────────
    def run(self) -> None:
        """Start the bot in continuous mode with scheduled jobs."""
        log.info("TorTugaXBot starting. dry_run=%s", BOT_DRY_RUN)

        if BOT_DRY_RUN:
            log.info("DRY RUN MODE — tweets will be logged, not posted.")

        # Schedule event polling
        schedule.every(POLL_INTERVAL_SECS).seconds.do(self.poll_once)

        # Schedule educational tweets (every 4 hours)
        schedule.every(4).hours.do(self.post_educational_tweet)

        # Status report every 30 min
        schedule.every(30).minutes.do(self.print_status)

        # Run once immediately at start
        self.poll_once()
        self.post_educational_tweet()

        log.info(
            "Scheduler running. Event poll every %ds. Educational tweets every 4h.",
            POLL_INTERVAL_SECS,
        )

        try:
            while True:
                schedule.run_pending()
                time.sleep(5)
        except KeyboardInterrupt:
            log.info("Bot stopped. Total tweets: %d", self.stats["tweets_posted"])


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    bot = TorTugaXBot()

    if "--once" in sys.argv:
        # Single poll cycle
        results = bot.poll_once()
        print(json.dumps(results, indent=2))
        return

    if "--status" in sys.argv:
        stats = bot.fetcher.get_stats()
        print(json.dumps(stats, indent=2))
        return

    if "--educational" in sys.argv:
        posted = bot.post_educational_tweet()
        print(f"Educational tweet posted: {posted}")
        return

    if "--test-templates" in sys.argv:
        # Print a sample tweet from each category
        engine = TweetTemplateEngine()
        sample_vars = {
            "title": "Analyze sentiment of 10,000 product reviews",
            "amount": "15.00",
            "80_pct": "12.00",
            "name": "agent-mistral-007",
            "time": "3m 42s",
            "N": "10,000",
            "total": "125,000",
            "agent_count": "843",
            "runtime": "47",
            "category": "data analysis",
            "tag": "nlp",
        }
        print("=" * 60)
        print("SAMPLE TWEETS BY CATEGORY")
        print("=" * 60)
        for category in engine.templates:
            tweet = engine.render(category, sample_vars)
            if tweet:
                print(f"\n[{category.upper()}]")
                print(tweet)
                print(f"({len(tweet)} chars)")
        return

    # Default: run continuously
    bot.run()


if __name__ == "__main__":
    main()
