# Live runs (2026-11-07 and 11-08, UTC)

Four runs against the real PayPal sandbox. Every ID below can be looked up in the sandbox
business account that owns the REST app. Money is sandbox money.

| File | What ran | Result |
|---|---|---|
| [live-check-20261107T213209Z.md](live-check-20261107T213209Z.md) | `tools/live_check.py`: two $12.00 holds approved by a sandbox buyer, a $7.00 capture with `final_capture`, request-id replays, a refused second capture and a refused void after capture, a $2.00 refund, a void of the second hold, four `PayPal-Mock-Response` refusals | 22 passed, 0 failed, 1 skipped (webhook verification needs a public URL) |
| [rescue-first-a2b969fd.md](rescue-first-a2b969fd.md) | `/try` in live mode: the "Typo in sshd_config" test VM, Claude Haiku 5.5, a $9.00 hold approved through the JS SDK v6 button | captured $9.00 (capture `6WS22323FC4216719`) 2 min 28 s after the hold; warranty 24 checks of 24 passed |
| [rescue-hosted-53f4549c.md](rescue-hosted-53f4549c.md) | the same flow on the hosted Keyhold: the public URL, an x86-64 cloud VM with KVM, the judge link, a sandbox buyer approving on the public page | captured $9.00 (capture `7WP61145AR824644E`) 1 min 45 s after the hold; 7 agent turns; about $0.0039 of model spend; warranty 24 checks of 24 passed |
| [rescue-clean-3fcc3c58.md](rescue-clean-3fcc3c58.md) | the same flow after the two fixes below | captured $9.00 (capture `8WA56714MN162430M`) 1 min 48 s after the hold; 8 agent turns; about $0.0034 of model spend; warranty 24 checks of 24 passed |

## What the first rescue showed, and what changed

Both problems showed up in the first live runs, and both are fixed.

1. **The free look said `unknown`.** The screen showed `Failed to start ssh.service`, but the
   prompt offered only `unknown` when more than one class could fit. A first attempt on the same
   fault ended with no hold (nothing held, nothing charged). The prompt now says that classes in
   the same tier cost the same, so the free look names the most likely one and the plan's first
   step confirms it. `unknown` stays for screens that show no failure.
2. **The agent changed a setting the fault did not need.** Its goal named the probe's port, which
   on the test VM is a forwarded port (33050) and not the port sshd uses inside the server (22).
   The agent added `Port 33050` to `sshd_config`. The probe still passed, so the money side could
   not tell; the transcript could. The goal now names the server's own port, and the agent's
   rules forbid changing ports, listen addresses, users or other settings the fault does not
   need. In the clean run the agent changed one word on line 3 and created `/run/sshd`, and
   reported "Port and other settings were not changed."

## How the numbers were taken

- Times come from the ledger's events: the hold is the `HOLD_PENDING -> HELD` transition, the
  capture is PayPal's `COMPLETED` answer.
- Turns are model calls in the agent loop. Model spend is Keyhold's estimate from token counts at
  list price (`web/spend.py`); the calls went through an Anthropic-compatible endpoint
  (`ANTHROPIC_BASE_URL`), so the amount billed may differ.
- Judge-mode timing was on: the capture needs two probe passes at least 10 seconds apart, and the
  warranty re-probes every minute for 24 minutes.
- The test VMs run under CPU emulation (no KVM), which makes every console step slower than on a
  hosted server.
