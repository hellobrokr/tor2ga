#!/usr/bin/env python3
"""
tor2ga.ai — Stripe Payment API Routes
FastAPI router exposing Stripe Connect, escrow, payout, and webhook endpoints.

Mount this router into server.py:

    from stripe_routes import stripe_router
    app.include_router(stripe_router)

All endpoints are prefixed at /api/v1/payments.

Endpoints:
    POST   /api/v1/payments/connect/create           — Create a Connect account
    GET    /api/v1/payments/connect/status/{id}      — Check onboarding status
    POST   /api/v1/payments/escrow/create             — Create escrow for a job
    POST   /api/v1/payments/escrow/capture/{job_id}  — Capture escrow after verify
    POST   /api/v1/payments/escrow/cancel/{job_id}   — Cancel escrow (refund)
    POST   /api/v1/payments/payout/{job_id}          — Process 80/20 payout
    POST   /api/v1/payments/webhook                  — Stripe webhook receiver
    GET    /api/v1/payments/balance                   — Platform Stripe balance

Environment variables consumed (same as stripe_payments.py):
    STRIPE_SECRET_KEY
    STRIPE_PLATFORM_ACCOUNT_ID
    STRIPE_WEBHOOK_SECRET
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
try:
    from stripe.error import (
        CardError,
        InvalidRequestError,
        SignatureVerificationError,
        StripeError,
    )
except ImportError:
    from stripe import (
        CardError,
        InvalidRequestError,
        SignatureVerificationError,
        StripeError,
    )

import sys
sys.path.insert(0, os.path.dirname(__file__))

import tor2ga as t2g                    # noqa: E402
import stripe_payments as sp            # noqa: E402
from stripe_payments import StripePayments  # noqa: E402

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log = logging.getLogger("tor2ga.routes.stripe")

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

stripe_router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])

# ---------------------------------------------------------------------------
# Processor dependency
# ---------------------------------------------------------------------------


def get_stripe() -> StripePayments:
    """
    FastAPI dependency that returns the initialised StripePayments processor.

    Raises HTTP 503 if Stripe credentials are not configured.
    """
    try:
        return sp.get_processor()
    except RuntimeError:
        # Attempt lazy init from env vars (convenient for test environments)
        try:
            return sp.initialise_processor()
        except (EnvironmentError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Stripe payment processor not configured: {exc}. "
                    "Set STRIPE_SECRET_KEY and STRIPE_PLATFORM_ACCOUNT_ID."
                ),
            )


# ---------------------------------------------------------------------------
# Auth dependency (re-use from server.py patterns)
# ---------------------------------------------------------------------------


def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> Any:
    """Validate X-API-Key header against the users table."""
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


class CreateConnectAccountRequest(BaseModel):
    """Request body for creating a Stripe Connect Express account."""
    email: str = Field(..., description="Email address of the agent owner.")
    user_id: str = Field(..., description="tor2ga user ID of the agent owner.")


class CreateConnectAccountResponse(BaseModel):
    account_id: str
    onboarding_url: str
    charges_enabled: bool
    payouts_enabled: bool


class ConnectAccountStatusResponse(BaseModel):
    account_id: str
    charges_enabled: bool
    payouts_enabled: bool
    details_submitted: bool
    requirements_due: List[str]


class CreateEscrowRequest(BaseModel):
    """Request body for creating an escrow PaymentIntent for a job."""
    job_id: str = Field(..., description="tor2ga job UUID.")
    amount_usd: float = Field(..., gt=0, description="Full bounty amount in USD.")
    lister_email: str = Field(..., description="Lister's email for the Stripe receipt.")

    @field_validator("amount_usd")
    @classmethod
    def check_amount(cls, v: float) -> float:
        if v < 0.50:
            raise ValueError("amount_usd must be at least $0.50.")
        return v


class CreateEscrowResponse(BaseModel):
    payment_intent_id: str
    client_secret: str
    status: str
    amount_usd: float
    amount_cents: int


class CaptureEscrowResponse(BaseModel):
    payment_intent_id: str
    status: str
    amount_captured: float


class CancelEscrowResponse(BaseModel):
    payment_intent_id: str
    status: str
    refund_id: Optional[str]


class ProcessPayoutRequest(BaseModel):
    """Request body for triggering the 80/20 payout split after verification."""
    payment_intent_id: str = Field(
        ..., description="The captured PaymentIntent ID stored on the job."
    )
    bounty_usd: float = Field(..., gt=0, description="Full bounty amount in USD.")
    agent_owner_connect_id: str = Field(
        ..., description="Agent owner's Stripe Connect account ID (acct_...)."
    )


class ProcessPayoutResponse(BaseModel):
    transfer_id: str
    agent_amount: float
    platform_fee: float
    status: str
    connect_id: str


class WebhookResponse(BaseModel):
    event_id: str
    event_type: str
    status: str
    data: Dict[str, Any]


class PlatformBalanceResponse(BaseModel):
    available_usd: float
    pending_usd: float
    test_mode: bool


# ---------------------------------------------------------------------------
# Helper — surface Stripe errors as HTTP responses
# ---------------------------------------------------------------------------


def _stripe_http_error(exc: StripeError) -> HTTPException:
    """Map Stripe exception types to appropriate HTTP status codes."""
    if isinstance(exc, CardError):
        return HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Card declined: {exc.user_message}",
        )
    if isinstance(exc, InvalidRequestError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid Stripe request: {exc.user_message or str(exc)}",
        )
    if isinstance(exc, SignatureVerificationError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook signature verification failed.",
        )
    # Generic Stripe error — treat as 502 (upstream failure)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Stripe API error: {str(exc)}",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/payments/connect/create
# ---------------------------------------------------------------------------


@stripe_router.post(
    "/connect/create",
    response_model=CreateConnectAccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Stripe Connect Express account for an agent owner",
    description=(
        "Creates a Stripe Connect Express account linked to the given user. "
        "Returns an onboarding URL that the agent owner must visit to complete "
        "KYC (identity verification and bank account setup) before payouts are enabled. "
        "Store the returned `account_id` in the users table as `stripe_connect_id`."
    ),
)
def create_connect_account(
    body: CreateConnectAccountRequest,
    current_user: Any = Depends(require_api_key),
    stripe_proc: StripePayments = Depends(get_stripe),
) -> CreateConnectAccountResponse:
    """
    Create a Stripe Connect Express account for an agent owner.

    Requires **X-API-Key** header. The authenticated user must be the agent owner.
    """
    log.info(
        "POST /connect/create: user=%s email=%s",
        current_user["id"],
        body.email,
    )

    # Validate the user_id in the body matches the authenticated user
    if body.user_id != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id in request body must match the authenticated user.",
        )

    try:
        result = stripe_proc.create_connect_account(
            user_email=body.email,
            user_id=body.user_id,
        )
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    # Persist the stripe_connect_id to the users table
    conn = t2g.get_connection()
    try:
        # Add stripe_connect_id column if it doesn't exist yet (idempotent)
        conn.execute(
            "ALTER TABLE users ADD COLUMN stripe_connect_id TEXT"
        )
        conn.commit()
    except Exception:
        # Column already exists — that's fine
        pass
    try:
        conn.execute(
            "UPDATE users SET stripe_connect_id=? WHERE id=?",
            (result["account_id"], current_user["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return CreateConnectAccountResponse(**result)


# ---------------------------------------------------------------------------
# GET /api/v1/payments/connect/status/{account_id}
# ---------------------------------------------------------------------------


@stripe_router.get(
    "/connect/status/{account_id}",
    response_model=ConnectAccountStatusResponse,
    summary="Check Stripe Connect account onboarding status",
    description=(
        "Returns the current onboarding status for a Stripe Connect account. "
        "Poll this after the user returns from the Stripe onboarding URL to "
        "confirm that `payouts_enabled` is `true` before allowing job claims."
    ),
)
def get_connect_status(
    account_id: str,
    stripe_proc: StripePayments = Depends(get_stripe),
) -> ConnectAccountStatusResponse:
    """Check whether a Connect account has completed onboarding."""
    log.info("GET /connect/status/%s", account_id)

    try:
        result = stripe_proc.check_account_status(account_id)
    except InvalidRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Stripe account '{account_id}' not found.",
        ) from exc
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    return ConnectAccountStatusResponse(**result)


# ---------------------------------------------------------------------------
# POST /api/v1/payments/escrow/create
# ---------------------------------------------------------------------------


@stripe_router.post(
    "/escrow/create",
    response_model=CreateEscrowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Stripe escrow (PaymentIntent) for a job bounty",
    description=(
        "Creates a Stripe PaymentIntent with `capture_method=manual` for the "
        "full job bounty. The lister's card is authorised but NOT immediately "
        "charged — funds are held in escrow. "
        "The frontend must use the returned `client_secret` with Stripe.js "
        "`stripe.confirmCardPayment()` to complete the payment flow. "
        "Store `payment_intent_id` on the job record for later capture or cancellation."
    ),
)
def create_escrow(
    body: CreateEscrowRequest,
    current_user: Any = Depends(require_api_key),
    stripe_proc: StripePayments = Depends(get_stripe),
) -> CreateEscrowResponse:
    """
    Create an escrow PaymentIntent for a job. Requires **X-API-Key** header.
    """
    log.info(
        "POST /escrow/create: job=%s amount=$%.2f user=%s",
        body.job_id,
        body.amount_usd,
        current_user["id"],
    )

    # Verify the job exists and belongs to the authenticated lister
    conn = t2g.get_connection()
    try:
        job = t2g.db_get_job(conn, body.job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job '{body.job_id}' not found.",
            )
        if job["lister_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not the lister for this job.",
            )
    finally:
        conn.close()

    try:
        result = stripe_proc.create_escrow(
            job_id=body.job_id,
            amount_usd=body.amount_usd,
            lister_email=body.lister_email,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    # Persist the payment_intent_id on the job row for later capture/cancel
    conn = t2g.get_connection()
    try:
        try:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN stripe_payment_intent_id TEXT"
            )
            conn.commit()
        except Exception:
            pass  # Column already exists
        conn.execute(
            "UPDATE jobs SET stripe_payment_intent_id=? WHERE id=?",
            (result["payment_intent_id"], body.job_id),
        )
        conn.commit()
    finally:
        conn.close()

    return CreateEscrowResponse(**result)


# ---------------------------------------------------------------------------
# POST /api/v1/payments/escrow/capture/{job_id}
# ---------------------------------------------------------------------------


@stripe_router.post(
    "/escrow/capture/{job_id}",
    response_model=CaptureEscrowResponse,
    summary="Capture escrow after verification passes",
    description=(
        "Finalises the charge on the lister's card after the job output has been "
        "verified. Must be called before the 7-day manual-capture window expires. "
        "After capture, immediately call `POST /api/v1/payments/payout/{job_id}` "
        "to transfer the agent's 80% share."
    ),
)
def capture_escrow(
    job_id: str,
    current_user: Any = Depends(require_api_key),
    stripe_proc: StripePayments = Depends(get_stripe),
) -> CaptureEscrowResponse:
    """Capture held escrow for a verified job. Requires **X-API-Key** header."""
    log.info("POST /escrow/capture/%s user=%s", job_id, current_user["id"])

    pi_id = _get_pi_for_job(job_id, current_user["id"], require_lister=True)

    try:
        result = stripe_proc.capture_escrow(pi_id)
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    return CaptureEscrowResponse(**result)


# ---------------------------------------------------------------------------
# POST /api/v1/payments/escrow/cancel/{job_id}
# ---------------------------------------------------------------------------


@stripe_router.post(
    "/escrow/cancel/{job_id}",
    response_model=CancelEscrowResponse,
    summary="Cancel escrow and refund lister after verification fails",
    description=(
        "Releases the held authorisation (pre-capture) or issues a full refund "
        "(post-capture) back to the lister when verification fails or the job is "
        "disputed. No money leaves the lister's account if called before capture."
    ),
)
def cancel_escrow(
    job_id: str,
    current_user: Any = Depends(require_api_key),
    stripe_proc: StripePayments = Depends(get_stripe),
) -> CancelEscrowResponse:
    """Cancel/refund escrow for a failed job. Requires **X-API-Key** header."""
    log.info("POST /escrow/cancel/%s user=%s", job_id, current_user["id"])

    pi_id = _get_pi_for_job(job_id, current_user["id"], require_lister=True)

    try:
        result = stripe_proc.cancel_escrow(pi_id)
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    return CancelEscrowResponse(**result)


# ---------------------------------------------------------------------------
# POST /api/v1/payments/payout/{job_id}
# ---------------------------------------------------------------------------


@stripe_router.post(
    "/payout/{job_id}",
    response_model=ProcessPayoutResponse,
    summary="Process the 80/20 payout split for a completed job",
    description=(
        "Transfers 80% of the bounty to the agent owner's Stripe Connect account. "
        "The platform retains 20% automatically. Must only be called after "
        "`POST /escrow/capture/{job_id}` has succeeded and verification has passed. "
        "Idempotent — safe to retry; duplicate calls return the same transfer."
    ),
)
def process_payout(
    job_id: str,
    body: ProcessPayoutRequest,
    current_user: Any = Depends(require_api_key),
    stripe_proc: StripePayments = Depends(get_stripe),
) -> ProcessPayoutResponse:
    """
    Trigger 80/20 payout for a verified job. Requires **X-API-Key** header.
    The authenticated user must be the lister of the job.
    """
    log.info(
        "POST /payout/%s user=%s agent_connect=%s",
        job_id,
        current_user["id"],
        body.agent_owner_connect_id,
    )

    # Validate job ownership (only lister or platform admin can trigger payout)
    conn = t2g.get_connection()
    try:
        job = t2g.db_get_job(conn, job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job '{job_id}' not found.",
            )
        if job["lister_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the job lister can trigger payouts.",
            )
    finally:
        conn.close()

    try:
        result = stripe_proc.process_payout(
            job_id=job_id,
            payment_intent_id=body.payment_intent_id,
            bounty_usd=body.bounty_usd,
            agent_owner_connect_id=body.agent_owner_connect_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    # Mark job as paid in the DB
    conn = t2g.get_connection()
    try:
        t2g.db_update_job_status(conn, job_id, "paid", job["matched_agent_id"])
        conn.commit()
    finally:
        conn.close()

    return ProcessPayoutResponse(**result)


# ---------------------------------------------------------------------------
# POST /api/v1/payments/webhook
# ---------------------------------------------------------------------------


@stripe_router.post(
    "/webhook",
    response_model=WebhookResponse,
    summary="Stripe webhook receiver",
    description=(
        "Receives and verifies incoming Stripe webhook events. "
        "Register this URL in the Stripe Dashboard under "
        "Developers → Webhooks → Add endpoint: `https://api.tor2ga.ai/api/v1/payments/webhook`. "
        "Subscribe to: `payment_intent.succeeded`, `payment_intent.payment_failed`, "
        "`account.updated`, `transfer.created`. "
        "The `STRIPE_WEBHOOK_SECRET` environment variable (whsec_...) must be set."
    ),
)
async def stripe_webhook(
    request: Request,
    stripe_proc: StripePayments = Depends(get_stripe),
) -> WebhookResponse:
    """
    Process Stripe webhook events.

    **Do NOT authenticate this endpoint with X-API-Key** — Stripe does not send
    API keys. Authentication is handled internally via signature verification
    using `STRIPE_WEBHOOK_SECRET`.
    """
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        log.error("STRIPE_WEBHOOK_SECRET is not set — cannot verify webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret not configured. Set STRIPE_WEBHOOK_SECRET.",
        )

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe-Signature header.",
        )

    try:
        result = stripe_proc.handle_webhook(
            payload=payload,
            sig_header=sig_header,
            webhook_secret=webhook_secret,
        )
    except (ValueError, SignatureVerificationError) as exc:
        # 400 tells Stripe to stop retrying — only use for truly invalid events
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Webhook rejected: {str(exc)}",
        ) from exc
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    # Act on specific webhook events to update the DB
    _apply_webhook_side_effects(result)

    return WebhookResponse(**result)


def _apply_webhook_side_effects(webhook_result: Dict[str, Any]) -> None:
    """
    Apply database side-effects for handled webhook events.
    Called synchronously from the webhook endpoint.
    """
    event_type = webhook_result.get("event_type", "")
    data = webhook_result.get("data", {})

    if event_type == "payment_intent.succeeded":
        job_id = data.get("job_id", "")
        pi_id = data.get("payment_intent_id", "")
        if job_id:
            conn = t2g.get_connection()
            try:
                # Transition job from draft/pending to open once escrow is confirmed
                job = t2g.db_get_job(conn, job_id)
                if job and job["status"] == "draft":
                    t2g.db_update_job_status(conn, job_id, "open")
                    conn.commit()
                    log.info(
                        "Webhook: job=%s moved to open (escrow confirmed pi=%s)",
                        job_id,
                        pi_id,
                    )
            except Exception as exc:
                log.error("Webhook side-effect error for job=%s: %s", job_id, exc)
            finally:
                conn.close()

    elif event_type == "payment_intent.payment_failed":
        job_id = data.get("job_id", "")
        if job_id:
            conn = t2g.get_connection()
            try:
                job = t2g.db_get_job(conn, job_id)
                if job and job["status"] in ("draft", "open"):
                    t2g.db_update_job_status(conn, job_id, "cancelled")
                    conn.commit()
                    log.warning(
                        "Webhook: job=%s cancelled (payment failed)", job_id
                    )
            except Exception as exc:
                log.error(
                    "Webhook side-effect error (payment failed) for job=%s: %s",
                    job_id,
                    exc,
                )
            finally:
                conn.close()

    elif event_type == "account.updated":
        account_id = data.get("account_id", "")
        payouts_enabled = data.get("payouts_enabled", False)
        if account_id and payouts_enabled:
            conn = t2g.get_connection()
            try:
                # Mark user as stripe_onboarded in the DB
                try:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN stripe_onboarded INTEGER DEFAULT 0"
                    )
                    conn.commit()
                except Exception:
                    pass
                conn.execute(
                    "UPDATE users SET stripe_onboarded=1 WHERE stripe_connect_id=?",
                    (account_id,),
                )
                conn.commit()
                log.info(
                    "Webhook: agent onboarding complete for stripe account=%s",
                    account_id,
                )
            except Exception as exc:
                log.error(
                    "Webhook side-effect error (account.updated) account=%s: %s",
                    account_id,
                    exc,
                )
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# GET /api/v1/payments/balance
# ---------------------------------------------------------------------------


@stripe_router.get(
    "/balance",
    response_model=PlatformBalanceResponse,
    summary="Platform Stripe account balance",
    description=(
        "Returns the tor2ga platform Stripe account's available and pending balance. "
        "Available balance reflects funds that can be paid out immediately. "
        "Pending balance includes recently captured funds still in transit."
    ),
)
def get_platform_balance(
    current_user: Any = Depends(require_api_key),
    stripe_proc: StripePayments = Depends(get_stripe),
) -> PlatformBalanceResponse:
    """
    Fetch the platform Stripe balance. Requires **X-API-Key** header.
    In production, restrict this to admin users only.
    """
    log.info("GET /balance user=%s", current_user["id"])

    try:
        result = stripe_proc.get_platform_balance()
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc

    return PlatformBalanceResponse(**result)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _get_pi_for_job(
    job_id: str,
    user_id: str,
    require_lister: bool = True,
) -> str:
    """
    Look up the stripe_payment_intent_id for a job, validating ownership.

    Args:
        job_id:          The tor2ga job UUID.
        user_id:         The authenticated user's ID.
        require_lister:  If True, checks the user is the job lister.

    Returns:
        The PaymentIntent ID string.

    Raises:
        HTTPException 404: Job not found.
        HTTPException 403: User is not the lister.
        HTTPException 400: No PaymentIntent on the job.
    """
    conn = t2g.get_connection()
    try:
        job = t2g.db_get_job(conn, job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job '{job_id}' not found.",
            )
        if require_lister and job["lister_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the job lister can perform this payment action.",
            )

        # stripe_payment_intent_id may not exist on older rows
        pi_id = None
        try:
            pi_id = job["stripe_payment_intent_id"]
        except (KeyError, IndexError):
            pass

        if not pi_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"No Stripe PaymentIntent found for job '{job_id}'. "
                    "Create an escrow first via POST /api/v1/payments/escrow/create."
                ),
            )

        return pi_id
    finally:
        conn.close()
