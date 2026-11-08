# Keyhold — build plan

Entry for the PayPal AI Hackathon (Devpost). Deadline: Thu Nov 12 2026 12:00 PST (Fri Nov 13 04:00 Taipei).
Judging: Technological Implementation, Design, Potential Impact, Innovation, Presentation, equally weighted, tie-break on
Technological Implementation. Judges may not run the project; a working build they CAN run is still required (Stage One).
PayPal judges reward: PayPal central, money actually moving, several PayPal capabilities composed, tested unhappy paths,
idempotency, verified webhooks, the official Server SDK and JS SDK v6, a reliable demo.

This is a clean-room project written from scratch. Do not read, copy or mention any other project of the author.

## Product

When a Linux server stops answering SSH, the owner's last way in is the hosting provider's web console (a screen and a
keyboard). Keyhold lends that keyboard to an AI agent under a PayPal lock:

**No hold, no keystrokes. Only your server answering can charge you.**

1. **Free look (no money).** The agent may take screenshots of the console but cannot press keys. Claude returns a fault
   class from a fixed taxonomy, evidence lines quoted from the screen, a price tier and a plan.
   Tiers: T1 $9 single config fix · T2 $19 boot-path repair · T3 $29 kernel/initramfs/disk.
   Code checks the tier against a class→tier table and hashes the quote. A capture can equal or undercut it, never exceed it.
2. **Owner probe, fixed at checkout.** Host, SSH port and user, the probe public key (the owner installs it; the agent and
   its process never see the private key), a URL and the expected content (substring or sha256). The probe spec hash goes
   into the order's custom_id.
3. **Hold.** The owner approves a JS SDK v6 one-time PayPal payment session; the server creates the order with
   intent AUTHORIZE for the quoted tier and authorizes it.
4. **The lock.** Keystrokes reach the console only through a gate. The gate grants input only after GET authorization
   shows CREATED with amount ≥ quote, re-checks PayPal every N actions and on a short cache TTL, and ends the session
   (HoldRevoked) the moment the authorization is VOIDED, expired or missing. Owner Stop = void = keyboard gone.
5. **Capture only on the owner probe.** Pass = SSH login with the hidden key AND the page content matches, twice, ≥30 s
   apart, with no failure between (flapping rule). Then capture the quoted amount (or less) with final_capture=true and
   PayPal-Request-Id cap-{rescue_id}. Neither the model nor the agent has any tool that can capture.
6. **Void.** give_up(reason) tool, turn limit, owner Stop, rescue timeout, probe never passing → void with
   PayPal-Request-Id void-{rescue_id}. Never void after capture.
7. **Warranty.** Re-probe hourly for 24 h (minutes in judge mode, labelled). Two consecutive failures → full refund with
   PayPal-Request-Id refund-{rescue_id}.
8. **Webhooks.** PAYMENT.AUTHORIZATION.CREATED/VOIDED, PAYMENT.CAPTURE.COMPLETED/REFUNDED/DECLINED/DENIED/PENDING/REVERSED,
   PAYMENT.REFUND.PENDING/FAILED. Store raw body and
   headers, return 200, verify with POST /v1/notifications/verify-webhook-signature, dedupe on event id, never regress state.
   The synchronous API response is the first tick; the verified webhook is the second.
9. **Reconciler.** At startup and every 5 min, read every non-terminal rescue back from PayPal and repair; replay pending
   outbox operations with the same request id.
10. **Honest stop.** Some faults cannot be fixed from the console (for example a GRUB menu protected by a password). The
    agent must call give_up with a reason, which voids the hold.

## Environment facts

- macOS on Apple Silicon (M5 Max). No Homebrew. OrbStack is installed (`orb`, `docker`). QEMU is not installed natively.
  Run QEMU inside a Linux container or OrbStack machine (apt install qemu-system-arm / qemu-system-x86, qemu-utils,
  genisoimage/cloud-image-utils). No KVM locally (TCG emulation, slower); a cloud Linux host with KVM is used for hosting.
- Guest: an official Debian 13 arm64 "genericcloud" qcow2 (download from cloud.debian.org, verify SHA512) with a cloud-init
  NoCloud seed. Each run uses a copy-on-write overlay so a fault can be reset instantly.
- Python ≥3.10 with uv (`uv run pytest -q`). Claude via the official Anthropic Python SDK (`anthropic`), model
  claude-opus-5-5, adaptive thinking, effort set explicitly; handle stop_reason "refusal" and enable the server-side
  fallbacks parameter as documented in the Claude API documentation for the Python SDK (read the SDK README and the
  tool-use guide before writing model code; do not guess SDK names). Credentials come from the environment at runtime.
- PayPal: the official Python package `paypal-server-sdk` (read the installed package to get real class and method names).
  Sandbox only; refuse any non-sandbox base URL. Platform notes, sandbox pitfalls and JS SDK v6 docs are in the research
  notes kept outside this repository (PayPal platform summary, JS SDK v6 pages, Orders v2 and Payments v2 schemas).
  Known: hosted sandbox MCP is broken; Agent Toolkit has no authorize/void; webhooks take 8–25 s
  (sometimes 1–2 min) in sandbox; simulator webhook events cannot pass signature verification.

## Package layout and interfaces (build to these so parts can be written in parallel)

```
keyhold/
  pay/client.py      PayPalClient (official SDK) and FakePayPal (same interface)
  pay/ledger.py      SQLite: rescues, state machine, outbox, events, webhook intake
  pay/reconciler.py  repair from PayPal, replay outbox
  pay/webhooks.py    intake, verify, dedupe, apply with state ranks
  pay/gate.py        HoldLease, GatedConsole, HoldRevoked
  console/base.py    Console protocol
  console/qemu.py    QEMU QMP backend (screendump PNG, send-key / input-send-event, system_reset)
  console/fake.py    scripted fake console for tests
  console/image.py   base image download/verify, cloud-init seed, overlays
  faults/            fault catalog: plant scripts, expected symptom text, class, tier, solvable flag
  agent/model.py     Claude client wrapper (+ FakeModel for tests)
  agent/firewall.py  keystroke firewall: refuse destructive commands (mkfs, dd of=/dev/*, rm -rf /, shred, wipefs,
                     reading /etc/shadow, writing authorized_keys, changing root password), with tests
  agent/quote.py     free look → Quote(fault_class, evidence[], tier, plan, quote_hash)
  agent/loop.py      keyboard-only agent: tools screenshot(), type(text), key(combo), power_cycle(), give_up(reason),
                     finished(summary); returns AgentResult(outcome: done|gave_up|turn_limit|revoked|error, reason, turns)
  probe/owner.py     ProbeSpec, run_probe (SSH BatchMode subprocess + HTTP GET), stable-pass rule, warranty schedule
  rescue.py          orchestrator: quote → hold → gated agent → probe → capture|void → warranty
web/                 FastAPI app (see below)
tests/               unit, property, chaos, E2E with fakes
```

Interfaces:
- `PayPalClient`/`FakePayPal`: `client_token()`, `create_hold(amount: Decimal, custom_id, invoice_id, request_id) -> Order`,
  `authorize(order_id, request_id) -> Authorization`, `get_order(id)`, `get_authorization(id) -> Authorization(id, status,
  amount, expiration_time)`, `capture(auth_id, amount, final_capture, invoice_id, request_id) -> Capture`,
  `void(auth_id, request_id)`, `refund(capture_id, amount, request_id) -> Refund`, `get_capture(id)`,
  `verify_webhook(headers, raw: bytes, webhook_id) -> bool`. FakePayPal enforces: capture ≤ authorized; final_capture
  releases the remainder; same request id → same result; void after capture fails; refund ≤ captured; injectable errors
  (AUTHORIZATION_EXPIRED, INTERNAL_SERVER_ERROR, TRANSACTION_REFUSED, PAYMENT_DENIED, PREVIOUSLY_CAPTURED,
  REFUND_AMOUNT_EXCEEDED).
- `Console`: `screenshot() -> bytes (PNG)`, `type_text(text)`, `press(keys: list[str])`, `power_cycle()`, `close()`.
- `GatedConsole(console, lease)`: screenshot always allowed; every input method calls `lease.require()` first.
- `run_agent(console, model, *, goal, quote, max_turns, firewall, on_event) -> AgentResult`.
- `run_probe(spec, ssh=..., http=...) -> ProbeResult(ok, ssh_ok, http_ok, detail, at)`.
- Ledger states: QUOTED → HOLD_PENDING → HELD → ACTIVE → PROBING → CAPTURED → WARRANTY_OK | REFUNDED; any state before
  CAPTURED → VOIDED. Outbox rows (op, request_id = {op}-{rescue_id}, for example cap-{rescue_id}; see
  `keyhold.types.request_id_for`) written before each PayPal call; at most one money operation in flight per rescue.

## Web (FastAPI + Jinja2, no heavy front-end framework)

- `/` what it is, in one screen. `/try` (passcode gate, per-IP rate limit, one VM at a time, model-spend cap): pick one of
  three whitelisted faults → a fresh overlay boots into the fault → free-look quote shown with evidence → JS SDK v6
  AUTHORIZE checkout → live status page (SSE) that reads PayPal state live: held $X → captured $Y or voided, with order,
  authorization and capture ids and timestamps, console thumbnails, every agent action and firewall refusal, probe results,
  and a Stop button (void). `/judge` ledger of all runs. `/webhooks/paypal`. `/api/orders`, `/api/orders/{id}/authorize`.
  `/healthz`. Design: calm, precise, operations-console feel; works on a phone; light and dark.

## Invariants (property-tested)

captured ≤ authorized ≤ quote · each rescue ends in exactly one of CAPTURED or VOIDED · never two captures · never void
after capture · refund ≤ captured · no keystroke reaches the console unless the lease saw CREATED for this rescue · a
revoked lease stops input within one action · replaying a money operation with its request id is a no-op · webhook
replays never change state · a crash between the outbox write and the PayPal response is repaired by the reconciler with
exactly one capture.

## Rules

- Sandbox only. Tests make no real network calls (fakes for PayPal, console, model, SSH, HTTP).
- No secrets in the repo; `.env.example` only. Never print secrets.
- Never touch any existing server or cloud account. Build and run VMs only locally in containers.
- No cloud-provider brand names, IP addresses or hostnames in code, docs, fixtures or UI text.
- Commits: author Ted Chen (already configured), `TZ=Asia/Taipei`, no "Co-Authored-By" trailer. Small commits with plain
  messages. README credits "AI coding assistance (Claude)".
- Plain, specific English in docs and UI. No hype, no "first" claims.
