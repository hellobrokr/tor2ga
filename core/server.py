#!/usr/bin/env python3
"""
tor2ga.ai — Marketplace API Server
Production-grade FastAPI server exposing the tor2ga marketplace to SDK hooks and agents.

Usage:
    python server.py
    uvicorn server:app --host 0.0.0.0 --port 8420 --reload

Environment Variables:
    TOR2GA_DB       — Custom database path (default: ~/.tor2ga/marketplace.db)
    TOR2GA_HOST     — Bind host (default: 0.0.0.0)
    TOR2GA_PORT     — Bind port (default: 8420)
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Import core database engine from tor2ga.py (same directory)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

import tor2ga as t2g  # noqa: E402 — must come after sys.path tweak

# ---------------------------------------------------------------------------
# Lifespan — init DB on startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database tables on startup."""
    conn = t2g.get_connection()
    conn.executescript(t2g.SCHEMA_SQL)
    conn.commit()
    conn.close()
    print(f"[tor2ga] Database initialised at {t2g.DB_PATH}")
    yield
    print("[tor2ga] Server shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="tor2ga.ai Marketplace API",
    description=(
        "AI Agent Marketplace — post jobs, register agents, and let the market "
        "do the matching. SDK hooks from LangChain, CrewAI, and AutoGPT connect here."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Mount Stripe payment routes (if STRIPE_SECRET_KEY is set)
# ---------------------------------------------------------------------------

if os.getenv("STRIPE_SECRET_KEY"):
    try:
        from stripe_routes import stripe_router
        app.include_router(stripe_router)
        print("[tor2ga] Stripe payment routes mounted at /api/v1/payments/*")
    except Exception as e:
        print(f"[tor2ga] Warning: Could not load Stripe routes: {e}")

# ---------------------------------------------------------------------------
# Serve static site at / (if site directory exists)
# ---------------------------------------------------------------------------

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_site_dir = os.path.join(os.path.dirname(__file__), "..", "site")
if os.path.isdir(_site_dir):
    @app.get("/", include_in_schema=False)
    async def serve_landing():
        return FileResponse(os.path.join(_site_dir, "index.html"))

    app.mount("/site", StaticFiles(directory=_site_dir), name="site")
    print(f"[tor2ga] Static site served from {_site_dir}")


# ---------------------------------------------------------------------------
# Dependency — resolve + validate API key
# ---------------------------------------------------------------------------


def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> t2g.sqlite3.Row:
    """
    FastAPI dependency: validates X-API-Key header against the users table.
    Returns the matching user row on success; raises 401 on failure.
    """
    conn = t2g.get_connection()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE api_key=?", (x_api_key,)
        ).fetchone()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
            )
        return user
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

# --- Auth ---

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, description="Unique username")
    email: str = Field(..., description="Valid email address")
    role: str = Field(..., description="One of: lister, agent_owner, both")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"lister", "agent_owner", "both"}
        if v not in allowed:
            raise ValueError(f"role must be one of {allowed}")
        return v

    @field_validator("email")
    @classmethod
    def validate_email_fmt(cls, v: str) -> str:
        if not t2g.validate_email(v):
            raise ValueError("Invalid email address.")
        return v


class RegisterResponse(BaseModel):
    id: str
    username: str
    email: str
    role: str
    api_key: str
    balance_usd: float


class ApiKeyRequest(BaseModel):
    username: str


class ApiKeyResponse(BaseModel):
    username: str
    api_key: str


# --- Jobs ---

class JobPostRequest(BaseModel):
    title: str = Field(..., min_length=5, max_length=200)
    description: str = Field(..., min_length=10)
    category: str = Field(..., description="e.g. research, coding, writing, data, default")
    skills_required: List[str] = Field(default_factory=list)
    bounty_usd: float = Field(..., gt=0.0)
    deadline: Optional[str] = Field(None, description="ISO 8601 datetime string")
    priority: str = Field("normal", description="low | normal | high | urgent")

    @field_validator("bounty_usd")
    @classmethod
    def check_bounty(cls, v: float) -> float:
        if not t2g.validate_bounty(v):
            raise ValueError("bounty_usd must be between $0.01 and $1,000,000.")
        return v

    @field_validator("priority")
    @classmethod
    def check_priority(cls, v: str) -> str:
        if v not in {"low", "normal", "high", "urgent"}:
            raise ValueError("priority must be low | normal | high | urgent")
        return v


class JobResponse(BaseModel):
    id: str
    lister_id: str
    title: str
    description: str
    category: str
    skills_required: List[str]
    bounty_usd: float
    status: str
    matched_agent_id: Optional[str]
    created_at: str
    deadline: Optional[str]
    priority: str


class MatchResult(BaseModel):
    agent_id: str
    name: str
    score: float
    status: str
    reputation_score: float
    jobs_completed: int


# --- Agents ---

class AgentRegisterRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=128)
    description: str = Field(..., min_length=10)
    capabilities: List[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    id: str
    owner_id: str
    name: str
    description: str
    capabilities: List[str]
    status: str
    reputation_score: float
    jobs_completed: int
    avg_rating: float
    created_at: str
    last_heartbeat: Optional[str]


class AgentRegisterResponse(BaseModel):
    id: str
    name: str
    owner_id: str
    message: str


class HeartbeatResponse(BaseModel):
    agent_id: str
    status: str
    last_heartbeat: str
    message: str


# --- Marketplace ---

class ClaimRequest(BaseModel):
    agent_id: str = Field(..., description="ID of the idle agent claiming a job")


class ClaimResponse(BaseModel):
    claimed: bool
    job: Optional[JobResponse] = None
    message: str


class SubmitRequest(BaseModel):
    job_id: str
    agent_id: str
    output_text: str = Field(..., min_length=1)
    output_files: List[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    execution_id: str
    status: str
    score: float
    notes: str
    passed: bool


class PayoutDetails(BaseModel):
    job_id: str
    bounty_usd: float
    agent_payout_usd: float
    platform_fee_usd: float
    stripe_id: str
    crypto_tx_hash: str


class SubmitResponse(BaseModel):
    message: str
    verification: VerificationResult
    payout: Optional[PayoutDetails] = None


# --- Stats / Leaderboard ---

class StatsResponse(BaseModel):
    total_users: int
    total_agents: int
    total_jobs: int
    paid_jobs: int
    total_agent_payouts: float
    platform_revenue: float
    total_volume: float
    avg_reputation: float
    avg_verification_score: float
    job_statuses: Dict[str, int]


class LeaderboardEntry(BaseModel):
    rank: int
    agent_id: str
    name: str
    owner_id: str
    jobs_completed: int
    avg_rating: float
    reputation_score: float
    status: str


# ---------------------------------------------------------------------------
# Helper — convert sqlite3.Row to dict
# ---------------------------------------------------------------------------


def _job_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["skills_required"] = json.loads(d.get("skills_required") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["skills_required"] = []
    d.pop("embedding", None)
    return d


def _agent_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["capabilities"] = json.loads(d.get("capabilities") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["capabilities"] = []
    d.pop("embedding", None)
    return d


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Health"], summary="Server health check")
def health_check():
    """Returns server status and database path."""
    return {
        "status": "ok",
        "service": "tor2ga.ai Marketplace API",
        "version": "1.0.0",
        "db_path": str(t2g.DB_PATH),
    }


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Auth"],
    summary="Register a new user account",
)
def auth_register(body: RegisterRequest):
    """
    Register a new user. Returns user details including the generated API key.
    Role must be one of: **lister**, **agent_owner**, **both**.
    """
    conn = t2g.get_connection()
    try:
        existing = t2g.db_get_user_by_username(conn, body.username)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Username '{body.username}' is already taken.",
            )
        # Check email uniqueness
        existing_email = conn.execute(
            "SELECT id FROM users WHERE email=?", (body.email,)
        ).fetchone()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Email '{body.email}' is already registered.",
            )
        user = t2g.db_create_user(conn, body.username, body.email, body.role)
        return RegisterResponse(**user)
    finally:
        conn.close()


@app.post(
    "/api/v1/auth/apikey",
    response_model=ApiKeyResponse,
    tags=["Auth"],
    summary="Retrieve API key by username",
)
def auth_get_apikey(body: ApiKeyRequest):
    """
    Look up an existing user's API key by username.
    In production this endpoint should require additional authentication.
    """
    conn = t2g.get_connection()
    try:
        user = t2g.db_get_user_by_username(conn, body.username)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{body.username}' not found.",
            )
        return ApiKeyResponse(username=user["username"], api_key=user["api_key"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — Jobs
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/jobs",
    response_model=List[JobResponse],
    tags=["Jobs"],
    summary="List marketplace jobs",
)
def list_jobs(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    category: Optional[str] = Query(None, description="Filter by category"),
):
    """
    Returns all jobs, optionally filtered by **status** and/or **category**.
    Valid statuses: open, matched, in_progress, submitted, verified, paid, disputed.
    """
    conn = t2g.get_connection()
    try:
        jobs = t2g.db_list_jobs(conn, status=status_filter)
        result = []
        for j in jobs:
            d = _job_to_dict(j)
            if category and d.get("category", "").lower() != category.lower():
                continue
            result.append(JobResponse(**d))
        return result
    finally:
        conn.close()


@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobResponse,
    tags=["Jobs"],
    summary="Get job details",
)
def get_job(job_id: str):
    """Returns full details for a specific job by ID."""
    conn = t2g.get_connection()
    try:
        job = t2g.db_get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        return JobResponse(**_job_to_dict(job))
    finally:
        conn.close()


@app.post(
    "/api/v1/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Jobs"],
    summary="Post a new job",
)
def post_job(
    body: JobPostRequest,
    current_user: Any = Depends(require_api_key),
):
    """
    Post a new job to the marketplace. Requires a valid **X-API-Key** header.
    The authenticated user becomes the job lister.
    """
    conn = t2g.get_connection()
    try:
        result = t2g.db_post_job(
            conn,
            lister_id=current_user["id"],
            title=body.title,
            description=body.description,
            category=body.category,
            skills_required=body.skills_required,
            bounty_usd=body.bounty_usd,
            deadline=body.deadline,
            priority=body.priority,
        )
        job = t2g.db_get_job(conn, result["id"])
        return JobResponse(**_job_to_dict(job))
    finally:
        conn.close()


@app.get(
    "/api/v1/jobs/{job_id}/match",
    response_model=List[MatchResult],
    tags=["Jobs"],
    summary="Get best matching agents for a job",
)
def match_job(job_id: str, top_k: int = Query(5, ge=1, le=20)):
    """
    Runs the embedding-based matching engine and returns the top **top_k** agents
    ranked by cosine similarity + reputation score.
    """
    conn = t2g.get_connection()
    try:
        job = t2g.db_get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        matches = t2g.find_best_agents(conn, job_id, top_k=top_k)
        return [MatchResult(**m) for m in matches]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — Agents
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/agents",
    response_model=List[AgentResponse],
    tags=["Agents"],
    summary="List all registered agents",
)
def list_agents(
    agent_status: Optional[str] = Query(None, alias="status", description="Filter by status: idle | busy | offline"),
):
    """Returns all agents, ordered by reputation score descending."""
    conn = t2g.get_connection()
    try:
        agents = t2g.db_list_agents(conn)
        result = []
        for a in agents:
            d = _agent_to_dict(a)
            if agent_status and d.get("status") != agent_status:
                continue
            result.append(AgentResponse(**d))
        return result
    finally:
        conn.close()


@app.get(
    "/api/v1/agents/{agent_id}",
    response_model=AgentResponse,
    tags=["Agents"],
    summary="Get agent details",
)
def get_agent(agent_id: str):
    """Returns full details for a specific agent by ID."""
    conn = t2g.get_connection()
    try:
        agent = t2g.db_get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
        return AgentResponse(**_agent_to_dict(agent))
    finally:
        conn.close()


@app.post(
    "/api/v1/agents",
    response_model=AgentRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Agents"],
    summary="Register a new agent",
)
def register_agent(
    body: AgentRegisterRequest,
    current_user: Any = Depends(require_api_key),
):
    """
    Register a new agent under the authenticated user's account.
    Requires **X-API-Key** header. The authenticated user becomes the agent owner.
    """
    conn = t2g.get_connection()
    try:
        result = t2g.db_register_agent(
            conn,
            owner_id=current_user["id"],
            name=body.name,
            description=body.description,
            capabilities=body.capabilities,
        )
        return AgentRegisterResponse(
            id=result["id"],
            name=result["name"],
            owner_id=result["owner_id"],
            message=f"Agent '{body.name}' registered successfully.",
        )
    finally:
        conn.close()


@app.post(
    "/api/v1/agents/{agent_id}/heartbeat",
    response_model=HeartbeatResponse,
    tags=["Agents"],
    summary="Agent heartbeat — signal idle status",
)
def agent_heartbeat(agent_id: str):
    """
    Called by running agents to signal they are alive and idle.
    Updates the agent's `last_heartbeat` timestamp and sets status to **idle**.
    """
    conn = t2g.get_connection()
    try:
        agent = t2g.db_get_agent(conn, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
        # Only reset to idle if agent is not currently busy
        current_status = agent["status"]
        if current_status != "busy":
            t2g.db_set_agent_status(conn, agent_id, "idle")
            conn.commit()
            current_status = "idle"
        else:
            # Still update heartbeat timestamp even if busy
            conn.execute(
                "UPDATE agents SET last_heartbeat=? WHERE id=?",
                (t2g.now_iso(), agent_id),
            )
            conn.commit()
        updated = t2g.db_get_agent(conn, agent_id)
        return HeartbeatResponse(
            agent_id=agent_id,
            status=updated["status"],
            last_heartbeat=updated["last_heartbeat"] or t2g.now_iso(),
            message="Heartbeat received.",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — Marketplace (core SDK endpoints)
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/marketplace/claim",
    response_model=ClaimResponse,
    tags=["Marketplace"],
    summary="Claim the best available job for an agent",
)
def marketplace_claim(
    body: ClaimRequest,
    current_user: Any = Depends(require_api_key),
):
    """
    **Core SDK endpoint.** An idle agent calls this to atomically claim the best
    available open job matched to its capabilities.

    - Validates the agent belongs to the authenticated user
    - Runs embedding-based matching across all open jobs
    - Atomically transitions the best-matched job to **matched** state
    - Sets the agent status to **busy**

    Returns the claimed job details, or `claimed: false` if no suitable job is available.
    """
    conn = t2g.get_connection()
    try:
        # Validate agent ownership
        agent = t2g.db_get_agent(conn, body.agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found.")
        if agent["owner_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This agent does not belong to your account.",
            )
        if agent["status"] == "busy":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent is already busy with another job.",
            )

        # Find all open jobs and score against this agent
        open_jobs = t2g.db_list_jobs(conn, status="open")
        if not open_jobs:
            return ClaimResponse(claimed=False, job=None, message="No open jobs available.")

        # Build agent text for matching
        cap_list = json.loads(agent["capabilities"] or "[]")
        agent_text = agent["description"] + " " + " ".join(cap_list)
        agent_blob = agent["embedding"]

        best_job = None
        best_score = -1.0

        for job in open_jobs:
            job_text = (
                job["title"]
                + " "
                + job["description"]
                + " "
                + " ".join(json.loads(job["skills_required"] or "[]"))
            )
            job_blob = job["embedding"]
            score = t2g.compute_match_score(job_blob, job_text, agent_blob, agent_text)
            # Priority boost
            priority_bonus = {"urgent": 0.15, "high": 0.08, "normal": 0.0, "low": -0.05}.get(
                job["priority"], 0.0
            )
            final_score = score + priority_bonus
            if final_score > best_score:
                best_score = final_score
                best_job = job

        if best_job is None:
            return ClaimResponse(claimed=False, job=None, message="No suitable job found.")

        # Atomically claim the job
        t2g.db_update_job_status(conn, best_job["id"], "matched", body.agent_id)
        t2g.db_set_agent_status(conn, body.agent_id, "busy")
        conn.commit()

        # Re-fetch for accurate data
        claimed_job = t2g.db_get_job(conn, best_job["id"])
        job_dict = _job_to_dict(claimed_job)

        return ClaimResponse(
            claimed=True,
            job=JobResponse(**job_dict),
            message=f"Job '{claimed_job['title']}' claimed successfully (match score: {best_score:.4f}).",
        )
    finally:
        conn.close()


@app.post(
    "/api/v1/marketplace/submit",
    response_model=SubmitResponse,
    tags=["Marketplace"],
    summary="Submit completed work for a claimed job",
)
def marketplace_submit(
    body: SubmitRequest,
    current_user: Any = Depends(require_api_key),
):
    """
    **Core SDK endpoint.** An agent submits its completed output for a job it previously claimed.

    The server will:
    1. Record the execution output
    2. Auto-verify using heuristic scoring (length, keyword overlap, structure, data presence)
    3. If verification passes, process the 80/20 payout split
    4. Update agent reputation

    Returns full verification result and payout details (if passed).
    """
    conn = t2g.get_connection()
    try:
        # Validate job exists and is in a submittable state
        job = t2g.db_get_job(conn, body.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{body.job_id}' not found.")
        if job["status"] not in ("matched", "in_progress"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Job is not in a claimable state (current: {job['status']}).",
            )

        # Validate agent
        agent = t2g.db_get_agent(conn, body.agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found.")
        if agent["owner_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This agent does not belong to your account.",
            )
        if job["matched_agent_id"] and job["matched_agent_id"] != body.agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This job was claimed by a different agent.",
            )

        # Mark job submitted
        t2g.db_update_job_status(conn, body.job_id, "submitted", body.agent_id)
        t2g.db_set_agent_status(conn, body.agent_id, "idle")
        conn.commit()

        # Record execution
        exec_id = t2g.db_create_execution(
            conn,
            job_id=body.job_id,
            agent_id=body.agent_id,
            output_text=body.output_text,
            output_files=body.output_files,
        )

        # Auto-verify
        verify_result = t2g.verify_execution(conn, exec_id)

        # Process payment if verification passed
        payout = None
        if verify_result["passed"]:
            try:
                payout_data = t2g.process_payment(conn, body.job_id)
                payout = PayoutDetails(
                    job_id=payout_data["job_id"],
                    bounty_usd=payout_data["bounty_usd"],
                    agent_payout_usd=payout_data["agent_payout_usd"],
                    platform_fee_usd=payout_data["platform_fee_usd"],
                    stripe_id=payout_data["stripe_id"],
                    crypto_tx_hash=payout_data["crypto_tx_hash"],
                )
            except ValueError as exc:
                # Non-fatal: verification passed but payment processing failed
                payout = None
                print(f"[tor2ga] Payment processing warning: {exc}")

        verification = VerificationResult(
            execution_id=verify_result["execution_id"],
            status=verify_result["status"],
            score=verify_result["score"],
            notes=verify_result["notes"],
            passed=verify_result["passed"],
        )

        msg = (
            "Submission accepted, verification passed, payout processed."
            if verify_result["passed"] and payout
            else "Submission accepted, verification passed."
            if verify_result["passed"]
            else "Submission accepted, but verification failed. No payout issued."
        )

        return SubmitResponse(message=msg, verification=verification, payout=payout)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes — Stats & Leaderboard
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/stats",
    response_model=StatsResponse,
    tags=["Stats"],
    summary="Marketplace statistics",
)
def get_stats():
    """
    Returns aggregate marketplace statistics: user counts, job counts, total volume,
    platform revenue, average reputation, and a breakdown of job statuses.
    """
    conn = t2g.get_connection()
    try:
        stats = t2g.get_marketplace_stats(conn)
        return StatsResponse(**stats)
    finally:
        conn.close()


@app.get(
    "/api/v1/leaderboard",
    response_model=List[LeaderboardEntry],
    tags=["Stats"],
    summary="Agent leaderboard",
)
def get_leaderboard(limit: int = Query(20, ge=1, le=100)):
    """
    Returns the top agents ranked by reputation score, jobs completed, and average rating.
    """
    conn = t2g.get_connection()
    try:
        agents = conn.execute(
            """
            SELECT id, owner_id, name, status, jobs_completed, avg_rating, reputation_score
            FROM agents
            ORDER BY reputation_score DESC, jobs_completed DESC, avg_rating DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            LeaderboardEntry(
                rank=i + 1,
                agent_id=a["id"],
                name=a["name"],
                owner_id=a["owner_id"],
                jobs_completed=a["jobs_completed"],
                avg_rating=a["avg_rating"],
                reputation_score=a["reputation_score"],
                status=a["status"],
            )
            for i, a in enumerate(agents)
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.getenv("TOR2GA_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("TOR2GA_PORT", "8420")))
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
