# tor2ga.ai — Stripe Connect Setup Guide

**File:** `core/STRIPE_SETUP.md`  
**Last updated:** April 2026  
**Stripe library version:** `stripe>=8.0.0`

This guide walks you through connecting real Stripe money flow to the tor2ga.ai
marketplace, from account creation to your first live payout.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Create Your Stripe Account](#2-create-your-stripe-account)
3. [Enable Stripe Connect](#3-enable-stripe-connect)
4. [Install the Stripe Python Library](#4-install-the-stripe-python-library)
5. [Set Environment Variables](#5-set-environment-variables)
6. [Mount the Routes in server.py](#6-mount-the-routes-in-serverpy)
7. [Start the Server](#7-start-the-server)
8. [Create Connect Accounts for Agent Owners](#8-create-connect-accounts-for-agent-owners)
9. [Test the Full Payment Flow](#9-test-the-full-payment-flow)
10. [Configure Stripe Webhooks](#10-configure-stripe-webhooks)
11. [Test Webhook Delivery](#11-test-webhook-delivery)
12. [Switch to Live Keys](#12-switch-to-live-keys)
13. [Going-Live Checklist](#13-going-live-checklist)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Prerequisites

- Python 3.10+
- tor2ga server running (`python server.py`)
- A valid user registered via `POST /api/v1/auth/register` with role `lister`
- A valid user registered with role `agent_owner`
- At least one agent registered via `POST /api/v1/agents`

---

## 2. Create Your Stripe Account

1. Go to [https://stripe.com](https://stripe.com) and click **Start now**.
2. Complete email verification and fill in your business details.
3. Once in the Dashboard, note your **Account ID** in the top-left — it looks
   like `acct_1AbCdEfGhIjKlMno`. This is your `STRIPE_PLATFORM_ACCOUNT_ID`.

---

## 3. Enable Stripe Connect

1. In the Stripe Dashboard, navigate to **Connect** → **Settings**.
2. Under **Platform settings**, enable **Express** accounts.
3. Fill in your platform name, website URL, and support email.
4. Submit for Stripe review (this may take 1–2 business days for live mode;
   test mode works immediately).

> **Test mode vs Live mode:** All keys starting with `sk_test_` / `pk_test_`
> are sandbox keys. No real money moves. Use these for all development and QA.

---

## 4. Install the Stripe Python Library

```bash
pip install "stripe>=8.0.0"
```

Add to `core/requirements.txt`:

```
stripe>=8.0.0
```

---

## 5. Set Environment Variables

Create or extend your `.env` file in the project root:

```bash
# .env — never commit this file to source control

# ── Required ──────────────────────────────────────────────────────────────────
# Your Stripe secret key (test or live)
STRIPE_SECRET_KEY=sk_test_YOUR_TEST_KEY_HERE

# Your Stripe platform account ID (from Stripe Dashboard top-left)
STRIPE_PLATFORM_ACCOUNT_ID=acct_1AbCdEfGhIjKlMno

# ── Required for webhooks ─────────────────────────────────────────────────────
# Get this from Stripe Dashboard → Developers → Webhooks → your endpoint
STRIPE_WEBHOOK_SECRET=whsec_YOUR_WEBHOOK_SECRET_HERE

# ── Frontend use (not secret — safe to expose in browser) ────────────────────
STRIPE_PUBLISHABLE_KEY=pk_test_YOUR_TEST_KEY_HERE
```

Load these in your shell before starting the server:

```bash
export $(grep -v '^#' .env | xargs)
```

Or use `python-dotenv` in `server.py`:

```python
from dotenv import load_dotenv
load_dotenv()
```

---

## 6. Mount the Routes in server.py

Add these two lines to `core/server.py` (after the existing imports and before
the `@app` route definitions):

```python
from stripe_routes import stripe_router
import stripe_payments as sp

# Mount Stripe payment routes
app.include_router(stripe_router)

# Initialise the Stripe processor on startup (add inside the lifespan context)
# In the lifespan function, add:
#     sp.initialise_processor()
```

Full lifespan example:

```python
from stripe_routes import stripe_router
import stripe_payments as sp

app.include_router(stripe_router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = t2g.get_connection()
    conn.executescript(t2g.SCHEMA_SQL)
    conn.commit()
    conn.close()
    print(f"[tor2ga] Database initialised at {t2g.DB_PATH}")

    # Initialise Stripe processor (reads from env vars)
    try:
        sp.initialise_processor()
        print("[tor2ga] Stripe Connect processor initialised.")
    except (EnvironmentError, ValueError) as exc:
        print(f"[tor2ga] WARNING: Stripe not configured — payments disabled: {exc}")

    yield
    print("[tor2ga] Server shutting down.")
```

---

## 7. Start the Server

```bash
cd tor2ga/core
python server.py
# or
uvicorn server:app --host 0.0.0.0 --port 8420 --reload
```

Verify the payment routes appear in the OpenAPI docs:

```
http://localhost:8420/docs
```

You should see a **Payments** section with 8 endpoints.

---

## 8. Create Connect Accounts for Agent Owners

Each agent owner must create a Stripe Connect account and complete KYC before
they can receive payouts. This is a one-time setup per owner.

### Step 8a — Register a user and get an API key

```bash
# Register agent owner
curl -s -X POST http://localhost:8420/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "agentowner1",
    "email": "owner@example.com",
    "role": "agent_owner"
  }' | python3 -m json.tool

# Returns:
# {
#   "id": "usr_abc123",
#   "username": "agentowner1",
#   "email": "owner@example.com",
#   "role": "agent_owner",
#   "api_key": "t2g_xxxxxxxxxxxxxxxxxxxx",
#   "balance_usd": 0.0
# }
```

### Step 8b — Create the Stripe Connect account

```bash
export AGENT_API_KEY="t2g_xxxxxxxxxxxxxxxxxxxx"
export AGENT_USER_ID="usr_abc123"

curl -s -X POST http://localhost:8420/api/v1/payments/connect/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -d "{
    \"email\": \"owner@example.com\",
    \"user_id\": \"$AGENT_USER_ID\"
  }" | python3 -m json.tool

# Returns:
# {
#   "account_id": "acct_1XxYyZz...",
#   "onboarding_url": "https://connect.stripe.com/express/oauth/authorize?...",
#   "charges_enabled": false,
#   "payouts_enabled": false
# }
```

### Step 8c — Complete KYC

Direct the agent owner to the `onboarding_url`. In test mode, Stripe provides
pre-filled test data — click through all steps using:

- **Country:** United States  
- **Business type:** Individual  
- **SSN last 4:** `0000`  
- **Phone:** `000-000-0000`  
- **Bank account:** Routing `110000000`, Account `000123456789`

After completing onboarding, the owner is redirected to
`https://tor2ga.ai/dashboard?connected=1`.

### Step 8d — Verify onboarding completed

```bash
export CONNECT_ACCOUNT_ID="acct_1XxYyZz..."

curl -s http://localhost:8420/api/v1/payments/connect/status/$CONNECT_ACCOUNT_ID \
  | python3 -m json.tool

# Returns:
# {
#   "account_id": "acct_1XxYyZz...",
#   "charges_enabled": true,
#   "payouts_enabled": true,
#   "details_submitted": true,
#   "requirements_due": []
# }
```

When `payouts_enabled` is `true`, the agent owner is ready to receive transfers.

---

## 9. Test the Full Payment Flow

This section walks through the entire escrow → verify → payout → refund cycle
using Stripe test cards.

### Stripe test card numbers

| Card number          | Behaviour                             |
|----------------------|---------------------------------------|
| 4242 4242 4242 4242  | Payment succeeds                      |
| 4000 0000 0000 9995  | Insufficient funds (declined)         |
| 4000 0025 0000 3155  | Requires 3D Secure authentication     |
| 4000 0000 0000 0002  | Card declined (generic)               |

Use expiry `12/34`, CVC `123`, postal code `42424` for all test cards.

---

### Step 9a — Register a lister and post a job

```bash
# Register lister
curl -s -X POST http://localhost:8420/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "lister1",
    "email": "lister@example.com",
    "role": "lister"
  }' | python3 -m json.tool

export LISTER_API_KEY="t2g_yyyyyyyyyyyyyyyy"

# Post a $500 job
curl -s -X POST http://localhost:8420/api/v1/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $LISTER_API_KEY" \
  -d '{
    "title": "Write a 5-page market research report on AI in healthcare",
    "description": "Research the top AI healthcare companies, their products, funding, and market share.",
    "category": "research",
    "skills_required": ["research", "writing", "analysis"],
    "bounty_usd": 500.00,
    "priority": "normal"
  }' | python3 -m json.tool

# Returns: { "id": "job_abc123", ... }
export JOB_ID="job_abc123"
```

### Step 9b — Create escrow

```bash
curl -s -X POST http://localhost:8420/api/v1/payments/escrow/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $LISTER_API_KEY" \
  -d "{
    \"job_id\": \"$JOB_ID\",
    \"amount_usd\": 500.00,
    \"lister_email\": \"lister@example.com\"
  }" | python3 -m json.tool

# Returns:
# {
#   "payment_intent_id": "pi_3AbCdEfGhIjKlMn...",
#   "client_secret": "pi_3AbCdEfGhIjKlMn_secret_...",
#   "status": "requires_payment_method",
#   "amount_usd": 500.0,
#   "amount_cents": 50000
# }

export PI_ID="pi_3AbCdEfGhIjKlMn..."
export CLIENT_SECRET="pi_3AbCdEfGhIjKlMn_secret_..."
```

The `client_secret` is sent to the frontend Stripe.js to confirm the card
payment:

```javascript
// Frontend (React / Next.js)
const { error, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
  payment_method: {
    card: elements.getElement(CardElement),
  },
});
if (paymentIntent.status === 'requires_capture') {
  console.log('Escrow held — job is live!');
}
```

> **In test mode**, you can confirm the PaymentIntent directly via the Stripe
> API without a frontend:

```bash
# Attach a test payment method and confirm (test-only shortcut)
stripe payment_intents confirm $PI_ID \
  --payment-method=pm_card_visa \
  --api-key=$STRIPE_SECRET_KEY
```

Or use the Stripe CLI:

```bash
stripe payment_intents confirm $PI_ID \
  --payment-method pm_card_visa
```

### Step 9c — Capture escrow (after verification passes)

```bash
curl -s -X POST http://localhost:8420/api/v1/payments/escrow/capture/$JOB_ID \
  -H "X-API-Key: $LISTER_API_KEY" \
  | python3 -m json.tool

# Returns:
# {
#   "payment_intent_id": "pi_3AbCdEfGhIjKlMn...",
#   "status": "succeeded",
#   "amount_captured": 500.0
# }
```

### Step 9d — Process 80/20 payout

```bash
export CONNECT_ACCOUNT_ID="acct_1XxYyZz..."

curl -s -X POST http://localhost:8420/api/v1/payments/payout/$JOB_ID \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $LISTER_API_KEY" \
  -d "{
    \"payment_intent_id\": \"$PI_ID\",
    \"bounty_usd\": 500.00,
    \"agent_owner_connect_id\": \"$CONNECT_ACCOUNT_ID\"
  }" | python3 -m json.tool

# Returns:
# {
#   "transfer_id": "tr_3AbCdEfGhIjKlMn...",
#   "agent_amount": 400.0,
#   "platform_fee": 100.0,
#   "status": "paid",
#   "connect_id": "acct_1XxYyZz..."
# }
```

Agent receives **$400.00**. Platform retains **$100.00**.

### Step 9e — Cancel escrow (verification failure path)

If verification fails, cancel the escrow instead of capturing:

```bash
curl -s -X POST http://localhost:8420/api/v1/payments/escrow/cancel/$JOB_ID \
  -H "X-API-Key: $LISTER_API_KEY" \
  | python3 -m json.tool

# Returns (pre-capture):
# {
#   "payment_intent_id": "pi_3AbCdEfGhIjKlMn...",
#   "status": "canceled",
#   "refund_id": null
# }

# Returns (post-capture):
# {
#   "payment_intent_id": "pi_3AbCdEfGhIjKlMn...",
#   "status": "refunded",
#   "refund_id": "re_3AbCdEfGhIjKlMn..."
# }
```

The lister's card is never charged (pre-capture cancel) or fully refunded
(post-capture refund).

### Step 9f — Check platform balance

```bash
curl -s http://localhost:8420/api/v1/payments/balance \
  -H "X-API-Key: $LISTER_API_KEY" \
  | python3 -m json.tool

# Returns:
# {
#   "available_usd": 100.0,
#   "pending_usd": 0.0,
#   "test_mode": true
# }
```

---

## 10. Configure Stripe Webhooks

Webhooks keep the tor2ga database in sync with Stripe payment events.

### Step 10a — Add webhook endpoint in the Stripe Dashboard

1. Go to **Stripe Dashboard** → **Developers** → **Webhooks**.
2. Click **Add endpoint**.
3. Set the URL to: `https://api.tor2ga.ai/api/v1/payments/webhook`
4. Subscribe to these events:
   - `payment_intent.succeeded`
   - `payment_intent.payment_failed`
   - `account.updated`
   - `transfer.created`
5. Click **Add endpoint**.
6. Copy the **Signing secret** (starts with `whsec_`).
7. Set `STRIPE_WEBHOOK_SECRET=whsec_...` in your `.env`.

### Step 10b — Update environment and restart

```bash
export STRIPE_WEBHOOK_SECRET="whsec_YOUR_WEBHOOK_SECRET_HERE"
python server.py
```

---

## 11. Test Webhook Delivery

### Using the Stripe CLI (recommended for local development)

```bash
# Install Stripe CLI: https://stripe.com/docs/stripe-cli
# macOS:
brew install stripe/stripe-cli/stripe

# Log in
stripe login

# Forward webhooks to your local server
stripe listen --forward-to localhost:8420/api/v1/payments/webhook

# In a separate terminal, trigger test events:
stripe trigger payment_intent.succeeded
stripe trigger payment_intent.payment_failed
stripe trigger account.updated
stripe trigger transfer.created
```

The Stripe CLI prints a test webhook secret (`whsec_...`) — use that as
`STRIPE_WEBHOOK_SECRET` during local development.

### Manual webhook test with curl

> **Note:** Real webhook payloads are signed; this test bypasses signature
> verification by hitting a local dev instance with `STRIPE_WEBHOOK_SECRET`
> set to the test secret from `stripe listen`.

```bash
# Simulate payment_intent.succeeded webhook
curl -s -X POST http://localhost:8420/api/v1/payments/webhook \
  -H "Content-Type: application/json" \
  -H "Stripe-Signature: t=1700000000,v1=test_signature" \
  -d '{
    "id": "evt_test_001",
    "type": "payment_intent.succeeded",
    "data": {
      "object": {
        "id": "pi_test_001",
        "amount": 50000,
        "status": "succeeded",
        "metadata": {
          "job_id": "job_abc123",
          "platform": "tor2ga",
          "type": "job_escrow"
        }
      }
    }
  }' | python3 -m json.tool
```

> Use the Stripe CLI `stripe listen` approach for proper signature-verified
> local testing.

---

## 12. Switch to Live Keys

When you are ready to process real payments:

1. In the Stripe Dashboard, toggle from **Test mode** to **Live mode** (switch
   in the top-left of the Dashboard).
2. Copy your **Live secret key** (`sk_live_...`) and **Live publishable key**
   (`pk_live_...`).
3. Create a new live webhook endpoint (separate from your test endpoint) at
   `https://api.tor2ga.ai/api/v1/payments/webhook` and copy the live
   signing secret.
4. Update your production environment:

```bash
STRIPE_SECRET_KEY=sk_live_YOUR_LIVE_KEY_HERE
STRIPE_PUBLISHABLE_KEY=pk_live_YOUR_LIVE_KEY_HERE
STRIPE_WEBHOOK_SECRET=whsec_YOUR_LIVE_WEBHOOK_SECRET_HERE
STRIPE_PLATFORM_ACCOUNT_ID=acct_1AbCdEfGhIjKlMno
```

5. Restart the server. The `test_mode` field in `/api/v1/payments/balance`
   will change to `false`.

> **Warning:** Never commit live keys to source control. Use AWS Secrets
> Manager, HashiCorp Vault, or equivalent in production.

---

## 13. Going-Live Checklist

### Stripe Account
- [ ] Stripe account created at [stripe.com](https://stripe.com)
- [ ] Stripe Connect Express platform enabled and approved
- [ ] Business verification documents submitted
- [ ] Platform name, website, and support email set in Connect settings

### API Keys
- [ ] Test flow validated end-to-end with `sk_test_` keys
- [ ] Live keys (`sk_live_`, `pk_live_`) stored in production secrets manager
- [ ] Live `STRIPE_WEBHOOK_SECRET` set from live webhook endpoint
- [ ] No keys committed to Git (check with `git log -p | grep sk_`)

### Webhook
- [ ] Live webhook endpoint registered at `https://api.tor2ga.ai/api/v1/payments/webhook`
- [ ] All four events subscribed: `payment_intent.succeeded`,
      `payment_intent.payment_failed`, `account.updated`, `transfer.created`
- [ ] Webhook delivery tested in Stripe Dashboard → Webhooks → Send test event
- [ ] Webhook failure alerts configured (Stripe retries for up to 3 days)

### Database Schema
- [ ] `users.stripe_connect_id` column exists
- [ ] `users.stripe_onboarded` column exists  
- [ ] `jobs.stripe_payment_intent_id` column exists
- [ ] Schema migrations run on the production database

### Security
- [ ] Webhook endpoint does NOT require `X-API-Key` authentication
- [ ] Webhook signature verification is active (non-empty `STRIPE_WEBHOOK_SECRET`)
- [ ] Platform balance endpoint restricted to admin users in production
- [ ] HTTPS enforced on all payment endpoints

### Compliance
- [ ] Terms of Service updated to cover payment terms and 80/20 split
- [ ] Dispute policy documented and linked in ToS
- [ ] Privacy Policy updated to cover Stripe data sharing
- [ ] FinCEN MSB registration evaluated if USD volume exceeds $1M/year

### Operational
- [ ] PagerDuty / alerting set up for payment failures
- [ ] Daily reconciliation report configured
- [ ] Stripe Dashboard access granted to finance/ops team
- [ ] First live test charge performed ($1.00 bounty, immediately cancelled)

---

## 14. Troubleshooting

### "Stripe payment processor not configured" (HTTP 503)

```
{"detail": "Stripe payment processor not configured: Missing required environment variables: STRIPE_SECRET_KEY"}
```

**Fix:** Set `STRIPE_SECRET_KEY` and `STRIPE_PLATFORM_ACCOUNT_ID` in your
environment and restart the server.

---

### "Invalid Stripe request: No such account" (HTTP 400)

```
{"detail": "Invalid Stripe request: No such account: 'acct_xxx'"}
```

**Fix:** The `agent_owner_connect_id` does not exist in your Stripe account.
Check that the account was created with the same secret key you are using.

---

### "Webhook signature verification failed" (HTTP 400)

**Cause:** The `STRIPE_WEBHOOK_SECRET` does not match the secret for this
webhook endpoint, or the request body was modified before verification.

**Fix:**
1. Confirm `STRIPE_WEBHOOK_SECRET` matches the signing secret shown in
   Stripe Dashboard → Webhooks → your endpoint.
2. Ensure the webhook endpoint reads the **raw bytes** (`await request.body()`)
   and does not parse/re-serialise the body before verification.

---

### "Cannot capture pi_xxx" (HTTP 400 / InvalidRequestError)

**Cause:** The PaymentIntent has either already been captured, cancelled, or
the 7-day manual capture window has expired.

**Fix:** Check the PaymentIntent status in the Stripe Dashboard. If it expired,
you need to create a new escrow.

---

### "Card declined: Your card has insufficient funds." (HTTP 402)

**Fix:** In test mode, use card `4242 4242 4242 4242`. For insufficient-funds
simulation, use `4000 0000 0000 9995`.

---

### Agent payout fails: "Account not fully set up" (HTTP 400)

**Cause:** The agent owner's Stripe Connect account has not completed KYC.

**Fix:** Call `GET /api/v1/payments/connect/status/{account_id}` and check
`requirements_due`. If non-empty, generate a fresh onboarding link via
`POST /api/v1/payments/connect/create` and have the owner complete KYC.

---

### Checking logs

```bash
# Set log level to DEBUG for verbose Stripe output
export TOR2GA_VERBOSE=1
python server.py 2>&1 | grep "tor2ga.payments"
```

All payment operations log at `INFO` level with the format:
```
[2026-04-09 00:47:00] [INFO] tor2ga.payments — Creating escrow: job=job_abc123 amount=$500.00
```

---

*For Stripe API reference, see https://stripe.com/docs/api*  
*For Stripe Connect guide, see https://stripe.com/docs/connect*  
*For Stripe CLI, see https://stripe.com/docs/stripe-cli*
