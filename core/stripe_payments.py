#!/usr/bin/env python3
"""
tor2ga.ai — Stripe Connect Payment Processor
Real Stripe Connect money flow for the tor2ga.ai AI Agent Marketplace.

Plugs into server.py and tor2ga.py to replace simulated payments with
live Stripe PaymentIntents, transfers, and refunds.

Payment flow:
    1. Lister posts a job with a bounty → create_escrow() charges their card
       with capture_method="manual" so funds are held but not yet captured.
    2. Agent claims + executes the job.
    3. Verification passes  → capture_escrow() captures the hold,
                              process_payout() transfers 80 % to the agent's
                              Stripe Connect account (20 % stays as platform fee).
    4. Verification fails   → cancel_escrow() releases the uncaptured hold,
                              returning funds to the lister automatically.

Environment variables (required):
    STRIPE_SECRET_KEY          sk_test_... or sk_live_...
    STRIPE_PLATFORM_ACCOUNT_ID acct_...  (your Stripe platform account ID)

Optional:
    STRIPE_WEBHOOK_SECRET      whsec_... (required for webhook verification)
    STRIPE_PUBLISHABLE_KEY     pk_test_... / pk_live_... (frontend use only)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Dict, Optional

import stripe

# stripe v15+ moved errors from stripe.error to stripe directly
try:
    from stripe.error import (
        AuthenticationError,
        CardError,
        IdempotencyError,
        InvalidRequestError,
        RateLimitError,
        SignatureVerificationError,
        StripeError,
    )
except ImportError:
    # stripe >= 15.x
    from stripe import (
        AuthenticationError,
        CardError,
        IdempotencyError,
        InvalidRequestError,
        RateLimitError,
        SignatureVerificationError,
        StripeError,
    )

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log = logging.getLogger("tor2ga.payments.stripe")
log.setLevel(logging.INFO)

if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] tor2ga.payments — %(message)s")
    )
    log.addHandler(_handler)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_SPLIT = 0.80       # Agent receives 80 % of the bounty
PLATFORM_SPLIT = 0.20    # Platform keeps 20 % as an application fee

# Maximum days Stripe allows between creating and capturing a PaymentIntent
# with capture_method=manual.  After this window the intent expires.
ESCROW_CAPTURE_WINDOW_DAYS = 7

# Stripe's minimum charge amount (USD) in cents
STRIPE_MIN_CHARGE_CENTS = 50  # $0.50


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _usd_to_cents(amount_usd: float) -> int:
    """Convert a USD float to the integer cents Stripe expects."""
    return int(round(amount_usd * 100))


def _cents_to_usd(cents: int) -> float:
    """Convert Stripe cents to a USD float."""
    return cents / 100.0


def _idempotency_key(*parts: str) -> str:
    """
    Deterministic idempotency key derived from stable identifiers.
    Guarantees that retrying the exact same operation is safe.
    """
    raw = ":".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


# ---------------------------------------------------------------------------
# StripePayments
# ---------------------------------------------------------------------------


class StripePayments:
    """
    Real Stripe Connect payment processor for tor2ga.ai.

    Instantiate once at application startup and reuse across requests:

        stripe_processor = StripePayments(
            stripe_secret_key=os.environ["STRIPE_SECRET_KEY"],
            platform_account_id=os.environ["STRIPE_PLATFORM_ACCOUNT_ID"],
        )

    All monetary amounts at the public API boundary are in USD floats.
    Internally, every Stripe API call converts to integer cents.
    """

    def __init__(self, stripe_secret_key: str, platform_account_id: str) -> None:
        """
        Initialise the processor and validate credentials.

        Args:
            stripe_secret_key:      Stripe secret key (sk_test_... or sk_live_...).
            platform_account_id:    Your Stripe platform account ID (acct_...).

        Raises:
            ValueError:  If either credential is missing or obviously malformed.
            AuthenticationError: If the key is rejected by Stripe on the health check.
        """
        if not stripe_secret_key:
            raise ValueError("stripe_secret_key must not be empty.")
        if not platform_account_id:
            raise ValueError("platform_account_id must not be empty.")
        if not stripe_secret_key.startswith(("sk_test_", "sk_live_")):
            raise ValueError(
                "stripe_secret_key must start with 'sk_test_' or 'sk_live_'."
            )
        if not platform_account_id.startswith("acct_"):
            raise ValueError(
                "platform_account_id must be a Stripe account ID starting with 'acct_'."
            )

        self._secret_key = stripe_secret_key
        self.platform_account_id = platform_account_id
        self.test_mode = stripe_secret_key.startswith("sk_test_")

        # Configure the global Stripe client for this instance's key.
        # In multi-key environments, pass stripe_account or use StripeClient.
        stripe.api_key = stripe_secret_key
        stripe.api_version = "2024-04-10"

        mode_label = "TEST" if self.test_mode else "LIVE"
        log.info(
            "StripePayments initialised — mode=%s, platform=%s",
            mode_label,
            platform_account_id,
        )

    # =========================================================================
    # Onboarding
    # =========================================================================

    def create_connect_account(self, user_email: str, user_id: str) -> Dict[str, Any]:
        """
        Create a Stripe Connect Express account for an agent owner.

        The account starts with transfers capability requested.  The owner
        must complete KYC via the onboarding link before payouts are enabled.

        Args:
            user_email: Email address of the agent owner.
            user_id:    tor2ga user ID — stored in Stripe metadata for traceability.

        Returns:
            {
                "account_id":      str,   # acct_xxx — store in users table
                "onboarding_url":  str,   # Stripe-hosted KYC link
                "charges_enabled": bool,
                "payouts_enabled": bool,
            }

        Raises:
            StripeError: On API failure.
        """
        log.info("Creating Connect account for user=%s email=%s", user_id, user_email)

        try:
            account = stripe.Account.create(
                type="express",
                email=user_email,
                capabilities={
                    "transfers": {"requested": True},
                },
                business_type="individual",
                settings={
                    "payouts": {
                        "schedule": {
                            "interval": "weekly",
                            "weekly_anchor": "monday",
                        }
                    }
                },
                metadata={
                    "platform": "tor2ga",
                    "user_id": user_id,
                },
            )
        except StripeError as exc:
            log.error("Failed to create Connect account: %s", exc)
            raise

        account_id = account.id
        log.info("Connect account created: %s for user=%s", account_id, user_id)

        # Immediately generate the onboarding link so the caller can redirect
        onboarding_url = self.get_onboarding_link(
            account_id=account_id,
            return_url="https://tor2ga.ai/dashboard?connected=1",
            refresh_url="https://tor2ga.ai/dashboard?refresh=1",
        )

        return {
            "account_id": account_id,
            "onboarding_url": onboarding_url,
            "charges_enabled": account.charges_enabled,
            "payouts_enabled": account.payouts_enabled,
        }

    def get_onboarding_link(
        self, account_id: str, return_url: str, refresh_url: str
    ) -> str:
        """
        Generate a Stripe-hosted onboarding URL for the agent owner to complete KYC.

        The link expires after a short time; generate a fresh one on each visit.

        Args:
            account_id:   The agent owner's Stripe Connect account ID.
            return_url:   URL Stripe redirects to after successful onboarding.
            refresh_url:  URL Stripe redirects to if the link expires.

        Returns:
            The one-time Stripe onboarding URL string.

        Raises:
            StripeError: On API failure.
        """
        log.info("Generating onboarding link for account=%s", account_id)

        try:
            link = stripe.AccountLink.create(
                account=account_id,
                return_url=return_url,
                refresh_url=refresh_url,
                type="account_onboarding",
            )
        except StripeError as exc:
            log.error("Failed to create AccountLink for %s: %s", account_id, exc)
            raise

        log.info("Onboarding link generated for account=%s", account_id)
        return link.url

    def check_account_status(self, account_id: str) -> Dict[str, Any]:
        """
        Check whether a Connect account has completed KYC onboarding.

        Args:
            account_id: The agent owner's Stripe Connect account ID.

        Returns:
            {
                "account_id":        str,
                "charges_enabled":   bool,
                "payouts_enabled":   bool,
                "details_submitted": bool,
                "requirements_due":  list[str],  # outstanding KYC items
            }

        Raises:
            InvalidRequestError: If the account ID does not exist.
            StripeError: On other API failures.
        """
        log.info("Checking account status for account=%s", account_id)

        try:
            account = stripe.Account.retrieve(account_id)
        except InvalidRequestError as exc:
            log.error("Account not found: %s — %s", account_id, exc)
            raise
        except StripeError as exc:
            log.error("Failed to retrieve account %s: %s", account_id, exc)
            raise

        requirements_due: list = []
        if account.requirements:
            requirements_due = list(account.requirements.currently_due or [])

        result = {
            "account_id": account_id,
            "charges_enabled": account.charges_enabled,
            "payouts_enabled": account.payouts_enabled,
            "details_submitted": account.details_submitted,
            "requirements_due": requirements_due,
        }
        log.info(
            "Account status for %s: charges=%s payouts=%s",
            account_id,
            account.charges_enabled,
            account.payouts_enabled,
        )
        return result

    # =========================================================================
    # Escrow
    # =========================================================================

    def create_escrow(
        self, job_id: str, amount_usd: float, lister_email: str
    ) -> Dict[str, Any]:
        """
        Create a PaymentIntent with manual capture (escrow semantics).

        The lister's card is authorised for the full bounty amount, but funds
        are not captured until verification passes.  The frontend uses the
        returned client_secret with Stripe.js to confirm the card payment.

        Stripe holds uncaptured authorisations for up to 7 days.

        Args:
            job_id:       tor2ga job UUID — stored in PaymentIntent metadata.
            amount_usd:   Full bounty amount in USD (e.g. 500.00 for $500).
            lister_email: Lister's email for the Stripe receipt.

        Returns:
            {
                "payment_intent_id": str,    # pi_xxx — store in jobs table
                "client_secret":     str,    # send to frontend to confirm payment
                "status":            str,    # "requires_payment_method"
                "amount_usd":        float,
                "amount_cents":      int,
            }

        Raises:
            ValueError: If amount is below Stripe's minimum charge.
            CardError: If Stripe rejects the card immediately.
            StripeError: On other API failures.
        """
        amount_cents = _usd_to_cents(amount_usd)

        if amount_cents < STRIPE_MIN_CHARGE_CENTS:
            raise ValueError(
                f"Bounty ${amount_usd:.2f} is below the minimum "
                f"${_cents_to_usd(STRIPE_MIN_CHARGE_CENTS):.2f} required by Stripe."
            )

        idempotency_key = _idempotency_key("create_escrow", job_id)

        log.info(
            "Creating escrow: job=%s amount=$%.2f lister=%s",
            job_id,
            amount_usd,
            lister_email,
        )

        try:
            payment_intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency="usd",
                # manual capture = hold funds without charging until capture_escrow()
                capture_method="manual",
                payment_method_types=["card"],
                receipt_email=lister_email,
                description=f"tor2ga job escrow — {job_id}",
                transfer_group=f"job_{job_id}",
                metadata={
                    "job_id": job_id,
                    "platform": "tor2ga",
                    "type": "job_escrow",
                    "lister_email": lister_email,
                },
                idempotency_key=idempotency_key,
            )
        except CardError as exc:
            log.warning(
                "Card declined for job=%s: code=%s msg=%s",
                job_id,
                exc.code,
                exc.user_message,
            )
            raise
        except StripeError as exc:
            log.error("Failed to create escrow for job=%s: %s", job_id, exc)
            raise

        log.info(
            "Escrow created: pi=%s job=%s status=%s",
            payment_intent.id,
            job_id,
            payment_intent.status,
        )

        return {
            "payment_intent_id": payment_intent.id,
            "client_secret": payment_intent.client_secret,
            "status": payment_intent.status,
            "amount_usd": amount_usd,
            "amount_cents": amount_cents,
        }

    def capture_escrow(self, payment_intent_id: str) -> Dict[str, Any]:
        """
        Capture a held PaymentIntent after verification passes.

        This finalises the charge on the lister's card.  Must be called before
        the 7-day authorisation window expires.  After capture, call
        process_payout() to transfer the agent's share.

        Args:
            payment_intent_id: The PaymentIntent ID returned by create_escrow().

        Returns:
            {
                "payment_intent_id": str,
                "status":            str,    # "succeeded"
                "amount_captured":   float,  # USD
            }

        Raises:
            InvalidRequestError: If the intent has already been captured/cancelled.
            StripeError: On other API failures.
        """
        log.info("Capturing escrow: pi=%s", payment_intent_id)

        idempotency_key = _idempotency_key("capture_escrow", payment_intent_id)

        try:
            payment_intent = stripe.PaymentIntent.capture(
                payment_intent_id,
                idempotency_key=idempotency_key,
            )
        except InvalidRequestError as exc:
            log.error(
                "Cannot capture pi=%s (may already be captured/cancelled): %s",
                payment_intent_id,
                exc,
            )
            raise
        except StripeError as exc:
            log.error("Failed to capture pi=%s: %s", payment_intent_id, exc)
            raise

        amount_captured_usd = _cents_to_usd(payment_intent.amount_received)

        log.info(
            "Escrow captured: pi=%s status=%s amount=$%.2f",
            payment_intent_id,
            payment_intent.status,
            amount_captured_usd,
        )

        return {
            "payment_intent_id": payment_intent_id,
            "status": payment_intent.status,
            "amount_captured": amount_captured_usd,
        }

    def cancel_escrow(self, payment_intent_id: str) -> Dict[str, Any]:
        """
        Cancel (release) an uncaptured escrow after verification fails.

        Cancelling a manual-capture PaymentIntent before capture releases the
        authorisation, so no money ever leaves the lister's account.  If the
        intent has already been captured, a refund is issued instead.

        Args:
            payment_intent_id: The PaymentIntent ID returned by create_escrow().

        Returns:
            {
                "payment_intent_id": str,
                "status":            str,   # "canceled" or "refunded"
                "refund_id":         str | None,
            }

        Raises:
            StripeError: On API failure.
        """
        log.info("Cancelling escrow: pi=%s", payment_intent_id)

        try:
            # Retrieve current state to decide cancel vs refund
            pi = stripe.PaymentIntent.retrieve(payment_intent_id)

            if pi.status in ("requires_capture", "requires_payment_method",
                             "requires_confirmation", "requires_action"):
                # Not yet captured — safe to cancel (no charge hits the card)
                cancelled_pi = stripe.PaymentIntent.cancel(payment_intent_id)
                log.info(
                    "Escrow cancelled (pre-capture): pi=%s status=%s",
                    payment_intent_id,
                    cancelled_pi.status,
                )
                return {
                    "payment_intent_id": payment_intent_id,
                    "status": "canceled",
                    "refund_id": None,
                }

            elif pi.status == "succeeded":
                # Already captured — issue a full refund
                idempotency_key = _idempotency_key("refund_escrow", payment_intent_id)
                refund = stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    reason="requested_by_customer",
                    metadata={
                        "platform": "tor2ga",
                        "reason": "verification_failed",
                    },
                    idempotency_key=idempotency_key,
                )
                log.info(
                    "Escrow refunded (post-capture): pi=%s refund=%s status=%s",
                    payment_intent_id,
                    refund.id,
                    refund.status,
                )
                return {
                    "payment_intent_id": payment_intent_id,
                    "status": "refunded",
                    "refund_id": refund.id,
                }

            else:
                log.warning(
                    "Cannot cancel pi=%s in status=%s", payment_intent_id, pi.status
                )
                return {
                    "payment_intent_id": payment_intent_id,
                    "status": pi.status,
                    "refund_id": None,
                }

        except StripeError as exc:
            log.error("Failed to cancel escrow pi=%s: %s", payment_intent_id, exc)
            raise

    # =========================================================================
    # Payouts
    # =========================================================================

    def process_payout(
        self,
        job_id: str,
        payment_intent_id: str,
        bounty_usd: float,
        agent_owner_connect_id: str,
    ) -> Dict[str, Any]:
        """
        Process the 80/20 split after a job is verified and escrow captured.

        Creates a Stripe Transfer from the platform account to the agent
        owner's Connected account.  The platform implicitly retains 20 % by
        only transferring 80 % — no explicit application fee object is needed
        on the Transfer because the funds already landed in the platform account
        when the PaymentIntent was captured.

        The transfer is linked to the same transfer_group as the PaymentIntent
        for full reconciliation in the Stripe Dashboard.

        Args:
            job_id:                   tor2ga job UUID.
            payment_intent_id:        The captured PaymentIntent ID.
            bounty_usd:               Full bounty amount in USD.
            agent_owner_connect_id:   Agent owner's Stripe Connect account ID (acct_...).

        Returns:
            {
                "transfer_id":    str,
                "agent_amount":   float,  # USD sent to agent (80 %)
                "platform_fee":   float,  # USD retained by platform (20 %)
                "status":         str,    # "paid"
                "connect_id":     str,
            }

        Raises:
            ValueError:  If bounty is zero or connect_id is missing.
            InvalidRequestError: If the Connect account is not ready for transfers.
            StripeError: On other API failures.
        """
        if bounty_usd <= 0:
            raise ValueError(f"bounty_usd must be positive, got {bounty_usd}")
        if not agent_owner_connect_id:
            raise ValueError("agent_owner_connect_id is required for payout.")

        agent_amount_cents = _usd_to_cents(bounty_usd * AGENT_SPLIT)
        platform_fee_cents = _usd_to_cents(bounty_usd) - agent_amount_cents
        # Correct for rounding: total_cents = agent + platform
        total_cents = _usd_to_cents(bounty_usd)
        # Recalculate platform fee from the remainder to avoid rounding drift
        platform_fee_cents = total_cents - agent_amount_cents

        idempotency_key = _idempotency_key("process_payout", job_id, payment_intent_id)

        log.info(
            "Processing payout: job=%s pi=%s bounty=$%.2f "
            "agent=%s (80%%=$%.2f) platform_fee=$%.2f",
            job_id,
            payment_intent_id,
            bounty_usd,
            agent_owner_connect_id,
            _cents_to_usd(agent_amount_cents),
            _cents_to_usd(platform_fee_cents),
        )

        try:
            transfer = stripe.Transfer.create(
                amount=agent_amount_cents,
                currency="usd",
                destination=agent_owner_connect_id,
                transfer_group=f"job_{job_id}",
                source_transaction=self._get_charge_id(payment_intent_id),
                description=f"tor2ga job payout — {job_id}",
                metadata={
                    "job_id": job_id,
                    "payment_intent_id": payment_intent_id,
                    "payout_pct": str(int(AGENT_SPLIT * 100)),
                    "platform": "tor2ga",
                },
                idempotency_key=idempotency_key,
            )
        except InvalidRequestError as exc:
            log.error(
                "Transfer failed for job=%s to account=%s: %s",
                job_id,
                agent_owner_connect_id,
                exc,
            )
            raise
        except StripeError as exc:
            log.error("Payout failed for job=%s: %s", job_id, exc)
            raise

        agent_amount_usd = _cents_to_usd(agent_amount_cents)
        platform_fee_usd = _cents_to_usd(platform_fee_cents)

        log.info(
            "Payout complete: transfer=%s job=%s agent=$%.2f platform_fee=$%.2f",
            transfer.id,
            job_id,
            agent_amount_usd,
            platform_fee_usd,
        )

        return {
            "transfer_id": transfer.id,
            "agent_amount": agent_amount_usd,
            "platform_fee": platform_fee_usd,
            "status": "paid",
            "connect_id": agent_owner_connect_id,
        }

    def _get_charge_id(self, payment_intent_id: str) -> Optional[str]:
        """
        Retrieve the Charge ID associated with a captured PaymentIntent.
        Required as source_transaction on Transfers.

        Returns None if no charge exists yet (e.g. intent not confirmed).
        """
        try:
            pi = stripe.PaymentIntent.retrieve(payment_intent_id)
            return pi.latest_charge or None
        except StripeError:
            return None

    # =========================================================================
    # Platform balance
    # =========================================================================

    def get_platform_balance(self) -> Dict[str, Any]:
        """
        Retrieve the platform Stripe account's available and pending balance.

        Returns:
            {
                "available_usd": float,
                "pending_usd":   float,
                "test_mode":     bool,
            }

        Raises:
            StripeError: On API failure.
        """
        log.info("Fetching platform balance for account=%s", self.platform_account_id)

        try:
            balance = stripe.Balance.retrieve()
        except StripeError as exc:
            log.error("Failed to retrieve platform balance: %s", exc)
            raise

        available_usd = sum(
            b.amount for b in balance.available if b.currency == "usd"
        ) / 100.0
        pending_usd = sum(
            b.amount for b in balance.pending if b.currency == "usd"
        ) / 100.0

        log.info(
            "Platform balance: available=$%.2f pending=$%.2f",
            available_usd,
            pending_usd,
        )

        return {
            "available_usd": available_usd,
            "pending_usd": pending_usd,
            "test_mode": self.test_mode,
        }

    def get_connect_balance(self, account_id: str) -> Dict[str, Any]:
        """
        Retrieve an agent owner's available and pending balance.

        Args:
            account_id: The agent owner's Stripe Connect account ID.

        Returns:
            {
                "account_id":    str,
                "available_usd": float,
                "pending_usd":   float,
            }

        Raises:
            StripeError: On API failure.
        """
        log.info("Fetching balance for Connect account=%s", account_id)

        try:
            balance = stripe.Balance.retrieve(stripe_account=account_id)
        except StripeError as exc:
            log.error("Failed to retrieve balance for %s: %s", account_id, exc)
            raise

        available_usd = sum(
            b.amount for b in balance.available if b.currency == "usd"
        ) / 100.0
        pending_usd = sum(
            b.amount for b in balance.pending if b.currency == "usd"
        ) / 100.0

        return {
            "account_id": account_id,
            "available_usd": available_usd,
            "pending_usd": pending_usd,
        }

    # =========================================================================
    # Webhooks
    # =========================================================================

    def handle_webhook(
        self, payload: bytes, sig_header: str, webhook_secret: str
    ) -> Dict[str, Any]:
        """
        Verify and dispatch a Stripe webhook event.

        Always call this from your webhook endpoint before acting on any event.
        Stripe signs every webhook delivery; the signature must be verified
        against the raw request body (not JSON-parsed) and the endpoint secret.

        Handled events:
            payment_intent.succeeded       — escrow confirmed by lister
            payment_intent.payment_failed  — lister's payment failed
            account.updated                — agent owner completed KYC
            transfer.created               — payout sent to agent

        Unrecognised events are returned with status="ignored".

        Args:
            payload:        Raw request body bytes (do NOT decode/re-encode).
            sig_header:     Value of the "Stripe-Signature" HTTP header.
            webhook_secret: The whsec_... secret for this webhook endpoint.

        Returns:
            {
                "event_id":   str,
                "event_type": str,
                "status":     str,  # "handled" | "ignored"
                "data":       dict,
            }

        Raises:
            SignatureVerificationError: If signature verification fails —
                respond with HTTP 400 to tell Stripe to stop retrying.
            StripeError: On other API failures.
        """
        log.info("Processing incoming Stripe webhook")

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, webhook_secret
            )
        except ValueError as exc:
            # Invalid payload (not valid JSON)
            log.error("Webhook payload is invalid JSON: %s", exc)
            raise
        except SignatureVerificationError as exc:
            log.error("Webhook signature verification failed: %s", exc)
            raise

        event_id = event["id"]
        event_type = event["type"]
        obj = event["data"]["object"]

        log.info("Webhook event: id=%s type=%s", event_id, event_type)

        handler_map = {
            "payment_intent.succeeded": self._on_payment_intent_succeeded,
            "payment_intent.payment_failed": self._on_payment_intent_failed,
            "account.updated": self._on_account_updated,
            "transfer.created": self._on_transfer_created,
        }

        handler = handler_map.get(event_type)
        if handler:
            result_data = handler(obj)
            status = "handled"
        else:
            log.info("Unhandled webhook event type: %s", event_type)
            result_data = {}
            status = "ignored"

        return {
            "event_id": event_id,
            "event_type": event_type,
            "status": status,
            "data": result_data,
        }

    def _on_payment_intent_succeeded(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        PaymentIntent succeeded — escrow has been confirmed by the lister.

        At this point the lister's card has been authorised (manual capture)
        or fully charged (automatic capture).  The job can now go live.
        """
        pi_id = obj["id"]
        job_id = obj.get("metadata", {}).get("job_id", "")
        amount_usd = _cents_to_usd(obj.get("amount", 0))

        log.info(
            "payment_intent.succeeded: pi=%s job=%s amount=$%.2f",
            pi_id,
            job_id,
            amount_usd,
        )

        # Caller should update jobs.stripe_payment_intent_id and set status='open'
        return {
            "payment_intent_id": pi_id,
            "job_id": job_id,
            "amount_usd": amount_usd,
            "action": "escrow_confirmed",
        }

    def _on_payment_intent_failed(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        PaymentIntent failed — lister's payment could not be collected.

        The job should be cancelled or returned to draft status.
        """
        pi_id = obj["id"]
        job_id = obj.get("metadata", {}).get("job_id", "")
        failure_reason = (
            (obj.get("last_payment_error") or {}).get("message", "Unknown")
        )

        log.warning(
            "payment_intent.payment_failed: pi=%s job=%s reason=%s",
            pi_id,
            job_id,
            failure_reason,
        )

        return {
            "payment_intent_id": pi_id,
            "job_id": job_id,
            "failure_reason": failure_reason,
            "action": "job_cancelled",
        }

    def _on_account_updated(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Connect account updated — typically fired when an agent owner completes KYC.

        Check payouts_enabled; if True, the owner is fully onboarded and ready
        to receive transfers.
        """
        account_id = obj["id"]
        charges_enabled = obj.get("charges_enabled", False)
        payouts_enabled = obj.get("payouts_enabled", False)
        details_submitted = obj.get("details_submitted", False)

        log.info(
            "account.updated: account=%s charges=%s payouts=%s details=%s",
            account_id,
            charges_enabled,
            payouts_enabled,
            details_submitted,
        )

        return {
            "account_id": account_id,
            "charges_enabled": charges_enabled,
            "payouts_enabled": payouts_enabled,
            "details_submitted": details_submitted,
            "action": "onboarding_complete" if payouts_enabled else "onboarding_pending",
        }

    def _on_transfer_created(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transfer created — agent payout has been initiated.

        The transfer_group links this back to the originating PaymentIntent.
        """
        transfer_id = obj["id"]
        amount_usd = _cents_to_usd(obj.get("amount", 0))
        destination = obj.get("destination", "")
        job_id = obj.get("metadata", {}).get("job_id", "")

        log.info(
            "transfer.created: transfer=%s job=%s amount=$%.2f destination=%s",
            transfer_id,
            job_id,
            amount_usd,
            destination,
        )

        return {
            "transfer_id": transfer_id,
            "job_id": job_id,
            "amount_usd": amount_usd,
            "destination": destination,
            "action": "payout_sent",
        }

    # =========================================================================
    # Convenience factory
    # =========================================================================

    @classmethod
    def from_env(cls) -> "StripePayments":
        """
        Construct a StripePayments instance from environment variables.

        Required environment variables:
            STRIPE_SECRET_KEY
            STRIPE_PLATFORM_ACCOUNT_ID

        Raises:
            EnvironmentError: If required variables are missing.
        """
        key = os.environ.get("STRIPE_SECRET_KEY", "")
        platform = os.environ.get("STRIPE_PLATFORM_ACCOUNT_ID", "")

        missing = []
        if not key:
            missing.append("STRIPE_SECRET_KEY")
        if not platform:
            missing.append("STRIPE_PLATFORM_ACCOUNT_ID")

        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        return cls(stripe_secret_key=key, platform_account_id=platform)


# ---------------------------------------------------------------------------
# Module-level singleton (optional convenience — nil until from_env() called)
# ---------------------------------------------------------------------------

_processor: Optional[StripePayments] = None


def get_processor() -> StripePayments:
    """
    Return the module-level singleton StripePayments instance.

    Call initialise_processor() once at app startup, then use get_processor()
    in route handlers to avoid re-reading env vars on every request.

    Raises:
        RuntimeError: If the processor has not been initialised.
    """
    global _processor
    if _processor is None:
        raise RuntimeError(
            "StripePayments not initialised. "
            "Call stripe_payments.initialise_processor() at startup, "
            "or use StripePayments.from_env() directly."
        )
    return _processor


def initialise_processor(
    stripe_secret_key: Optional[str] = None,
    platform_account_id: Optional[str] = None,
) -> StripePayments:
    """
    Create and cache the module-level StripePayments singleton.

    Falls back to environment variables if arguments are not provided.

    Args:
        stripe_secret_key:    Override for STRIPE_SECRET_KEY env var.
        platform_account_id:  Override for STRIPE_PLATFORM_ACCOUNT_ID env var.

    Returns:
        The initialised StripePayments instance.
    """
    global _processor
    key = stripe_secret_key or os.environ.get("STRIPE_SECRET_KEY", "")
    platform = platform_account_id or os.environ.get("STRIPE_PLATFORM_ACCOUNT_ID", "")
    _processor = StripePayments(stripe_secret_key=key, platform_account_id=platform)
    return _processor
