# Live runs (2026-11-07 and 11-08, UTC)

Eleven runs against the real PayPal sandbox: the money check, seven rescues that captured and
three that the owner stopped. Every ID below can be looked up in the sandbox business account
that owns the REST app. Money is sandbox money.

| File | What ran | Result |
|---|---|---|
| [live-check-20261107T213209Z.md](live-check-20261107T213209Z.md) | `tools/live_check.py`: two $12.00 holds approved by a sandbox buyer, a $7.00 capture with `final_capture`, request-id replays, a refused second capture and a refused void after capture, a $2.00 refund, a void of the second hold, four `PayPal-Mock-Response` refusals | 22 passed, 0 failed, 1 skipped (webhook verification needs a public URL) |
| [rescue-first-a2b969fd.md](rescue-first-a2b969fd.md) | `/try` in live mode: the "Typo in sshd_config" test VM, Claude Haiku 5.5, a $9.00 hold approved through the JS SDK v6 button | captured $9.00 (capture `6WS22323FC4216719`) 2 min 28 s after the hold; warranty 24 checks of 24 passed |
| [rescue-hosted-53f4549c.md](rescue-hosted-53f4549c.md) | the same flow on the hosted Keyhold: the public URL, an x86-64 cloud VM with KVM, the judge link, a sandbox buyer approving on the public page | captured $9.00 (capture `7WP61145AR824644E`) 1 min 45 s after the hold; 7 agent turns; about $0.0039 of model spend; warranty 24 checks of 24 passed |
| [rescue-clean-3fcc3c58.md](rescue-clean-3fcc3c58.md) | the same flow after the two fixes below | captured $9.00 (capture `8WA56714MN162430M`) 1 min 48 s after the hold; 8 agent turns; about $0.0034 of model spend; warranty 24 checks of 24 passed |
| [rescue-hosted-8cf5a71c.md](rescue-hosted-8cf5a71c.md) | hosted, on the redesigned pages, with the webhook registered: "Typo in sshd_config" | captured $9.00 (capture `1FM0294205526573H`) 80 s after the hold; 7 agent turns; about $0.0030 of model spend; warranty 6 checks of 6 passed; 2 webhooks verified |
| [rescue-hosted-a19567da.md](rescue-hosted-a19567da.md) | hosted: "fstab lists a disk that is gone", the T2 fault | captured $19.00 (capture `37X1688089973222U`) 87 s after the hold; 12 agent turns; about $0.0051 of model spend; warranty 6 checks of 6 passed; 2 webhooks verified |
| [rescue-hosted-a44702f2.md](rescue-hosted-a44702f2.md) | hosted: the same fault, and the owner pressed **Stop** 6 s after the keyboard was lent | authorization `8VN59510G70260907` voided 1 s after Stop; $0 charged; the agent's only input was the console password, and nothing was typed after Stop; 2 webhooks verified, the second one `PAYMENT.AUTHORIZATION.VOIDED` |

| [rescue-hosted-e1d7502e.md](rescue-hosted-e1d7502e.md) | hosted, recorded from the owner's own browser: "Typo in sshd_config" | captured $9.00 (capture `4EU4081387193880G`) 76 s after the hold; warranty 6 of 6; 2 webhooks verified |
| [rescue-hosted-bf5a2f31.md](rescue-hosted-bf5a2f31.md) | the same, the run shown in the film: PayPal's own approval window, then Claude at the console | captured $9.00 (capture `0JP43729YN445900W`) 97 s after the hold; 8 agent turns; warranty 6 of 6; 2 webhooks verified |
| [rescue-hosted-9c26b96a.md](rescue-hosted-9c26b96a.md) | hosted: "fstab lists a disk that is gone", Stop pressed while Claude was typing a command | voided, $0 charged, but **a command already being typed finished after Stop**; see "What the Stop runs showed" below |
| [rescue-hosted-23ed2251.md](rescue-hosted-23ed2251.md) | the same fault after the fix, the Stop shown in the film | authorization `4XY289162B198180C` voided 0.9 s after Stop; $0 charged; nothing typed after Stop; 2 webhooks verified |

## What the Stop runs showed, and what changed

The first Stop recorded from the owner's browser (9c26b96a) found a real gap. The gate checked
the hold once per input, and QEMU sends a typed text in batches of 40 keys. Claude had started
typing a 140-character command (`cp /etc/fstab … ; cat /etc/fstab ; journalctl …`) a moment
before Stop. `revoke()` correctly waited for the input in flight, but that input was the whole
command, so its keys and its Enter went out over the next 5.7 seconds and it ran (a backup copy
and two read-only looks; nothing was changed). The page then said "Nothing was typed after
Stop", because it measured from the later `stop` event.

The fix: text under a hold now goes out in batches of at most 8 keys, each sent only after
`HoldLease.cut_now()` says the hold is still live (the Stop flag, and the ledger for a void or
capture started anywhere), and an Enter goes out on its own after one more check. A Stop or void
that lands mid-command cuts it, and the line is not entered (`InputCut`, logged as
`agent_type_cut`). The page now measures from the moment the lease was revoked, so 9c26b96a's own
page now says that one input that was already being sent finished after Stop. An independent
review of the fix raced Stop against typing 40 times: Stop waited at most 0.11 s and no key went
out after it. In the run after the fix (23ed2251) the hold was voided 0.9 s after Stop and
nothing was typed after it.

## Webhooks on real deliveries

The webhook was registered for the hosted URL with `tools/register_webhook.py` (ten event types)
and `PAYPAL_WEBHOOK_ID` set on the deployment. The seven hosted runs since then received fourteen
deliveries, and all fourteen passed `verify-webhook-signature` and were applied. The first three
runs received six:
`PAYMENT.AUTHORIZATION.CREATED` three times, `PAYMENT.CAPTURE.COMPLETED` twice and
`PAYMENT.AUTHORIZATION.VOIDED` once. All six passed `verify-webhook-signature` and were applied.
One arrived out of order: in run 8cf5a71c PayPal delivered the capture's event 6 s before the
authorization's, and the ledger, which never moves a state backwards, applied both without
changing the outcome.

Hosted timing: these three runs used a compressed warranty, 6 re-probes 20 s apart, so a judge
sees the whole plan finish in two minutes. The real plan is 24 hourly checks; the earlier runs
used 24 checks a minute apart.

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
  warranty re-probes every minute for 24 minutes (every 20 s, 6 times, in the last three runs).
- The two local runs used CPU emulation (no KVM), which makes every console step slower. The
  hosted runs used an x86-64 cloud VM with KVM.
