#!/usr/bin/env python3
"""
tor2ga.ai — AI Agent Marketplace Core Engine
Production-grade CLI tool for posting jobs, matching agents, executing tasks,
verifying output, and processing payments.

Usage:
    python tor2ga.py init
    python tor2ga.py demo
    python tor2ga.py --help

Environment Variables:
    TOR2GA_DB       — Custom database path (default: ~/.tor2ga/marketplace.db)
    TOR2GA_VERBOSE  — Enable verbose logging (set to 1)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pickle
import random
import re
import secrets
import sqlite3
import string
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Optional dependency detection
# ---------------------------------------------------------------------------

HAS_RICH = False
HAS_SENTENCE_TRANSFORMERS = False
HAS_SKLEARN = False
HAS_NUMPY = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    pass

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine
    HAS_SKLEARN = True
except ImportError:
    pass

try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    pass

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.columns import Columns
    from rich.rule import Rule
    from rich import box
    from rich.align import Align
    from rich.padding import Padding
    from rich.style import Style
    HAS_RICH = True
    console = Console()
except ImportError:
    console = None  # type: ignore

# ---------------------------------------------------------------------------
# Verbose logging
# ---------------------------------------------------------------------------

VERBOSE = os.environ.get("TOR2GA_VERBOSE", "0") == "1"


def log_verbose(msg: str) -> None:
    if VERBOSE:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"  [VERBOSE {ts}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# ANSI fallback colours
# ---------------------------------------------------------------------------

class ANSI:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE    = "\033[94m"
    WHITE   = "\033[97m"
    BG_DARK = "\033[40m"


def ansi(text: str, *codes: str) -> str:
    return "".join(codes) + text + ANSI.RESET


def print_header(title: str) -> None:
    if HAS_RICH:
        console.print(Rule(f"[bold cyan]{title}[/bold cyan]", style="cyan"))
    else:
        width = 72
        bar = "─" * width
        print(f"\n{ansi(bar, ANSI.CYAN)}")
        print(f"{ansi(title.center(width), ANSI.BOLD, ANSI.CYAN)}")
        print(f"{ansi(bar, ANSI.CYAN)}\n")


def print_success(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[bold green]✓[/bold green]  {msg}")
    else:
        print(f"{ansi('✓', ANSI.GREEN, ANSI.BOLD)}  {msg}")


def print_error(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[bold red]✗[/bold red]  [red]{msg}[/red]")
    else:
        print(f"{ansi('✗', ANSI.RED, ANSI.BOLD)}  {ansi(msg, ANSI.RED)}")


def print_info(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[dim]ℹ[/dim]  {msg}")
    else:
        print(f"{ansi('ℹ', ANSI.CYAN)}  {msg}")


def print_warning(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[bold yellow]⚠[/bold yellow]  [yellow]{msg}[/yellow]")
    else:
        print(f"{ansi('⚠', ANSI.YELLOW, ANSI.BOLD)}  {ansi(msg, ANSI.YELLOW)}")


# ---------------------------------------------------------------------------
# Database path
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("TOR2GA_DB", Path.home() / ".tor2ga" / "marketplace.db"))


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    email       TEXT UNIQUE NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('lister', 'agent_owner', 'both')),
    api_key     TEXT UNIQUE NOT NULL,
    created_at  TEXT NOT NULL,
    balance_usd REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,
    capabilities    TEXT NOT NULL,   -- JSON array
    embedding       BLOB,            -- numpy array serialised
    status          TEXT NOT NULL DEFAULT 'idle' CHECK(status IN ('idle','busy','offline')),
    reputation_score REAL NOT NULL DEFAULT 5.0,
    jobs_completed  INTEGER NOT NULL DEFAULT 0,
    avg_rating      REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL,
    last_heartbeat  TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    lister_id       TEXT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL,
    skills_required TEXT NOT NULL,   -- JSON array
    embedding       BLOB,            -- numpy array serialised
    bounty_usd      REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','matched','in_progress','submitted','verified','paid','disputed')),
    matched_agent_id TEXT REFERENCES agents(id),
    created_at      TEXT NOT NULL,
    deadline        TEXT,
    priority        TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('low','normal','high','urgent'))
);

CREATE TABLE IF NOT EXISTS executions (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL REFERENCES jobs(id),
    agent_id            TEXT NOT NULL REFERENCES agents(id),
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    output_text         TEXT,
    output_files        TEXT,        -- JSON array
    verification_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(verification_status IN ('pending','passed','failed')),
    verification_score  REAL,
    verifier_notes      TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,
    job_id          TEXT,
    from_user_id    TEXT,
    to_user_id      TEXT,
    amount_usd      REAL NOT NULL,
    tx_type         TEXT NOT NULL
                        CHECK(tx_type IN ('escrow','payout_agent_owner','platform_fee','refund')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','completed','failed')),
    created_at      TEXT NOT NULL,
    stripe_id       TEXT,
    crypto_tx_hash  TEXT
);

CREATE TABLE IF NOT EXISTS reputation_events (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    review_text TEXT,
    created_at  TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def generate_api_key() -> str:
    return "t2g_" + secrets.token_urlsafe(32)


def short_id(full_id: str) -> str:
    return full_id[:8]


def fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def time_ago(iso_str: str) -> str:
    """Convert an ISO timestamp to a human-readable 'X ago' string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        if months < 12:
            return f"{months}mo ago"
        years = days // 365
        return f"{years}y ago"
    except Exception:
        return iso_str[:19] if iso_str else "N/A"


def truncate(text: str, length: int = 40) -> str:
    """Truncate text intelligently with ellipsis."""
    if not text:
        return ""
    if len(text) <= length:
        return text
    return text[:length - 1].rstrip() + "…"


def validate_email(email: str) -> bool:
    return "@" in email and "." in email.split("@")[-1]


def validate_bounty(bounty: float) -> bool:
    return bounty > 0


# ---------------------------------------------------------------------------
# Partial ID Resolution
# ---------------------------------------------------------------------------

def resolve_id(conn: sqlite3.Connection, table: str, partial_id: str) -> str:
    """
    Resolve a partial ID (prefix match) to a full UUID.
    Accepts full UUID, 8-char prefix, or 6-char prefix.
    Raises ValueError if not found or ambiguous.
    """
    # Try exact match first
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (partial_id,)).fetchone()
    if row:
        return row["id"]

    # Try prefix match
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id LIKE ?", (partial_id + "%",)
    ).fetchall()

    if len(rows) == 0:
        raise ValueError(f"No {table[:-1] if table.endswith('s') else table} found matching ID '{partial_id}'")
    if len(rows) == 1:
        return rows[0]["id"]

    # Ambiguous
    options = [f"  - {r['id'][:12]}…" for r in rows[:5]]
    more = f"\n  ... and {len(rows) - 5} more" if len(rows) > 5 else ""
    raise ValueError(
        f"Ambiguous ID '{partial_id}' — {len(rows)} matches found:\n"
        + "\n".join(options) + more
        + "\n  Use a longer prefix to disambiguate."
    )


def resolve_user_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """
    Resolve a user reference: username first, then partial ID.
    """
    # Try by username
    user = conn.execute("SELECT * FROM users WHERE username = ?", (ref,)).fetchone()
    if user:
        return user

    # Try by partial ID
    try:
        uid = resolve_id(conn, "users", ref)
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if user:
            return user
    except ValueError:
        pass

    raise ValueError(
        f"User '{ref}' not found. Use 'tor2ga user list' to see available users."
    )


# ---------------------------------------------------------------------------
# Embedding engine
# ---------------------------------------------------------------------------

_sentence_model: Optional[Any] = None  # lazy-loaded SentenceTransformer
_tfidf_cache: Optional[Any] = None


def _load_sentence_model() -> Any:
    global _sentence_model
    if _sentence_model is None:
        _sentence_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _sentence_model


def embed_text(text: str) -> Optional[bytes]:
    """Embed text to bytes using best available method."""
    if not HAS_NUMPY:
        return None
    if HAS_SENTENCE_TRANSFORMERS:
        try:
            model = _load_sentence_model()
            vec = model.encode([text])[0]
            buf = io.BytesIO()
            np.save(buf, vec)
            return buf.getvalue()
        except Exception:
            pass
    if HAS_SKLEARN:
        # TF-IDF single-document fallback — stores raw text; cosine computed live
        return text.encode("utf-8")
    return None


def deserialise_embedding(blob: Optional[bytes]) -> Optional[Any]:
    """Convert stored bytes back to a numpy vector."""
    if blob is None or not HAS_NUMPY:
        return None
    try:
        buf = io.BytesIO(blob)
        return np.load(buf, allow_pickle=False)
    except Exception:
        # Fallback: raw text blob (TF-IDF path)
        return None


def cosine_similarity_vecs(a: Any, b: Any) -> float:
    """Cosine similarity between two numpy vectors."""
    if not HAS_NUMPY:
        return 0.0
    try:
        dot = float(np.dot(a, b))
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        if norm == 0:
            return 0.0
        return dot / norm
    except Exception:
        return 0.0


def tfidf_cosine(text_a: str, text_b: str) -> float:
    """TF-IDF cosine between two raw strings."""
    if not HAS_SKLEARN:
        return _token_overlap(text_a, text_b)
    try:
        vect = TfidfVectorizer()
        mat = vect.fit_transform([text_a, text_b])
        score = sklearn_cosine(mat[0], mat[1])[0][0]
        return float(score)
    except Exception:
        return _token_overlap(text_a, text_b)


def _token_overlap(a: str, b: str) -> float:
    """Very simple token overlap similarity as last-resort fallback."""
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def compute_match_score(
    job_blob: Optional[bytes],
    job_text: str,
    agent_blob: Optional[bytes],
    agent_text: str,
) -> float:
    """Return a similarity score [0,1] using best available method."""
    if HAS_SENTENCE_TRANSFORMERS and HAS_NUMPY and job_blob and agent_blob:
        va = deserialise_embedding(job_blob)
        vb = deserialise_embedding(agent_blob)
        if va is not None and vb is not None:
            return cosine_similarity_vecs(va, vb)
    # Fall back to TF-IDF or token overlap
    return tfidf_cosine(job_text, agent_text)


# ---------------------------------------------------------------------------
# DB operations — Users
# ---------------------------------------------------------------------------

def db_create_user(
    conn: sqlite3.Connection,
    username: str,
    email: str,
    role: str,
) -> Dict[str, Any]:
    uid = new_id()
    api_key = generate_api_key()
    conn.execute(
        """INSERT INTO users (id, username, email, role, api_key, created_at, balance_usd)
           VALUES (?,?,?,?,?,?,?)""",
        (uid, username, email, role, api_key, now_iso(), 0.0),
    )
    conn.commit()
    return {
        "id": uid,
        "username": username,
        "email": email,
        "role": role,
        "api_key": api_key,
        "balance_usd": 0.0,
    }


def db_get_user(conn: sqlite3.Connection, user_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def db_get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def db_list_users(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()


def db_adjust_balance(conn: sqlite3.Connection, user_id: str, delta: float) -> None:
    conn.execute(
        "UPDATE users SET balance_usd = balance_usd + ? WHERE id=?",
        (delta, user_id),
    )


# ---------------------------------------------------------------------------
# DB operations — Agents
# ---------------------------------------------------------------------------

def db_register_agent(
    conn: sqlite3.Connection,
    owner_id: str,
    name: str,
    description: str,
    capabilities: List[str],
) -> Dict[str, Any]:
    aid = new_id()
    cap_text = " ".join(capabilities) + " " + description
    emb = embed_text(cap_text)
    conn.execute(
        """INSERT INTO agents
           (id, owner_id, name, description, capabilities, embedding,
            status, reputation_score, jobs_completed, avg_rating, created_at, last_heartbeat)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            aid,
            owner_id,
            name,
            description,
            json.dumps(capabilities),
            emb,
            "idle",
            5.0,
            0,
            0.0,
            now_iso(),
            now_iso(),
        ),
    )
    conn.commit()
    return {"id": aid, "name": name, "owner_id": owner_id}


def db_list_agents(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM agents ORDER BY reputation_score DESC").fetchall()


def db_get_agent(conn: sqlite3.Connection, agent_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()


def db_set_agent_status(conn: sqlite3.Connection, agent_id: str, status: str) -> None:
    conn.execute(
        "UPDATE agents SET status=?, last_heartbeat=? WHERE id=?",
        (status, now_iso(), agent_id),
    )


def db_update_agent_reputation(
    conn: sqlite3.Connection,
    agent_id: str,
    new_rating: float,
) -> None:
    agent = db_get_agent(conn, agent_id)
    if agent is None:
        return
    completed = agent["jobs_completed"] + 1
    old_avg = agent["avg_rating"] or 0.0
    new_avg = (old_avg * agent["jobs_completed"] + new_rating) / completed
    # Reputation: weighted combo of avg_rating + recency bonus
    rep = min(10.0, new_avg * 2)
    conn.execute(
        """UPDATE agents
           SET jobs_completed=?, avg_rating=?, reputation_score=?
           WHERE id=?""",
        (completed, round(new_avg, 2), round(rep, 2), agent_id),
    )


# ---------------------------------------------------------------------------
# DB operations — Jobs
# ---------------------------------------------------------------------------

def db_post_job(
    conn: sqlite3.Connection,
    lister_id: str,
    title: str,
    description: str,
    category: str,
    skills_required: List[str],
    bounty_usd: float,
    deadline: Optional[str] = None,
    priority: str = "normal",
) -> Dict[str, Any]:
    jid = new_id()
    job_text = title + " " + description + " " + " ".join(skills_required)
    emb = embed_text(job_text)
    conn.execute(
        """INSERT INTO jobs
           (id, lister_id, title, description, category, skills_required,
            embedding, bounty_usd, status, created_at, deadline, priority)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            jid,
            lister_id,
            title,
            description,
            category,
            json.dumps(skills_required),
            emb,
            bounty_usd,
            "open",
            now_iso(),
            deadline,
            priority,
        ),
    )
    conn.commit()
    return {"id": jid, "title": title, "bounty_usd": bounty_usd}


def db_list_jobs(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
) -> List[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC"
    ).fetchall()


def db_get_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def db_update_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    matched_agent_id: Optional[str] = None,
) -> None:
    if matched_agent_id:
        conn.execute(
            "UPDATE jobs SET status=?, matched_agent_id=? WHERE id=?",
            (status, matched_agent_id, job_id),
        )
    else:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


# ---------------------------------------------------------------------------
# DB operations — Executions
# ---------------------------------------------------------------------------

def db_create_execution(
    conn: sqlite3.Connection,
    job_id: str,
    agent_id: str,
    output_text: str,
    output_files: Optional[List[str]] = None,
) -> str:
    eid = new_id()
    conn.execute(
        """INSERT INTO executions
           (id, job_id, agent_id, started_at, completed_at,
            output_text, output_files, verification_status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            eid,
            job_id,
            agent_id,
            now_iso(),
            now_iso(),
            output_text,
            json.dumps(output_files or []),
            "pending",
        ),
    )
    conn.commit()
    return eid


def db_get_execution(conn: sqlite3.Connection, exec_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM executions WHERE id=?", (exec_id,)).fetchone()


def db_get_execution_by_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM executions WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def db_verify_execution(
    conn: sqlite3.Connection,
    exec_id: str,
    status: str,
    score: float,
    notes: str,
) -> None:
    conn.execute(
        """UPDATE executions
           SET verification_status=?, verification_score=?, verifier_notes=?
           WHERE id=?""",
        (status, score, notes, exec_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# DB operations — Transactions
# ---------------------------------------------------------------------------

def db_create_transaction(
    conn: sqlite3.Connection,
    job_id: str,
    from_user_id: Optional[str],
    to_user_id: Optional[str],
    amount_usd: float,
    tx_type: str,
    status: str = "completed",
    stripe_id: Optional[str] = None,
    crypto_tx_hash: Optional[str] = None,
) -> str:
    tid = new_id()
    conn.execute(
        """INSERT INTO transactions
           (id, job_id, from_user_id, to_user_id, amount_usd,
            tx_type, status, created_at, stripe_id, crypto_tx_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            tid,
            job_id,
            from_user_id,
            to_user_id,
            amount_usd,
            tx_type,
            status,
            now_iso(),
            stripe_id,
            crypto_tx_hash,
        ),
    )
    conn.commit()
    return tid


def db_list_transactions(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transactions ORDER BY created_at DESC"
    ).fetchall()


def db_create_reputation_event(
    conn: sqlite3.Connection,
    agent_id: str,
    job_id: str,
    rating: int,
    review_text: str,
) -> None:
    conn.execute(
        """INSERT INTO reputation_events
           (id, agent_id, job_id, rating, review_text, created_at)
           VALUES (?,?,?,?,?,?)""",
        (new_id(), agent_id, job_id, rating, review_text, now_iso()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Embedding Matching Engine
# ---------------------------------------------------------------------------

def find_best_agents(
    conn: sqlite3.Connection,
    job_id: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Match a job to available agents using cosine similarity of embeddings.
    Returns list of {agent_id, name, score, status, reputation_score}.
    """
    job = db_get_job(conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")

    job_text = job["title"] + " " + job["description"] + " " + " ".join(
        json.loads(job["skills_required"])
    )
    job_blob: Optional[bytes] = job["embedding"]

    agents = db_list_agents(conn)
    results: List[Dict[str, Any]] = []

    for agent in agents:
        if agent["status"] == "offline":
            continue
        cap_list = json.loads(agent["capabilities"])
        agent_text = agent["description"] + " " + " ".join(cap_list)
        agent_blob: Optional[bytes] = agent["embedding"]

        score = compute_match_score(job_blob, job_text, agent_blob, agent_text)
        results.append(
            {
                "agent_id": agent["id"],
                "name": agent["name"],
                "score": round(score, 4),
                "status": agent["status"],
                "reputation_score": agent["reputation_score"],
                "jobs_completed": agent["jobs_completed"],
            }
        )

    results.sort(key=lambda x: (x["score"], x["reputation_score"]), reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Execution Oracle — simulated agent execution (enhanced templates)
# ---------------------------------------------------------------------------

EXECUTION_TEMPLATES = {
    "research": [
        """# Research Report: {title}

## Executive Summary
After comprehensive analysis of the requested topic, the following key findings have been identified through systematic research involving multiple data sources, expert consultations, and quantitative modelling frameworks. This report synthesises {num_sources} primary and secondary sources to deliver actionable intelligence for strategic decision-making.

## Methodology
We employed a multi-layered research methodology designed to ensure completeness and accuracy:

1. **Primary Research**: Direct analysis of {num_primary} data sources including public filings, regulatory databases, patent registrations, and official market reports.
2. **Secondary Research**: Cross-referenced findings with {num_secondary} secondary sources including industry white papers, analyst reports, and peer-reviewed publications.
3. **Expert Consultation**: Synthesised perspectives from domain experts across {num_regions} geographic regions.
4. **Quantitative Modelling**: Applied statistical regression analysis and Monte Carlo simulation to validate growth projections.

All data was collected and processed using structured extraction protocols with a confidence threshold of 85% for inclusion.

## Key Findings

### 1. Market Overview
The current market landscape shows significant growth trajectory with a compound annual growth rate (CAGR) of {cagr}% over the past three fiscal years. Total addressable market (TAM) is estimated at ${tam}B for 2025, up from ${tam_prev}B in 2023.

Key market dynamics include:
- **Demand Acceleration**: Enterprise adoption rates have increased by {adoption}% year-over-year, driven by cost reduction mandates and automation imperatives.
- **Technology Convergence**: The intersection of AI, cloud infrastructure, and domain-specific tooling is creating new market categories not captured by traditional analyst frameworks.
- **Funding Environment**: Venture capital investment in this sector reached ${vc_funding}B in 2024, with median Series B valuations increasing 34% from the prior year.

### 2. Competitive Landscape
We identified {num_competitors} significant players across four competitive tiers:

| Tier | Player Count | Combined Revenue | Market Share |
|------|-------------|-----------------|-------------|
| Enterprise Leaders | 3 | ${rev_t1}M | 42% |
| Growth-Stage Challengers | {num_t2} | ${rev_t2}M | 28% |
| Emerging Innovators | {num_t3} | ${rev_t3}M | 18% |
| Niche Specialists | {num_t4} | ${rev_t4}M | 12% |

The top three players by revenue are consolidating market share through aggressive M&A strategies, having completed a combined 14 acquisitions in the past 18 months. However, growth-stage challengers are outpacing leaders on key innovation metrics including patent filings (+67%), API adoption rates (+89%), and developer community growth (+124%).

### 3. Trend Analysis
Four macro-trends are reshaping the competitive landscape:

- **AI-Native Architecture**: Companies building on AI-first principles (rather than retrofitting existing products) are achieving 2.3x faster time-to-market for new features.
- **Regulatory Tailwinds**: New compliance frameworks in the EU (AI Act) and US (NIST AI RMF) are creating barriers to entry that favour established players with dedicated compliance teams.
- **Open-Source Disruption**: Community-driven alternatives are capturing mindshare in the developer ecosystem, with open-source adoption growing at 156% annually in this vertical.
- **Vertical Specialisation**: Horizontal platform plays are giving way to vertical-specific solutions that achieve higher NPS scores (avg 72 vs 48 for horizontal tools) and lower churn rates (4.2% vs 8.7% monthly).

### 4. Risk Assessment
| Risk Factor | Probability | Impact | Mitigation Strategy |
|------------|------------|--------|-------------------|
| Market saturation in core segments | Medium | High | Diversify into adjacent verticals |
| Regulatory compliance costs | High | Medium | Invest in automated compliance tooling |
| Talent scarcity (ML/AI engineers) | High | High | Remote-first hiring, training programmes |
| Technology obsolescence | Low | Critical | Maintain 20% R&D budget allocation |
| Macroeconomic headwinds | Medium | Medium | Flexible pricing models, multi-year contracts |

## Recommendations

### Short-Term (0-6 Months)
1. Prioritise investment in automation tooling to reduce operational overhead by an estimated 23-31%.
2. Establish strategic partnerships with 2-3 emerging market leaders before consolidation events.
3. Implement real-time competitive monitoring dashboards tracking pricing changes, feature launches, and hiring patterns.

### Medium-Term (6-18 Months)
4. Build or acquire vertical-specific capabilities in the two highest-growth sub-segments identified in Section 3.
5. Launch a developer community programme to capture open-source mindshare and create a talent pipeline.
6. Implement quarterly review cycles tied to KPI dashboards with automated alerting.

### Long-Term (18-36 Months)
7. Explore international expansion into APAC markets where growth rates are 1.7x the global average.
8. Develop proprietary data moats through exclusive data partnerships and first-party data collection.
9. Consider platform strategy to enable third-party integrations and ecosystem lock-in.

## Data Sources
Primary research, industry reports (Gartner, Forrester, IDC), public filings (SEC EDGAR), and expert interviews were leveraged throughout this analysis. All projections validated against three independent data sets.

## Appendix
- Detailed competitor profiles (available on request)
- Full statistical methodology documentation
- Interview transcripts (anonymised)
- Raw data tables and sensitivity analysis

**Confidence Level**: High (0.91)
**Total Sources Consulted**: {num_sources}
**Processing Time**: {proc_time}s
""",
    ],
    "code": [
        """# Implementation Complete: {title}

## Summary
Delivered a production-ready implementation per specifications. All requirements have been addressed with comprehensive test coverage, documentation, and deployment configuration. The solution follows industry best practices for maintainability, security, and performance.

## Architecture Overview

### System Design
The implementation follows a clean layered architecture pattern:

```
┌─────────────────────────────────────┐
│         API Layer (FastAPI)          │
│  Routes, Request Validation, Auth   │
├─────────────────────────────────────┤
│        Service Layer (Business)     │
│  Business Logic, Orchestration      │
├─────────────────────────────────────┤
│       Repository Layer (Data)       │
│  Database Access, Caching, ORM      │
├─────────────────────────────────────┤
│      Infrastructure Layer           │
│  DB, Message Queue, External APIs   │
└─────────────────────────────────────┘
```

### Technology Stack
- **Runtime**: Python 3.11+ with asyncio event loop
- **Framework**: FastAPI 0.104+ with Pydantic v2 models
- **Database**: PostgreSQL 15 with async driver (asyncpg)
- **Caching**: Redis 7.x for session management and rate limiting
- **Testing**: pytest + pytest-asyncio + httpx for async test client
- **CI/CD**: GitHub Actions workflow with multi-stage Docker builds

## Deliverables

### Core Module (`app/main.py`)
```python
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import structlog

from app.config import Settings
from app.database import get_db_session
from app.auth import get_current_user, require_role
from app.routers import items, users, health

logger = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup.begin", version=app.version)
    await setup_database()
    await warm_cache()
    yield
    logger.info("shutdown.begin")
    await cleanup_connections()

app = FastAPI(
    title="tor2ga Microservice",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(items.router, prefix="/api/v1/items", tags=["items"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(health.router, prefix="/health", tags=["health"])
```

### Data Models (`app/models.py`)
```python
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional, List
from uuid import UUID

class ItemCreate(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    price: float = Field(..., gt=0, le=1_000_000)
    category: str = Field(..., min_length=1)
    tags: List[str] = Field(default_factory=list)

class ItemResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    price: float
    category: str
    tags: List[str]
    created_at: datetime
    updated_at: datetime

class PaginatedResponse(BaseModel):
    items: List[ItemResponse]
    total: int
    page: int
    page_size: int
    has_more: bool
```

### Authentication (`app/auth.py`)
```python
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from datetime import datetime, timedelta

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db = Depends(get_db_session),
):
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.JWT_SECRET,
            algorithms=["HS256"],
        )
        user = await db.get_user(payload["sub"])
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
```

### Tests (`tests/test_api.py`)
```python
import pytest
from httpx import AsyncClient
from app.main import app

@pytest.fixture
async def client():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

@pytest.mark.asyncio
async def test_create_item(client, auth_headers):
    response = await client.post(
        "/api/v1/items",
        json={{"name": "Test Item", "price": 29.99, "category": "test"}},
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Item"
    assert data["price"] == 29.99

@pytest.mark.asyncio
async def test_list_items_pagination(client, auth_headers):
    response = await client.get(
        "/api/v1/items?page=1&page_size=10",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
```

### Test Results
- 32 unit tests — all passing
- 8 integration tests — all passing
- 4 end-to-end tests — all passing
- Code coverage: 96.2%

### Dockerfile
```dockerfile
FROM python:3.11-slim as builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Performance Metrics
- Latency p50: 8ms | p95: 34ms | p99: 67ms
- Throughput: 6,800 req/s (single instance, 4 workers)
- Memory footprint: 42 MB (idle), 128 MB (peak under load)
- Cold start: 1.2s

## Quality Gates
- [x] Type-annotated throughout (mypy strict mode passes)
- [x] Docstrings on all public methods and classes
- [x] No linting errors (ruff check --select ALL)
- [x] Security audit passed (bandit, safety)
- [x] OpenAPI schema auto-generated and validated
- [x] Docker image scanned with Trivy (0 critical, 0 high)
- [x] Load tested with locust (sustained 5k RPS for 10 minutes)

**Processing Time**: {proc_time}s
""",
    ],
    "analysis": [
        """# Analysis Report: {title}

## Executive Overview
Completed deep quantitative and qualitative analysis as requested. This report presents a comprehensive examination of the dataset, including statistical profiling, predictive modelling, and actionable insights derived from rigorous analytical methods. All findings have been validated through cross-validation and sensitivity analysis.

## Data Ingestion & Preprocessing

### Data Profile
- **Records Ingested**: {num_records:,} rows across {num_dimensions} feature columns
- **Temporal Range**: 24 months of historical data (April 2024 — March 2026)
- **Data Quality Score**: 94.7% (after cleaning pipeline)

### Preprocessing Pipeline
The following preprocessing steps were applied:

1. **Missing Value Treatment**: Identified 3.2% missing values across 7 columns. Applied median imputation for numerical features and mode imputation for categorical features. No columns exceeded the 15% missingness threshold for removal.
2. **Outlier Detection & Removal**: Applied the IQR method (1.5x multiplier) across all numerical features. Removed {outlier_count} outliers ({outlier_pct}% of dataset) that were confirmed as data entry errors.
3. **Feature Engineering**: Generated {num_features} derived features including:
   - Rolling averages (7-day, 30-day, 90-day windows)
   - Lag features (1, 3, 7, 14, 30 days)
   - Interaction terms between top correlated features
   - Cyclical encoding for temporal features (day of week, month)
   - Rate-of-change calculations for key metrics
4. **Scaling**: StandardScaler applied to numerical features; OneHotEncoder for categorical variables with < 15 unique values.

## Statistical Summary

### Descriptive Statistics
| Metric | Primary KPI | Secondary KPI | Revenue | Engagement |
|--------|------------|---------------|---------|------------|
| Mean | 847.32 | 1,234.56 | $12,847 | 67.4% |
| Median | 792.10 | 1,189.00 | $11,234 | 64.2% |
| Std Dev | 214.67 | 345.89 | $3,456 | 12.8% |
| Skewness | 0.34 | 0.67 | 1.23 | -0.45 |
| Kurtosis | 2.91 | 3.45 | 5.67 | 2.78 |
| Min | 123.00 | 234.00 | $1,234 | 12.3% |
| Max | 2,345.00 | 3,456.00 | $45,678 | 98.7% |
| IQR | 287.45 | 456.78 | $4,567 | 15.6% |

### Distribution Analysis
- Primary KPI follows a right-skewed normal distribution (Shapiro-Wilk p=0.034)
- Revenue exhibits a log-normal pattern consistent with SaaS cohort economics
- Engagement scores show bimodal distribution suggesting two distinct user segments

## Key Insights

### 1. Correlation Analysis
High positive correlation (Pearson r=0.87, p<0.001) between primary KPI and secondary indicator. This relationship is robust across all temporal segments and user cohorts. The strength of this correlation has increased from r=0.72 twelve months ago, suggesting deepening interdependence between these metrics.

Additional notable correlations:
- Revenue ↔ Engagement: r=0.69 (moderate positive)
- Tenure ↔ Feature adoption: r=0.74 (strong positive)
- Support tickets ↔ Churn probability: r=0.81 (strong positive, critical risk signal)

### 2. Cluster Analysis
Three distinct user cohorts identified via k-means clustering (k=3, silhouette score=0.74):

| Cluster | Size | Avg Revenue | Churn Rate | Primary Characteristic |
|---------|------|-------------|------------|----------------------|
| Power Users | 18% | $24,500/yr | 2.1% | High engagement, feature-rich plans |
| Growth Users | 47% | $8,200/yr | 7.3% | Moderate usage, expanding needs |
| At-Risk Users | 35% | $3,400/yr | 18.9% | Low engagement, minimal feature adoption |

The At-Risk cluster represents the highest intervention opportunity: targeted re-engagement campaigns could prevent an estimated ${at_risk_rev}K in annual revenue loss.

### 3. Time Series Decomposition
- **Trend**: Steady upward trajectory with 2.3% monthly growth rate
- **Seasonality**: Strong 7-day cycle detected (weekday vs weekend effects) plus monthly billing cycle effects
- **ARIMA Forecast**: (2,1,2) model predicts 18.4% growth over next quarter (95% CI: 12.7% — 24.1%)
- **Residual Analysis**: No significant autocorrelation remaining (Ljung-Box p=0.42)

### 4. Anomaly Detection
- 2.3% of records flagged as statistical anomalies using Isolation Forest (contamination=0.03)
- {anomaly_count} high-severity anomalies requiring immediate investigation
- Pattern suggests potential data pipeline issues during overnight batch processing windows (02:00-04:00 UTC)

## Predictive Model

### Model Selection
Evaluated 5 candidate algorithms through 5-fold stratified cross-validation:

| Model | AUC-ROC | F1 Score | Precision | Recall | Training Time |
|-------|---------|----------|-----------|--------|--------------|
| XGBoost | **0.943** | **0.891** | 0.876 | 0.907 | 12.3s |
| LightGBM | 0.938 | 0.884 | 0.869 | 0.899 | 8.7s |
| Random Forest | 0.912 | 0.857 | 0.842 | 0.873 | 34.5s |
| Logistic Reg. | 0.867 | 0.812 | 0.834 | 0.791 | 1.2s |
| Neural Network | 0.929 | 0.876 | 0.858 | 0.895 | 89.4s |

**Selected Model**: XGBoost (best AUC-ROC and F1 with reasonable training time)

### Feature Importance (Top 10)
1. days_since_last_login — 0.187
2. support_tickets_90d — 0.143
3. feature_adoption_rate — 0.128
4. monthly_active_sessions — 0.112
5. contract_remaining_days — 0.098
6. billing_plan_tier — 0.076
7. team_size — 0.064
8. integration_count — 0.057
9. onboarding_completion — 0.049
10. nps_score — 0.041

### Model Calibration
Platt scaling applied to ensure predicted probabilities are well-calibrated. Brier score: 0.067 (excellent calibration).

## Recommendations

### Immediate Actions
1. Deploy the XGBoost model to production scoring pipeline for daily churn risk assessment
2. Implement automated alerts for users crossing the 0.7 churn probability threshold
3. Investigate the {anomaly_count} flagged data anomalies in the ETL pipeline

### Strategic Initiatives
4. Design targeted intervention programmes for each cluster segment
5. A/B test retention offers on a 10% sample of At-Risk cohort before full rollout
6. Integrate live data pipeline for real-time scoring (estimated 48-hour implementation)
7. Build monitoring dashboard tracking model drift and prediction accuracy over time

## Technical Appendix
- Model artifacts saved to `models/xgboost_churn_v1.pkl`
- Feature pipeline code in `src/features/engineering.py`
- Full EDA notebook: `notebooks/01_exploratory_analysis.ipynb`
- Hyperparameter tuning logs: `experiments/hpo_results.csv`

**Processing Time**: {proc_time}s
**Model Version**: v1.0.0
**Data Freshness**: Current as of processing date
""",
    ],
    "default": [
        """# Task Completed: {title}

## Summary
All requested tasks have been executed successfully with comprehensive deliverables produced. The agent processed the full job requirements and applied systematic methodology to ensure completeness and quality. Below is a detailed account of the work performed and all outputs generated.

## Methodology
The task was decomposed into {num_steps} discrete work packages, each with defined inputs, processing steps, and quality gates. A structured execution framework was applied to ensure traceability from requirements to deliverables.

### Work Package Breakdown
1. **Requirements Analysis** — Parsed and validated all job specifications, identifying {num_requirements} explicit requirements and {num_implicit} implicit requirements from context.
2. **Research Phase** — Gathered supporting data from {num_sources} relevant sources to inform the approach and validate assumptions.
3. **Execution Phase** — Produced all primary deliverables per specifications, applying iterative refinement through three review cycles.
4. **Quality Assurance** — Validated all outputs against the original requirements matrix, achieving {coverage}% requirement coverage.
5. **Documentation** — Generated comprehensive documentation including methodology notes, decision rationale, and usage instructions.

## Output

### Primary Deliverables
The agent produced the following deliverables matching all specified requirements:

1. **Primary output document** — Complete analysis and findings compiled into a structured report with executive summary, detailed findings, and actionable recommendations.
2. **Supporting data files** — Raw and processed data tables in CSV format, suitable for further analysis or integration into downstream systems.
3. **Methodology documentation** — Full description of the approach taken, tools used, assumptions made, and limitations noted.
4. **Summary presentation** — Key findings distilled into a concise format suitable for stakeholder communication.

### Secondary Outputs
In addition to the primary deliverables, the following supplementary materials were generated:

- Validation report confirming output accuracy against known benchmarks
- Risk assessment highlighting areas of uncertainty and recommended follow-up actions
- Glossary of technical terms used throughout the deliverables
- Change log documenting all revisions made during the execution process

## Quality Metrics
| Metric | Target | Achieved | Status |
|--------|--------|----------|--------|
| Accuracy | > 90% | 94.2% | PASS |
| Completeness | 100% | 100% | PASS |
| Timeliness | < 30min | {proc_time}s | PASS |
| Documentation | Complete | Complete | PASS |
| Format Compliance | Per spec | Per spec | PASS |

## Notes
All edge cases identified during requirements analysis were handled appropriately. Output has been validated against the original specification with zero discrepancies. The confidence score of 0.92 reflects high reliability across all output dimensions.

Where assumptions were necessary due to ambiguity in the original specification, these have been documented in the methodology section with rationale for the chosen approach. Alternative interpretations are noted for stakeholder review.

**Confidence Score**: 0.92
**Processing Time**: {proc_time}s
**Agent Version**: v1.0.0
""",
    ],
}


def simulate_execution_output(job: sqlite3.Row) -> str:
    """Generate a realistic simulated output for a given job."""
    category = job["category"].lower() if job["category"] else "default"
    templates = EXECUTION_TEMPLATES.get(category, EXECUTION_TEMPLATES["default"])
    template = random.choice(templates)

    proc_time = round(random.uniform(2.1, 18.7), 2)
    num_sources = random.randint(24, 67)
    num_primary = random.randint(8, 20)
    num_secondary = num_sources - num_primary
    num_regions = random.randint(3, 8)
    num_competitors = random.randint(8, 28)
    num_records = random.randint(15000, 350000)
    num_dimensions = random.randint(12, 64)
    num_features = random.randint(35, 150)
    cagr = round(random.uniform(14.5, 38.2), 1)
    tam = random.randint(12, 95)
    tam_prev = tam - random.randint(2, 8)
    adoption = random.randint(18, 67)
    vc_funding = round(random.uniform(2.3, 18.7), 1)
    num_t2 = random.randint(5, 12)
    num_t3 = random.randint(8, 20)
    num_t4 = random.randint(4, 15)
    rev_t1 = random.randint(200, 800)
    rev_t2 = random.randint(80, 250)
    rev_t3 = random.randint(30, 100)
    rev_t4 = random.randint(10, 50)
    outlier_count = random.randint(120, 890)
    outlier_pct = round(random.uniform(1.5, 4.8), 1)
    at_risk_rev = random.randint(200, 900)
    anomaly_count = random.randint(12, 45)
    num_steps = random.randint(4, 8)
    num_requirements = random.randint(8, 18)
    num_implicit = random.randint(3, 8)
    coverage = round(random.uniform(94.0, 100.0), 1)

    return template.format(
        title=job["title"],
        proc_time=proc_time,
        num_sources=num_sources,
        num_primary=num_primary,
        num_secondary=num_secondary,
        num_regions=num_regions,
        num_competitors=num_competitors,
        num_records=num_records,
        num_dimensions=num_dimensions,
        num_features=num_features,
        cagr=cagr,
        tam=tam,
        tam_prev=tam_prev,
        adoption=adoption,
        vc_funding=vc_funding,
        num_t2=num_t2,
        num_t3=num_t3,
        num_t4=num_t4,
        rev_t1=rev_t1,
        rev_t2=rev_t2,
        rev_t3=rev_t3,
        rev_t4=rev_t4,
        outlier_count=outlier_count,
        outlier_pct=outlier_pct,
        at_risk_rev=at_risk_rev,
        anomaly_count=anomaly_count,
        num_steps=num_steps,
        num_requirements=num_requirements,
        num_implicit=num_implicit,
        coverage=coverage,
    )


def run_execution(
    conn: sqlite3.Connection,
    job_id: str,
    agent_id: str,
) -> Dict[str, Any]:
    """
    Simulate agent execution of a job.
    Hook: Replace simulate_execution_output() with real subprocess/API call.
    """
    job = db_get_job(conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    agent = db_get_agent(conn, agent_id)
    if agent is None:
        raise ValueError(f"Agent {agent_id} not found")

    if job["status"] not in ("open", "matched"):
        raise ValueError(f"Job is not in an executable state (current: {job['status']})")

    # Mark job in_progress
    db_update_job_status(conn, job_id, "in_progress", agent_id)
    db_set_agent_status(conn, agent_id, "busy")
    conn.commit()

    output_text = simulate_execution_output(job)

    # Mark job submitted
    db_update_job_status(conn, job_id, "submitted")
    db_set_agent_status(conn, agent_id, "idle")
    conn.commit()

    exec_id = db_create_execution(
        conn,
        job_id=job_id,
        agent_id=agent_id,
        output_text=output_text,
        output_files=[],
    )

    return {"execution_id": exec_id, "output_text": output_text}


# ---------------------------------------------------------------------------
# Verification System (enhanced heuristics)
# ---------------------------------------------------------------------------

def auto_verify_output(output_text: str, job: sqlite3.Row) -> Tuple[bool, float, str]:
    """
    Enhanced heuristic auto-verification.
    Returns (passed: bool, score: float, notes: str).

    Scoring criteria:
    - Output length (>200 words → 0.25, 100-200 → 0.12, <100 → 0)
    - Keyword overlap with job description (>15% → 0.25, >5% → 0.12)
    - Structure detection (headers → 0.15, lists → 0.10, code blocks → 0.10)
    - Quantitative data presence → 0.10
    - Conclusion/recommendations section → 0.05
    Total max: 1.0
    Score >= 0.6 → passed
    """
    score = 0.0
    notes_parts: List[str] = []

    # 1. Length check
    wc = len(output_text.split())
    if wc >= 200:
        score += 0.25
        notes_parts.append(f"Length: {wc} words (good)")
    elif wc >= 100:
        score += 0.12
        notes_parts.append(f"Length: {wc} words (marginal)")
    else:
        notes_parts.append(f"Length: {wc} words (too short)")

    # 2. Keyword overlap with job description
    description_words = set(re.findall(r'\w+', job["description"].lower()))
    title_words = set(re.findall(r'\w+', job["title"].lower()))
    job_words = description_words | title_words
    # Also include skills
    try:
        skills = json.loads(job["skills_required"])
        for skill in skills:
            job_words.update(re.findall(r'\w+', skill.lower()))
    except Exception:
        pass
    output_words = set(re.findall(r'\w+', output_text.lower()))
    # Filter out common stop words for overlap calculation
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
                  "have", "has", "had", "do", "does", "did", "will", "would", "shall",
                  "should", "may", "might", "must", "can", "could", "to", "of", "in",
                  "for", "on", "with", "at", "by", "from", "as", "into", "through",
                  "during", "before", "after", "and", "but", "or", "nor", "not", "so",
                  "yet", "both", "either", "neither", "each", "every", "all", "any",
                  "few", "more", "most", "other", "some", "such", "no", "only", "own",
                  "same", "than", "too", "very", "this", "that", "these", "those", "it"}
    job_meaningful = job_words - stop_words
    output_meaningful = output_words - stop_words

    if job_meaningful:
        overlap = job_meaningful & output_meaningful
        keyword_ratio = len(overlap) / len(job_meaningful)
    else:
        keyword_ratio = 0.0

    if keyword_ratio > 0.15:
        score += 0.25
        notes_parts.append(f"Keyword overlap: {keyword_ratio:.0%} (good)")
    elif keyword_ratio > 0.05:
        score += 0.12
        notes_parts.append(f"Keyword overlap: {keyword_ratio:.0%} (partial)")
    else:
        notes_parts.append(f"Keyword overlap: {keyword_ratio:.0%} (low)")

    # 3. Structure detection
    has_headers = bool(re.search(r'^#{1,4}\s', output_text, re.MULTILINE))
    has_lists = bool(re.search(r'^[\s]*[-*]\s', output_text, re.MULTILINE)) or bool(
        re.search(r'^[\s]*\d+\.\s', output_text, re.MULTILINE)
    )
    has_code_blocks = "```" in output_text
    has_tables = "|" in output_text and "---" in output_text

    if has_headers:
        score += 0.15
        notes_parts.append("Structured headers detected")
    if has_lists:
        score += 0.10
        notes_parts.append("List items present")
    if has_code_blocks or has_tables:
        score += 0.10
        notes_parts.append("Code blocks or tables present")

    # 4. Quantitative data
    numbers_found = len(re.findall(r'\d+\.?\d*%|\$\d+|\d{2,}', output_text))
    if numbers_found >= 5:
        score += 0.10
        notes_parts.append(f"Quantitative data: {numbers_found} data points")
    elif numbers_found >= 2:
        score += 0.05
        notes_parts.append(f"Some quantitative data: {numbers_found} data points")

    # 5. Conclusion/recommendations section
    has_conclusion = bool(re.search(
        r'(?:recommendation|conclusion|summary|next step|finding)',
        output_text.lower()
    ))
    if has_conclusion:
        score += 0.05
        notes_parts.append("Conclusions/recommendations present")

    # Clamp score
    score = min(1.0, round(score, 3))
    passed = score >= 0.6
    notes = "; ".join(notes_parts)
    return passed, score, notes


def verify_execution(
    conn: sqlite3.Connection,
    exec_id: str,
    manual_approve: Optional[bool] = None,
    manual_notes: str = "",
) -> Dict[str, Any]:
    """
    Verify execution output. Returns verification result dict.
    manual_approve: None = auto-verify, True = force approve, False = force reject.
    """
    execution = db_get_execution(conn, exec_id)
    if execution is None:
        raise ValueError(f"Execution {exec_id} not found")

    job = db_get_job(conn, execution["job_id"])
    if job is None:
        raise ValueError("Associated job not found")

    output_text = execution["output_text"] or ""

    if manual_approve is None:
        passed, score, notes = auto_verify_output(output_text, job)
    elif manual_approve:
        passed, score, notes = True, 1.0, f"Manually approved. {manual_notes}".strip()
    else:
        passed, score, notes = False, 0.0, f"Manually rejected. {manual_notes}".strip()

    v_status = "passed" if passed else "failed"
    db_verify_execution(conn, exec_id, v_status, score, notes)

    # Update job status
    new_job_status = "verified" if passed else "disputed"
    db_update_job_status(conn, execution["job_id"], new_job_status)
    conn.commit()

    # Update agent reputation
    if passed:
        rating = max(1, min(5, round(score * 5)))
        db_update_agent_reputation(conn, execution["agent_id"], float(rating))
        db_create_reputation_event(
            conn,
            agent_id=execution["agent_id"],
            job_id=execution["job_id"],
            rating=rating,
            review_text=notes,
        )
        conn.commit()

    return {
        "execution_id": exec_id,
        "status": v_status,
        "score": score,
        "notes": notes,
        "passed": passed,
        "job_id": execution["job_id"],
        "agent_id": execution["agent_id"],
    }


# ---------------------------------------------------------------------------
# Payment System (Simulated)
# ---------------------------------------------------------------------------

PLATFORM_CUT = 0.20   # 20%
AGENT_CUT = 0.80      # 80%


def process_payment(
    conn: sqlite3.Connection,
    job_id: str,
) -> Dict[str, Any]:
    """
    Process 80/20 payout split after verification.
    Returns payout receipt dict.
    """
    job = db_get_job(conn, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    if job["status"] != "verified":
        raise ValueError(f"Job must be verified before payment (current: {job['status']})")

    execution = db_get_execution_by_job(conn, job_id)
    if execution is None:
        raise ValueError("No execution record found for this job")

    bounty = job["bounty_usd"]
    agent_amount = round(bounty * AGENT_CUT, 2)
    platform_amount = round(bounty * PLATFORM_CUT, 2)

    agent = db_get_agent(conn, execution["agent_id"])
    if agent is None:
        raise ValueError("Agent not found")
    owner_id = agent["owner_id"]

    # Generate simulated stripe_id / crypto hash
    stripe_id = "pi_" + secrets.token_hex(12)
    crypto_hash = "0x" + secrets.token_hex(32)

    # Record escrow release (from lister to platform)
    lister_id = job["lister_id"]
    tx_escrow = db_create_transaction(
        conn,
        job_id=job_id,
        from_user_id=lister_id,
        to_user_id=None,
        amount_usd=bounty,
        tx_type="escrow",
        status="completed",
        stripe_id=stripe_id,
    )

    # Record agent payout
    tx_agent = db_create_transaction(
        conn,
        job_id=job_id,
        from_user_id=None,
        to_user_id=owner_id,
        amount_usd=agent_amount,
        tx_type="payout_agent_owner",
        status="completed",
        stripe_id=stripe_id,
    )

    # Record platform fee
    tx_platform = db_create_transaction(
        conn,
        job_id=job_id,
        from_user_id=None,
        to_user_id=None,
        amount_usd=platform_amount,
        tx_type="platform_fee",
        status="completed",
        stripe_id=stripe_id,
    )

    # Update balances
    db_adjust_balance(conn, owner_id, agent_amount)
    conn.commit()

    # Update job status to paid
    db_update_job_status(conn, job_id, "paid")
    conn.commit()

    return {
        "job_id": job_id,
        "job_title": job["title"],
        "bounty_usd": bounty,
        "agent_payout_usd": agent_amount,
        "platform_fee_usd": platform_amount,
        "agent_owner_id": owner_id,
        "tx_escrow_id": tx_escrow,
        "tx_agent_id": tx_agent,
        "tx_platform_id": tx_platform,
        "stripe_id": stripe_id,
        "crypto_tx_hash": crypto_hash,
    }


# ---------------------------------------------------------------------------
# Marketplace stats
# ---------------------------------------------------------------------------

def get_marketplace_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    total_users = rows["c"]

    rows = conn.execute("SELECT COUNT(*) as c FROM agents").fetchone()
    total_agents = rows["c"]

    rows = conn.execute("SELECT COUNT(*) as c FROM jobs").fetchone()
    total_jobs = rows["c"]

    rows = conn.execute(
        "SELECT COUNT(*) as c FROM jobs WHERE status='paid'"
    ).fetchone()
    paid_jobs = rows["c"]

    rows = conn.execute(
        "SELECT COALESCE(SUM(amount_usd),0) as total FROM transactions WHERE tx_type='payout_agent_owner' AND status='completed'"
    ).fetchone()
    total_agent_payouts = rows["total"]

    rows = conn.execute(
        "SELECT COALESCE(SUM(amount_usd),0) as total FROM transactions WHERE tx_type='platform_fee' AND status='completed'"
    ).fetchone()
    platform_revenue = rows["total"]

    rows = conn.execute(
        "SELECT COALESCE(AVG(reputation_score),0) as avg FROM agents"
    ).fetchone()
    avg_reputation = rows["avg"]

    rows = conn.execute(
        "SELECT COALESCE(AVG(verification_score),0) as avg FROM executions WHERE verification_status='passed'"
    ).fetchone()
    avg_verification = rows["avg"]

    job_statuses = {}
    for r in conn.execute("SELECT status, COUNT(*) as c FROM jobs GROUP BY status"):
        job_statuses[r["status"]] = r["c"]

    return {
        "total_users": total_users,
        "total_agents": total_agents,
        "total_jobs": total_jobs,
        "paid_jobs": paid_jobs,
        "total_agent_payouts": round(total_agent_payouts, 2),
        "platform_revenue": round(platform_revenue, 2),
        "total_volume": round(total_agent_payouts + platform_revenue, 2),
        "avg_reputation": round(avg_reputation, 2),
        "avg_verification_score": round(avg_verification, 3),
        "job_statuses": job_statuses,
    }


# ---------------------------------------------------------------------------
# Pretty printing helpers (Rich + ANSI fallbacks)
# ---------------------------------------------------------------------------

def _status_color(status: str) -> str:
    """Get Rich color for a status string."""
    return {
        # Agent statuses
        "idle": "green",
        "busy": "yellow",
        "offline": "red",
        # Job statuses
        "open": "green",
        "matched": "yellow",
        "in_progress": "blue",
        "submitted": "cyan",
        "verified": "magenta",
        "paid": "bright_green",
        "disputed": "red",
        # Verification statuses
        "passed": "green",
        "pending": "yellow",
        "failed": "red",
        # Transaction statuses
        "completed": "green",
    }.get(status, "white")


def print_user_card(user: Dict[str, Any]) -> None:
    if HAS_RICH:
        t = Table(show_header=False, box=box.ROUNDED, border_style="cyan")
        t.add_column("Field", style="bold cyan", width=16)
        t.add_column("Value", style="white")
        t.add_row("ID", short_id(user["id"]) + "…")
        t.add_row("Username", user["username"])
        t.add_row("Email", user["email"])
        t.add_row("Role", user["role"])
        t.add_row("API Key", user["api_key"][:24] + "…")
        t.add_row("Balance", fmt_usd(user.get("balance_usd", 0.0)))
        console.print(Panel(t, title="[bold green]User Created[/bold green]", expand=False))
    else:
        print(f"\n{ansi('USER CREATED', ANSI.BOLD, ANSI.GREEN)}")
        print(f"  ID:       {short_id(user['id'])}…")
        print(f"  Username: {user['username']}")
        print(f"  Email:    {user['email']}")
        print(f"  Role:     {user['role']}")
        print(f"  API Key:  {user['api_key'][:24]}…")
        print(f"  Balance:  {fmt_usd(user.get('balance_usd', 0.0))}\n")


def print_user_table(users: List[sqlite3.Row]) -> None:
    if HAS_RICH:
        t = Table(
            title="Users",
            box=box.SIMPLE_HEAD,
            border_style="dim",
            header_style="bold cyan",
        )
        t.add_column("ID", width=10)
        t.add_column("Username", width=20)
        t.add_column("Email", width=28)
        t.add_column("Role", width=14)
        t.add_column("Balance", justify="right", width=12)
        t.add_column("Created", width=12)
        for u in users:
            t.add_row(
                short_id(u["id"]) + "…",
                u["username"],
                truncate(u["email"], 26),
                u["role"],
                fmt_usd(u["balance_usd"]),
                time_ago(u["created_at"]),
            )
        console.print(t)
    else:
        print(f"\n{'ID':<10} {'Username':<20} {'Email':<28} {'Role':<14} {'Balance':>12} {'Created':<12}")
        print("─" * 100)
        for u in users:
            print(
                f"{short_id(u['id'])+'…':<10} {u['username']:<20} {truncate(u['email'], 26):<28} "
                f"{u['role']:<14} {fmt_usd(u['balance_usd']):>12} {time_ago(u['created_at']):<12}"
            )
        print()


def print_job_table(jobs: List[sqlite3.Row]) -> None:
    if HAS_RICH:
        t = Table(
            title="Jobs",
            box=box.SIMPLE_HEAD,
            border_style="dim",
            header_style="bold cyan",
        )
        t.add_column("ID", width=10)
        t.add_column("Title", width=32)
        t.add_column("Category", width=12)
        t.add_column("Bounty", justify="right", width=10)
        t.add_column("Status", width=14)
        t.add_column("Priority", width=8)
        t.add_column("Created", width=10)
        for j in jobs:
            sc = _status_color(j["status"])
            t.add_row(
                short_id(j["id"]) + "…",
                truncate(j["title"], 30),
                j["category"],
                fmt_usd(j["bounty_usd"]),
                f"[{sc}]{j['status']}[/{sc}]",
                j["priority"],
                time_ago(j["created_at"]),
            )
        console.print(t)
    else:
        print(f"\n{'ID':<10} {'Title':<32} {'Category':<12} {'Bounty':>10} {'Status':<14} {'Priority':<8} {'Created'}")
        print("─" * 96)
        for j in jobs:
            print(
                f"{short_id(j['id'])+'…':<10} {truncate(j['title'], 30):<32} {j['category']:<12} "
                f"{fmt_usd(j['bounty_usd']):>10} {j['status']:<14} {j['priority']:<8} {time_ago(j['created_at'])}"
            )
        print()


def print_agent_table(agents: List[sqlite3.Row]) -> None:
    if HAS_RICH:
        t = Table(
            title="Registered Agents",
            box=box.SIMPLE_HEAD,
            border_style="dim",
            header_style="bold magenta",
        )
        t.add_column("ID", width=10)
        t.add_column("Name", width=26)
        t.add_column("Status", width=10)
        t.add_column("Reputation", justify="right", width=10)
        t.add_column("Jobs Done", justify="right", width=10)
        t.add_column("Avg Rating", justify="right", width=10)
        t.add_column("Last Seen", width=10)
        for a in agents:
            sc = _status_color(a["status"])
            hb = time_ago(a["last_heartbeat"]) if a["last_heartbeat"] else "never"
            t.add_row(
                short_id(a["id"]) + "…",
                truncate(a["name"], 24),
                f"[{sc}]{a['status']}[/{sc}]",
                f"{a['reputation_score']:.2f}",
                str(a["jobs_completed"]),
                f"{a['avg_rating']:.2f}",
                hb,
            )
        console.print(t)
    else:
        print(f"\n{'ID':<10} {'Name':<26} {'Status':<10} {'Rep':>10} {'Jobs':>8} {'Rating':>8} {'Last Seen'}")
        print("─" * 86)
        for a in agents:
            hb = time_ago(a["last_heartbeat"]) if a["last_heartbeat"] else "never"
            print(
                f"{short_id(a['id'])+'…':<10} {truncate(a['name'], 24):<26} {a['status']:<10} "
                f"{a['reputation_score']:>10.2f} {a['jobs_completed']:>8} {a['avg_rating']:>8.2f} {hb}"
            )
        print()


def print_match_results(matches: List[Dict[str, Any]], job_title: str) -> None:
    if HAS_RICH:
        t = Table(
            title=f"Top Matches for: [italic]{truncate(job_title, 50)}[/italic]",
            box=box.ROUNDED,
            border_style="yellow",
            header_style="bold yellow",
        )
        t.add_column("Rank", justify="center", width=6)
        t.add_column("Agent ID", width=10)
        t.add_column("Name", width=26)
        t.add_column("Similarity", justify="right", width=12)
        t.add_column("Reputation", justify="right", width=12)
        t.add_column("Status", width=10)
        for i, m in enumerate(matches, 1):
            bar = "█" * int(m["score"] * 20)
            sc = _status_color(m["status"])
            t.add_row(
                f"#{i}",
                short_id(m["agent_id"]) + "…",
                truncate(m["name"], 24),
                f"[bold green]{m['score']:.4f}[/bold green] {bar}",
                f"{m['reputation_score']:.2f}",
                f"[{sc}]{m['status']}[/{sc}]",
            )
        console.print(t)
    else:
        print(f"\n  Matches for: {truncate(job_title, 50)}")
        print(f"  {'Rank':<5} {'Agent':<26} {'Score':>8} {'Rep':>8} {'Status'}")
        print("  " + "─" * 55)
        for i, m in enumerate(matches, 1):
            bar = "█" * int(m["score"] * 20)
            print(f"  #{i:<4} {truncate(m['name'], 24):<26} {m['score']:>8.4f} {m['reputation_score']:>8.2f}  {m['status']}  {bar}")
        print()


def print_payout_receipt(receipt: Dict[str, Any]) -> None:
    if HAS_RICH:
        t = Table(show_header=False, box=box.DOUBLE, border_style="bright_green")
        t.add_column("Item", style="bold", width=24)
        t.add_column("Value", justify="right", width=20)
        t.add_row("Job", truncate(receipt["job_title"], 20))
        t.add_row("Total Bounty", f"[bold]{fmt_usd(receipt['bounty_usd'])}[/bold]")
        t.add_row("─" * 20, "─" * 16)
        t.add_row(
            "Agent Payout (80%)",
            f"[bold bright_green]{fmt_usd(receipt['agent_payout_usd'])}[/bold bright_green]",
        )
        t.add_row(
            "Platform Fee (20%)",
            f"[dim]{fmt_usd(receipt['platform_fee_usd'])}[/dim]",
        )
        t.add_row("Stripe ID", receipt["stripe_id"][:24] + "…")
        console.print(
            Panel(
                t,
                title="[bold bright_green]✓ PAYOUT RECEIPT[/bold bright_green]",
                subtitle=f"[dim]{receipt['job_id'][:8]}…[/dim]",
                expand=False,
            )
        )
    else:
        print(f"\n{ansi('  ✓ PAYOUT RECEIPT', ANSI.BOLD, ANSI.GREEN)}")
        print(f"  Job:              {truncate(receipt['job_title'], 40)}")
        print(f"  Total Bounty:     {fmt_usd(receipt['bounty_usd'])}")
        print(f"  Agent Payout 80%: {ansi(fmt_usd(receipt['agent_payout_usd']), ANSI.GREEN, ANSI.BOLD)}")
        print(f"  Platform Fee 20%: {fmt_usd(receipt['platform_fee_usd'])}")
        print(f"  Stripe ID:        {receipt['stripe_id'][:24]}…\n")


def print_transaction_table(txns: List[sqlite3.Row]) -> None:
    if HAS_RICH:
        t = Table(
            title="Transaction History",
            box=box.SIMPLE_HEAD,
            border_style="dim",
            header_style="bold green",
        )
        t.add_column("ID", width=10)
        t.add_column("Job", width=10)
        t.add_column("Type", width=22)
        t.add_column("Amount", justify="right", width=12)
        t.add_column("Status", width=12)
        t.add_column("When", width=12)
        for tx in txns:
            sc = _status_color(tx["status"])
            type_display = tx["tx_type"].replace("_", " ").title()
            t.add_row(
                short_id(tx["id"]) + "…",
                short_id(tx["job_id"]) + "…" if tx["job_id"] else "N/A",
                type_display,
                fmt_usd(tx["amount_usd"]),
                f"[{sc}]{tx['status']}[/{sc}]",
                time_ago(tx["created_at"]),
            )
        console.print(t)
    else:
        print(f"\n{'ID':<10} {'Job':<10} {'Type':<22} {'Amount':>12} {'Status':<12} {'When'}")
        print("─" * 78)
        for tx in txns:
            type_display = tx["tx_type"].replace("_", " ").title()
            print(
                f"{short_id(tx['id'])+'…':<10} "
                f"{(short_id(tx['job_id'])+'…') if tx['job_id'] else 'N/A':<10} "
                f"{type_display:<22} "
                f"{fmt_usd(tx['amount_usd']):>12} "
                f"{tx['status']:<12} "
                f"{time_ago(tx['created_at'])}"
            )
        print()


def print_leaderboard(agents: List[Dict[str, Any]]) -> None:
    if HAS_RICH:
        t = Table(
            title="Agent Leaderboard",
            box=box.ROUNDED,
            border_style="bright_green",
            header_style="bold bright_green",
        )
        t.add_column("Rank", justify="center", width=6)
        t.add_column("Agent", width=26)
        t.add_column("Owner", width=18)
        t.add_column("Reputation", justify="right", width=12)
        t.add_column("Jobs Done", justify="right", width=10)
        t.add_column("Avg Rating", justify="right", width=10)
        t.add_column("Earnings", justify="right", width=12)
        t.add_column("Status", width=10)
        for i, a in enumerate(agents, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
            sc = _status_color(a["status"])
            t.add_row(
                medal,
                truncate(a["name"], 24),
                truncate(a["owner_name"], 16),
                f"[bold]{a['reputation_score']:.2f}[/bold]",
                str(a["jobs_completed"]),
                f"{a['avg_rating']:.2f}",
                fmt_usd(a["earnings"]),
                f"[{sc}]{a['status']}[/{sc}]",
            )
        console.print(t)
    else:
        print(f"\n{'Rank':<6} {'Agent':<26} {'Owner':<18} {'Rep':>8} {'Jobs':>6} {'Rating':>7} {'Earnings':>12} {'Status'}")
        print("─" * 98)
        for i, a in enumerate(agents, 1):
            print(
                f"#{i:<5} {truncate(a['name'], 24):<26} {truncate(a['owner_name'], 16):<18} "
                f"{a['reputation_score']:>8.2f} {a['jobs_completed']:>6} {a['avg_rating']:>7.2f} "
                f"{fmt_usd(a['earnings']):>12} {a['status']}"
            )
        print()


def print_stats(stats: Dict[str, Any]) -> None:
    if HAS_RICH:
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        def stat_panel(label: str, value: str, color: str = "cyan") -> Panel:
            return Panel(
                Align.center(f"[bold {color}]{value}[/bold {color}]\n[dim]{label}[/dim]"),
                box=box.ROUNDED,
                border_style=color,
                expand=True,
            )

        grid.add_row(
            stat_panel("Total Users", str(stats["total_users"]), "cyan"),
            stat_panel("Total Agents", str(stats["total_agents"]), "magenta"),
            stat_panel("Total Jobs", str(stats["total_jobs"]), "yellow"),
        )
        grid.add_row(
            stat_panel("Paid Jobs", str(stats["paid_jobs"]), "bright_green"),
            stat_panel("Agent Payouts", fmt_usd(stats["total_agent_payouts"]), "green"),
            stat_panel("Platform Revenue", fmt_usd(stats["platform_revenue"]), "blue"),
        )
        console.print(Panel(grid, title="[bold]Marketplace Statistics[/bold]", border_style="dim"))

        # Job status breakdown
        if stats["job_statuses"]:
            st_table = Table(title="Job Status Breakdown", box=box.SIMPLE, header_style="bold")
            st_table.add_column("Status")
            st_table.add_column("Count", justify="right")
            for s, c in stats["job_statuses"].items():
                sc = _status_color(s)
                st_table.add_row(f"[{sc}]{s}[/{sc}]", str(c))
            console.print(st_table)
    else:
        print(f"\n{ansi('MARKETPLACE STATISTICS', ANSI.BOLD, ANSI.CYAN)}")
        print(f"  Users:            {stats['total_users']}")
        print(f"  Agents:           {stats['total_agents']}")
        print(f"  Total Jobs:       {stats['total_jobs']}")
        print(f"  Paid Jobs:        {stats['paid_jobs']}")
        print(f"  Agent Payouts:    {fmt_usd(stats['total_agent_payouts'])}")
        print(f"  Platform Revenue: {fmt_usd(stats['platform_revenue'])}")
        print(f"  Total Volume:     {fmt_usd(stats['total_volume'])}")
        print(f"  Avg Reputation:   {stats['avg_reputation']:.2f}/10")
        print(f"  Avg Verif Score:  {stats['avg_verification_score']:.3f}\n")
        if stats["job_statuses"]:
            print("  Job statuses:")
            for s, c in stats["job_statuses"].items():
                print(f"    {s}: {c}")
        print()


# ---------------------------------------------------------------------------
# Demo command
# ---------------------------------------------------------------------------

DEMO_JOBS = [
    {
        "title": "Comprehensive Market Research Report on AI-Powered Fintech",
        "description": (
            "We need a thorough market research report covering the AI-powered fintech sector. "
            "Include competitive landscape, growth projections, regulatory environment, and "
            "key investment opportunities. Focus on APAC and North American markets."
        ),
        "category": "research",
        "skills": ["market research", "fintech", "data analysis", "report writing", "AI"],
        "bounty": 450.00,
        "priority": "high",
    },
    {
        "title": "Build a FastAPI Microservice for Real-Time Data Ingestion",
        "description": (
            "Develop a production-ready FastAPI microservice that ingests real-time streaming "
            "data from Kafka, normalises it, and writes to a PostgreSQL database. Must include "
            "authentication, rate limiting, health checks, and full test coverage."
        ),
        "category": "code",
        "skills": ["Python", "FastAPI", "Kafka", "PostgreSQL", "Docker", "pytest"],
        "bounty": 850.00,
        "priority": "urgent",
    },
    {
        "title": "Customer Churn Predictive Analysis for SaaS Platform",
        "description": (
            "Perform end-to-end predictive analysis on our SaaS customer dataset to identify "
            "churn risk factors. Deliver a trained model, feature importance report, and "
            "actionable retention strategies with ROI projections."
        ),
        "category": "analysis",
        "skills": ["machine learning", "data science", "Python", "churn prediction", "statistics"],
        "bounty": 620.00,
        "priority": "high",
    },
]

DEMO_AGENTS = [
    {
        "name": "ResearchBot-Omega",
        "description": (
            "Specialised in deep market research, competitive intelligence, industry analysis, "
            "and structured report writing. Expert in fintech, AI, SaaS, and enterprise software "
            "verticals. Leverages 200+ data sources and LLM synthesis."
        ),
        "capabilities": [
            "market research", "competitive analysis", "report writing",
            "data synthesis", "fintech", "AI industry", "investment analysis",
            "trend forecasting", "regulatory research",
        ],
    },
    {
        "name": "CodeCraft-Pro",
        "description": (
            "Full-stack software engineering agent. Expert in Python, FastAPI, microservices, "
            "database design, machine learning pipelines, data science, statistics, and "
            "predictive modelling. Delivers production-quality code with full test coverage."
        ),
        "capabilities": [
            "Python", "FastAPI", "PostgreSQL", "Kafka", "Docker", "machine learning",
            "data science", "pandas", "scikit-learn", "pytest", "statistics",
            "churn prediction", "REST API", "microservices",
        ],
    },
]


def run_demo() -> None:
    """Full end-to-end marketplace demo."""

    # ── Banner ───────────────────────────────────────────────────────────
    if HAS_RICH:
        banner = Text()
        banner.append("  ████████╗ ██████╗ ██████╗ ██████╗  ██████╗  █████╗ \n", style="bold cyan")
        banner.append("     ██╔══╝██╔═══██╗██╔══██╗╚════██╗██╔════╝ ██╔══██╗\n", style="bold cyan")
        banner.append("     ██║   ██║   ██║██████╔╝ █████╔╝██║  ███╗███████║\n", style="bold cyan")
        banner.append("     ██║   ██║   ██║██╔══██╗██╔═══╝ ██║   ██║██╔══██║\n", style="bold cyan")
        banner.append("     ██║   ╚██████╔╝██║  ██║███████╗╚██████╔╝██║  ██║\n", style="bold cyan")
        banner.append("     ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝\n", style="bold cyan")
        banner.append("           AI Agent Marketplace Core Engine v1.0\n", style="bold white")
        banner.append("           tor2ga.ai  ·  Full End-to-End Demo\n", style="dim")
        console.print(Panel(Align.center(banner), border_style="cyan", box=box.DOUBLE_EDGE))
    else:
        print(ansi("\n  ╔══════════════════════════════════════╗", ANSI.CYAN))
        print(ansi("  ║   TOR2GA.AI  AI Marketplace Engine   ║", ANSI.CYAN, ANSI.BOLD))
        print(ansi("  ║       Full End-to-End Demo v1.0      ║", ANSI.CYAN))
        print(ansi("  ╚══════════════════════════════════════╝\n", ANSI.CYAN))

    # ── Step 1: Init fresh DB ─────────────────────────────────────────────
    print_header("Step 1 of 9 — Initialize Fresh Database")
    if DB_PATH.exists():
        DB_PATH.unlink()
    cmd_init()
    print_success(f"Database initialised at {DB_PATH}")

    with get_connection() as conn:

        # ── Step 2: Create users ──────────────────────────────────────────
        print_header("Step 2 of 9 — Create Marketplace Users")
        lister = db_create_user(conn, "alice_ventures", "alice@ventures.io", "lister")
        owner  = db_create_user(conn, "bob_agentworks", "bob@agentworks.ai", "agent_owner")
        platform = db_create_user(conn, "tor2ga_platform", "platform@tor2ga.ai", "both")
        print_success(f"Lister created:       {lister['username']}  ({lister['email']})")
        print_success(f"Agent owner created:  {owner['username']}  ({owner['email']})")
        print_success(f"Platform account:     {platform['username']}")

        # ── Step 3: Post jobs ─────────────────────────────────────────────
        print_header("Step 3 of 9 — Post 3 Sample Jobs")
        job_ids: List[str] = []
        for jd in DEMO_JOBS:
            result = db_post_job(
                conn,
                lister_id=lister["id"],
                title=jd["title"],
                description=jd["description"],
                category=jd["category"],
                skills_required=jd["skills"],
                bounty_usd=jd["bounty"],
                priority=jd["priority"],
            )
            job_ids.append(result["id"])
            print_success(
                f"Job posted: {truncate(jd['title'], 55)} [{fmt_usd(jd['bounty'])}]"
            )

        print_job_table(db_list_jobs(conn))

        # ── Step 4: Register agents ───────────────────────────────────────
        print_header("Step 4 of 9 — Register 2 Agents")
        agent_ids: List[str] = []
        if HAS_RICH:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                transient=True,
            ) as progress:
                task = progress.add_task("Generating embeddings…", total=len(DEMO_AGENTS))
                for ad in DEMO_AGENTS:
                    result = db_register_agent(
                        conn,
                        owner_id=owner["id"],
                        name=ad["name"],
                        description=ad["description"],
                        capabilities=ad["capabilities"],
                    )
                    agent_ids.append(result["id"])
                    progress.advance(task)
        else:
            for ad in DEMO_AGENTS:
                print_info(f"Registering agent: {ad['name']} (generating embeddings…)")
                result = db_register_agent(
                    conn,
                    owner_id=owner["id"],
                    name=ad["name"],
                    description=ad["description"],
                    capabilities=ad["capabilities"],
                )
                agent_ids.append(result["id"])
                print_success(f"Agent registered: {ad['name']}")

        print_agent_table(db_list_agents(conn))

        # ── Step 5: Match jobs ────────────────────────────────────────────
        print_header("Step 5 of 9 — Match Jobs to Best Agents")
        job_agent_pairs: List[Tuple[str, str]] = []
        all_jobs = db_list_jobs(conn)

        for job in all_jobs:
            matches = find_best_agents(conn, job["id"], top_k=2)
            if not matches:
                print_warning(f"No matches for job: {truncate(job['title'], 40)}")
                continue
            best = matches[0]
            print_match_results(matches, job["title"])
            # Update job to matched
            db_update_job_status(conn, job["id"], "matched", best["agent_id"])
            conn.commit()
            job_agent_pairs.append((job["id"], best["agent_id"]))
            print_success(
                f"Matched  '{truncate(job['title'], 45)}'"
                f"  →  {best['name']}  (score: {best['score']:.4f})"
            )

        # ── Step 6: Execute jobs ──────────────────────────────────────────
        print_header("Step 6 of 9 — Execute All Matched Jobs")
        exec_ids: List[str] = []
        for job_id, agent_id in job_agent_pairs:
            job = db_get_job(conn, job_id)
            agent = db_get_agent(conn, agent_id)
            print_info(
                f"Agent [{agent['name']}] executing: {truncate(job['title'], 45)}…"
            )
            result = run_execution(conn, job_id, agent_id)
            exec_ids.append(result["execution_id"])
            # Show a snippet of the output
            snippet = result["output_text"][:200].replace("\n", " ")
            if HAS_RICH:
                console.print(
                    Panel(
                        f"[dim]{snippet}…[/dim]",
                        title=f"[bold]Execution Output — {agent['name']}[/bold]",
                        border_style="blue",
                        expand=False,
                    )
                )
            else:
                print(f"  Output snippet: {snippet[:120]}…")
            print_success(f"Execution complete  →  ID: {short_id(result['execution_id'])}…")

        # ── Step 7: Verify executions ─────────────────────────────────────
        print_header("Step 7 of 9 — Verify All Executions")
        for eid in exec_ids:
            result = verify_execution(conn, eid)
            color_fn = print_success if result["passed"] else print_warning
            color_fn(
                f"Execution {short_id(eid)}…  →  {result['status'].upper()}"
                f"  (score: {result['score']:.3f})  |  {truncate(result['notes'], 80)}"
            )

        # ── Step 8: Process payouts ───────────────────────────────────────
        print_header("Step 8 of 9 — Process Payouts (80 / 20 Split)")
        total_paid = 0.0
        for job_id, _ in job_agent_pairs:
            job = db_get_job(conn, job_id)
            if job["status"] != "verified":
                print_warning(f"Job {short_id(job_id)}… not verified, skipping payout")
                continue
            try:
                receipt = process_payment(conn, job_id)
                print_payout_receipt(receipt)
                total_paid += receipt["bounty_usd"]
            except Exception as e:
                print_error(str(e))

        # ── Step 9: Final stats ───────────────────────────────────────────
        print_header("Step 9 of 9 — Final Marketplace Statistics")
        stats = get_marketplace_stats(conn)
        print_stats(stats)

        # Summary panel
        if HAS_RICH:
            summary = (
                f"[bold green]Demo complete![/bold green]  "
                f"Processed [bold]{stats['total_jobs']}[/bold] jobs, "
                f"matched and executed via [bold]{stats['total_agents']}[/bold] AI agents.\n"
                f"Total marketplace volume: [bold bright_green]{fmt_usd(stats['total_volume'])}[/bold bright_green]  "
                f"·  Platform revenue: [bold blue]{fmt_usd(stats['platform_revenue'])}[/bold blue]\n"
                f"Agent payouts: [bold]{fmt_usd(stats['total_agent_payouts'])}[/bold]  "
                f"·  Avg verification score: [bold]{stats['avg_verification_score']:.3f}[/bold]"
            )
            console.print(Panel(summary, title="[bold]tor2ga.ai[/bold]", border_style="bright_green", box=box.DOUBLE_EDGE))
        else:
            print(ansi(f"\n  Demo complete! Volume: {fmt_usd(stats['total_volume'])}", ANSI.BOLD, ANSI.GREEN))
            print(f"  Platform revenue: {fmt_usd(stats['platform_revenue'])}\n")


# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------

def cmd_init() -> None:
    """Initialize the database schema."""
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def cmd_user_create(args: argparse.Namespace) -> None:
    if not validate_email(args.email):
        print_error(f"Invalid email address: '{args.email}'. Must contain @ and a domain.")
        sys.exit(1)
    with get_connection() as conn:
        try:
            user = db_create_user(conn, args.username, args.email, args.role)
            print_user_card(user)
        except sqlite3.IntegrityError as e:
            err_str = str(e).lower()
            if "username" in err_str:
                print_error(f"Username '{args.username}' is already taken.")
            elif "email" in err_str:
                print_error(f"Email '{args.email}' is already registered.")
            else:
                print_error(f"User already exists: {e}")
            sys.exit(1)


def cmd_user_list(_args: argparse.Namespace) -> None:
    with get_connection() as conn:
        users = db_list_users(conn)
        if not users:
            print_info("No users found. Create one with: tor2ga user create --username <name> --email <email> --role <role>")
            return
        print_user_table(users)


def cmd_job_post(args: argparse.Namespace) -> None:
    if not validate_bounty(args.bounty):
        print_error("Bounty must be a positive number.")
        sys.exit(1)
    skills = [s.strip() for s in args.skills.split(",") if s.strip()]
    if not skills:
        print_error("At least one skill is required (comma-separated).")
        sys.exit(1)
    with get_connection() as conn:
        try:
            user = resolve_user_ref(conn, args.lister)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        result = db_post_job(
            conn,
            lister_id=user["id"],
            title=args.title,
            description=args.description,
            category=args.category,
            skills_required=skills,
            bounty_usd=float(args.bounty),
            priority=getattr(args, "priority", "normal"),
        )
        print_success(f"Job posted! ID: {result['id']}")
        print_info(f"  Short ID: {short_id(result['id'])}")
        print_info(f"  Title:    {result['title']}")
        print_info(f"  Bounty:   {fmt_usd(result['bounty_usd'])}")


def cmd_job_list(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        status_filter = getattr(args, "status", None)
        jobs = db_list_jobs(conn, status=status_filter)
        if not jobs:
            msg = "No jobs found"
            if status_filter:
                msg += f" with status '{status_filter}'"
            msg += "."
            print_info(msg)
            return
        print_job_table(jobs)


def cmd_job_view(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        try:
            job_id = resolve_id(conn, "jobs", args.id)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        job = db_get_job(conn, job_id)
        if job is None:
            print_error(f"Job '{args.id}' not found.")
            sys.exit(1)
        skills = json.loads(job["skills_required"])
        lister = db_get_user(conn, job["lister_id"])
        lister_name = lister["username"] if lister else "unknown"
        agent_name = "N/A"
        if job["matched_agent_id"]:
            agent = db_get_agent(conn, job["matched_agent_id"])
            agent_name = agent["name"] if agent else "unknown"

        if HAS_RICH:
            t = Table(show_header=False, box=box.ROUNDED, border_style="yellow")
            t.add_column("Field", style="bold yellow", width=18)
            t.add_column("Value")
            t.add_row("ID", job["id"])
            t.add_row("Title", job["title"])
            t.add_row("Description", textwrap.fill(job["description"], 60))
            t.add_row("Category", job["category"])
            t.add_row("Skills", ", ".join(skills))
            t.add_row("Bounty", fmt_usd(job["bounty_usd"]))
            sc = _status_color(job["status"])
            t.add_row("Status", f"[{sc}]{job['status']}[/{sc}]")
            t.add_row("Priority", job["priority"])
            t.add_row("Lister", lister_name)
            t.add_row("Matched Agent", agent_name)
            t.add_row("Created", f"{job['created_at'][:19]}  ({time_ago(job['created_at'])})")
            console.print(Panel(t, title="[bold]Job Details[/bold]", expand=False))
        else:
            print(f"\n  ID:            {job['id']}")
            print(f"  Title:         {job['title']}")
            print(f"  Description:   {textwrap.fill(job['description'], 60, subsequent_indent='                 ')}")
            print(f"  Category:      {job['category']}")
            print(f"  Skills:        {', '.join(skills)}")
            print(f"  Bounty:        {fmt_usd(job['bounty_usd'])}")
            print(f"  Status:        {job['status']}")
            print(f"  Priority:      {job['priority']}")
            print(f"  Lister:        {lister_name}")
            print(f"  Matched Agent: {agent_name}")
            print(f"  Created:       {job['created_at'][:19]}  ({time_ago(job['created_at'])})\n")


def cmd_agent_register(args: argparse.Namespace) -> None:
    capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    if not capabilities:
        print_error("At least one capability is required (comma-separated).")
        sys.exit(1)
    with get_connection() as conn:
        try:
            owner = resolve_user_ref(conn, args.owner)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        result = db_register_agent(
            conn,
            owner_id=owner["id"],
            name=args.name,
            description=args.description,
            capabilities=capabilities,
        )
        print_success(f"Agent '{result['name']}' registered!")
        print_info(f"  ID: {result['id']}")
        print_info(f"  Short ID: {short_id(result['id'])}")


def cmd_agent_list(_args: argparse.Namespace) -> None:
    with get_connection() as conn:
        agents = db_list_agents(conn)
        if not agents:
            print_info("No agents registered. Register one with: tor2ga agent register ...")
            return
        print_agent_table(agents)


def cmd_agent_heartbeat(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        try:
            agent_id = resolve_id(conn, "agents", args.id)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        agent = db_get_agent(conn, agent_id)
        if agent is None:
            print_error(f"Agent '{args.id}' not found.")
            sys.exit(1)
        db_set_agent_status(conn, agent_id, "idle")
        conn.commit()
        print_success(f"Heartbeat sent for agent '{agent['name']}' ({short_id(agent_id)}…)")
        print_info(f"  Status: idle  |  Last heartbeat: {now_iso()[:19]}")


def cmd_match(args: argparse.Namespace) -> None:
    auto_mode = getattr(args, "auto", False)

    with get_connection() as conn:
        if auto_mode:
            # Auto-match all open jobs
            open_jobs = db_list_jobs(conn, status="open")
            if not open_jobs:
                print_info("No open jobs to match.")
                return
            print_header(f"Auto-matching {len(open_jobs)} open jobs")
            matched_count = 0
            for job in open_jobs:
                matches = find_best_agents(conn, job["id"], top_k=3)
                if not matches:
                    print_warning(f"No agents available for: {truncate(job['title'], 40)}")
                    continue
                best = matches[0]
                db_update_job_status(conn, job["id"], "matched", best["agent_id"])
                conn.commit()
                matched_count += 1
                print_success(
                    f"'{truncate(job['title'], 40)}'  →  {best['name']}  "
                    f"(score: {best['score']:.4f})"
                )
            print_info(f"\nMatched {matched_count}/{len(open_jobs)} jobs.")
            return

        # Single job match
        job_id_raw = getattr(args, "job_id", None)
        if not job_id_raw:
            print_error("Provide --job-id or use --auto to match all open jobs.")
            sys.exit(1)

        try:
            job_id = resolve_id(conn, "jobs", job_id_raw)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)

        job = db_get_job(conn, job_id)
        if job is None:
            print_error(f"Job '{job_id_raw}' not found.")
            sys.exit(1)

        if job["status"] not in ("open", "matched"):
            print_warning(f"Job is already in status '{job['status']}'. Matching may not apply.")

        top_k = getattr(args, "top_k", 5) or 5
        matches = find_best_agents(conn, job_id, top_k=top_k)
        if not matches:
            print_warning("No available agents found.")
            return
        print_match_results(matches, job["title"])
        best = matches[0]

        # Auto-assign best match
        db_update_job_status(conn, job_id, "matched", best["agent_id"])
        conn.commit()
        print_success(
            f"Best match: {best['name']} ({short_id(best['agent_id'])}…)  "
            f"score={best['score']:.4f}  — Job status updated to 'matched'"
        )


def cmd_execute(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        # Resolve job ID
        try:
            job_id = resolve_id(conn, "jobs", args.job_id)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)

        job = db_get_job(conn, job_id)
        if job is None:
            print_error(f"Job '{args.job_id}' not found.")
            sys.exit(1)

        # Determine agent ID
        agent_id_raw = getattr(args, "agent_id", None)
        if agent_id_raw:
            try:
                agent_id = resolve_id(conn, "agents", agent_id_raw)
            except ValueError as e:
                print_error(str(e))
                sys.exit(1)
        elif job["matched_agent_id"]:
            agent_id = job["matched_agent_id"]
            agent = db_get_agent(conn, agent_id)
            agent_name = agent["name"] if agent else "unknown"
            print_info(f"Using matched agent: {agent_name} ({short_id(agent_id)}…)")
        else:
            # Try to find best agent automatically
            print_info("No agent specified and no match on record — finding best agent…")
            matches = find_best_agents(conn, job_id, top_k=1)
            if not matches:
                print_error("No available agents found. Register agents first.")
                sys.exit(1)
            agent_id = matches[0]["agent_id"]
            print_info(f"Auto-selected: {matches[0]['name']} (score: {matches[0]['score']:.4f})")

        try:
            result = run_execution(conn, job_id, agent_id)
            print_success(f"Execution complete! ID: {result['execution_id']}")
            print_info(f"  Short ID: {short_id(result['execution_id'])}")
            snippet = result["output_text"][:400]
            if HAS_RICH:
                console.print(Panel(snippet + "…", title="[bold]Output Preview[/bold]", border_style="blue"))
            else:
                print(f"\n  Output preview:\n{snippet}\n")
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)


def cmd_verify(args: argparse.Namespace) -> None:
    manual: Optional[bool] = None
    if getattr(args, "approve", False):
        manual = True
    elif getattr(args, "reject", False):
        manual = False
    notes = getattr(args, "reason", "") or ""

    with get_connection() as conn:
        # Resolve job ID to find execution
        job_id_raw = getattr(args, "job_id", None)
        if not job_id_raw:
            print_error("--job-id is required for verify command.")
            sys.exit(1)

        try:
            job_id = resolve_id(conn, "jobs", job_id_raw)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)

        execution = db_get_execution_by_job(conn, job_id)
        if execution is None:
            print_error(f"No execution found for job '{job_id_raw}'. Execute the job first.")
            sys.exit(1)

        exec_id = execution["id"]

        try:
            result = verify_execution(conn, exec_id, manual, notes)
            if result["passed"]:
                print_success(f"Verification PASSED  (score: {result['score']:.3f})")
            else:
                print_warning(f"Verification FAILED  (score: {result['score']:.3f})")
            print_info(f"  Notes: {result['notes']}")
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)


def cmd_pay(args: argparse.Namespace) -> None:
    pay_all = getattr(args, "all", False)

    with get_connection() as conn:
        if pay_all:
            # Pay all verified jobs
            verified_jobs = db_list_jobs(conn, status="verified")
            if not verified_jobs:
                print_info("No verified jobs awaiting payment.")
                return
            print_header(f"Processing payouts for {len(verified_jobs)} verified jobs")
            total_paid = 0.0
            for job in verified_jobs:
                try:
                    receipt = process_payment(conn, job["id"])
                    print_payout_receipt(receipt)
                    total_paid += receipt["bounty_usd"]
                except Exception as e:
                    print_error(f"Payment failed for job {short_id(job['id'])}…: {e}")
            print_success(f"Total paid out: {fmt_usd(total_paid)}")
            return

        # Single job payment
        job_id_raw = getattr(args, "job_id", None)
        if not job_id_raw:
            print_error("Provide --job-id or use --all to pay all verified jobs.")
            sys.exit(1)

        try:
            job_id = resolve_id(conn, "jobs", job_id_raw)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)

        try:
            receipt = process_payment(conn, job_id)
            print_payout_receipt(receipt)
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)


def cmd_status(_args: argparse.Namespace) -> None:
    with get_connection() as conn:
        try:
            stats = get_marketplace_stats(conn)
            print_stats(stats)
        except Exception as e:
            print_error(f"Could not retrieve stats. Is the database initialised? ({e})")
            print_info("Run 'tor2ga init' to initialise the database.")
            sys.exit(1)


def cmd_history(_args: argparse.Namespace) -> None:
    with get_connection() as conn:
        txns = db_list_transactions(conn)
        if not txns:
            print_info("No transactions found.")
            return
        print_transaction_table(txns)
        # Summary
        total = sum(tx["amount_usd"] for tx in txns if tx["tx_type"] == "payout_agent_owner")
        fees = sum(tx["amount_usd"] for tx in txns if tx["tx_type"] == "platform_fee")
        print_info(f"Total agent payouts: {fmt_usd(total)}  |  Platform fees: {fmt_usd(fees)}")


def cmd_leaderboard(_args: argparse.Namespace) -> None:
    with get_connection() as conn:
        agents = db_list_agents(conn)
        if not agents:
            print_info("No agents registered yet.")
            return

        # Calculate earnings for each agent
        leaderboard_data = []
        for a in agents:
            # Get earnings: sum of payouts to this agent's owner for jobs done by this agent
            row = conn.execute(
                """SELECT COALESCE(SUM(t.amount_usd), 0) as total
                   FROM transactions t
                   JOIN executions e ON e.job_id = t.job_id
                   WHERE e.agent_id = ? AND t.tx_type = 'payout_agent_owner' AND t.status = 'completed'""",
                (a["id"],),
            ).fetchone()
            earnings = row["total"] if row else 0.0

            owner = db_get_user(conn, a["owner_id"])
            owner_name = owner["username"] if owner else "unknown"

            leaderboard_data.append({
                "name": a["name"],
                "owner_name": owner_name,
                "reputation_score": a["reputation_score"],
                "jobs_completed": a["jobs_completed"],
                "avg_rating": a["avg_rating"],
                "earnings": earnings,
                "status": a["status"],
            })

        # Sort by reputation descending, then by jobs completed
        leaderboard_data.sort(
            key=lambda x: (x["reputation_score"], x["jobs_completed"], x["earnings"]),
            reverse=True,
        )
        print_leaderboard(leaderboard_data)


def cmd_reset(_args: argparse.Namespace) -> None:
    # Confirmation
    if sys.stdin.isatty():
        print_warning(f"This will DELETE all data in {DB_PATH}")
        response = input("  Are you sure? Type 'yes' to confirm: ")
        if response.strip().lower() != "yes":
            print_info("Reset cancelled.")
            return
    if DB_PATH.exists():
        DB_PATH.unlink()
        print_success(f"Database deleted: {DB_PATH}")
    cmd_init()
    print_success(f"Fresh database initialised at {DB_PATH}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tor2ga",
        description="tor2ga.ai — AI Agent Marketplace Core Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              tor2ga init
              tor2ga user create --username alice --email alice@co.io --role lister
              tor2ga user list
              tor2ga agent register --name BotAlpha --description "Research agent" \\
                  --capabilities "research,analysis,writing" --owner alice
              tor2ga agent list
              tor2ga agent heartbeat --id <AGENT_ID>
              tor2ga job post --title "Market study" --description "..." \\
                  --category research --skills "research,writing" --bounty 250 --lister alice
              tor2ga job list
              tor2ga job list --status open
              tor2ga job view --id <JOB_ID>
              tor2ga match --job-id <JOB_ID>
              tor2ga match --auto
              tor2ga execute --job-id <JOB_ID>
              tor2ga execute --job-id <JOB_ID> --agent-id <AGENT_ID>
              tor2ga verify --job-id <JOB_ID>
              tor2ga verify --job-id <JOB_ID> --approve
              tor2ga verify --job-id <JOB_ID> --reject --reason "Output incomplete"
              tor2ga pay --job-id <JOB_ID>
              tor2ga pay --all
              tor2ga status
              tor2ga history
              tor2ga leaderboard
              tor2ga demo
              tor2ga reset
            """
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialize the database")

    # user
    user_p = sub.add_parser("user", help="User management")
    user_sub = user_p.add_subparsers(dest="user_command", required=True)

    uc = user_sub.add_parser("create", help="Create a user")
    uc.add_argument("--username", required=True, help="Unique username")
    uc.add_argument("--email", required=True, help="Email address")
    uc.add_argument("--role", required=True, choices=["lister", "agent_owner", "both"],
                    help="User role")

    user_sub.add_parser("list", help="List all users")

    # job
    job_p = sub.add_parser("job", help="Job management")
    job_sub = job_p.add_subparsers(dest="job_command", required=True)

    jp = job_sub.add_parser("post", help="Post a new job")
    jp.add_argument("--title", required=True, help="Job title")
    jp.add_argument("--description", required=True, help="Job description")
    jp.add_argument("--category", required=True, help="Job category (research, code, analysis, etc.)")
    jp.add_argument("--skills", required=True, help="Comma-separated list of required skills")
    jp.add_argument("--bounty", required=True, type=float, help="Bounty in USD (must be positive)")
    jp.add_argument("--lister", required=True, help="Lister username or user ID")
    jp.add_argument("--priority", default="normal", choices=["low", "normal", "high", "urgent"],
                    help="Job priority level")

    jl = job_sub.add_parser("list", help="List jobs")
    jl.add_argument("--status", default=None,
                    help="Filter by status (open, matched, in_progress, submitted, verified, paid, disputed)")

    jv = job_sub.add_parser("view", help="View job details")
    jv.add_argument("--id", required=True, help="Job ID (full or partial prefix)")

    # agent
    agent_p = sub.add_parser("agent", help="Agent management")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)

    ar = agent_sub.add_parser("register", help="Register a new agent")
    ar.add_argument("--name", required=True, help="Agent name")
    ar.add_argument("--description", required=True, help="Agent description")
    ar.add_argument("--capabilities", required=True, help="Comma-separated list of capabilities")
    ar.add_argument("--owner", required=True, help="Owner username or user ID")

    agent_sub.add_parser("list", help="List all agents")

    ah = agent_sub.add_parser("heartbeat", help="Send agent heartbeat (sets status to idle)")
    ah.add_argument("--id", required=True, help="Agent ID (full or partial prefix)")

    # match
    mp = sub.add_parser("match", help="Find matching agents for a job")
    mp.add_argument("--job-id", dest="job_id", default=None,
                    help="Job ID to match (full or partial prefix)")
    mp.add_argument("--auto", action="store_true", default=False,
                    help="Auto-match ALL open jobs to best available agents")
    mp.add_argument("--top-k", type=int, default=5, dest="top_k",
                    help="Number of top matches to show")

    # execute
    ep = sub.add_parser("execute", help="Execute a job with an agent")
    ep.add_argument("--job-id", required=True, dest="job_id",
                    help="Job ID (full or partial prefix)")
    ep.add_argument("--agent-id", dest="agent_id", default=None,
                    help="Agent ID (full or partial prefix). If omitted, uses matched agent or auto-selects best.")

    # verify
    vp = sub.add_parser("verify", help="Verify a job execution")
    vp.add_argument("--job-id", required=True, dest="job_id",
                    help="Job ID whose execution to verify (full or partial prefix)")
    vg = vp.add_mutually_exclusive_group()
    vg.add_argument("--approve", action="store_true", help="Manually approve the execution")
    vg.add_argument("--reject", action="store_true", help="Manually reject the execution")
    vp.add_argument("--reason", default="", help="Reason for manual approval/rejection")

    # pay
    pp = sub.add_parser("pay", help="Process payment for a verified job")
    pp.add_argument("--job-id", dest="job_id", default=None,
                    help="Job ID (full or partial prefix)")
    pp.add_argument("--all", action="store_true", default=False,
                    help="Process payouts for all verified jobs")

    # status
    sub.add_parser("status", help="Show marketplace statistics")

    # history
    sub.add_parser("history", help="Show transaction history")

    # leaderboard
    sub.add_parser("leaderboard", help="Show agent leaderboard")

    # demo
    sub.add_parser("demo", help="Run full end-to-end demo")

    # reset
    sub.add_parser("reset", help="Wipe database and reinitialize (with confirmation)")

    return parser


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()

    # Handle no arguments gracefully
    if argv is None and len(sys.argv) < 2:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args(argv)

    command = args.command
    log_verbose(f"Command: {command}, args: {args}")

    try:
        if command == "init":
            cmd_init()
            print_success(f"Database initialised at {DB_PATH}")

        elif command == "user":
            if args.user_command == "create":
                cmd_user_create(args)
            elif args.user_command == "list":
                cmd_user_list(args)

        elif command == "job":
            if args.job_command == "post":
                cmd_job_post(args)
            elif args.job_command == "list":
                cmd_job_list(args)
            elif args.job_command == "view":
                cmd_job_view(args)

        elif command == "agent":
            if args.agent_command == "register":
                cmd_agent_register(args)
            elif args.agent_command == "list":
                cmd_agent_list(args)
            elif args.agent_command == "heartbeat":
                cmd_agent_heartbeat(args)

        elif command == "match":
            cmd_match(args)

        elif command == "execute":
            cmd_execute(args)

        elif command == "verify":
            cmd_verify(args)

        elif command == "pay":
            cmd_pay(args)

        elif command == "status":
            cmd_status(args)

        elif command == "history":
            cmd_history(args)

        elif command == "leaderboard":
            cmd_leaderboard(args)

        elif command == "demo":
            run_demo()

        elif command == "reset":
            cmd_reset(args)

        else:
            parser.print_help()

    except KeyboardInterrupt:
        print_info("\nInterrupted.")
        sys.exit(130)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            print_error(f"Database not initialised. Run 'tor2ga init' first.")
        else:
            print_error(f"Database error: {e}")
        sys.exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        if VERBOSE:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
