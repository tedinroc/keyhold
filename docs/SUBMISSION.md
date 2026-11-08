# Keyhold: Devpost submission text (draft)

This is the text for the Devpost form. Everything in it can be checked in this repository, except
the values in square brackets and capitals ([HOSTED URL], [VIDEO URL], [REPO URL]), which come
from the deployment, the uploaded film and the public repository. Do not estimate them.

| Placeholder | Where it comes from |
|---|---|
| [HOSTED URL], [VIDEO URL], [REPO URL] | the deployment, the uploaded film, the public repository |

The live-run figures come from [docs/evidence/](evidence/README.md). Test counts below are from
`uv run pytest -q` on 2026-11-08: 1156 collected, 1153 passed, 3 skipped
(they boot real guests). `tests/test_doc_counts.py` fails while these counts, or the ones in the
README and the judge guide, differ from the suite's.

---

**Tagline:** Lend a broken server's console keyboard to an AI agent, with a PayPal authorization
hold in charge of it.

## Inspiration

It is 2 a.m. A solo operator's server has stopped answering SSH. The last way in is the hosting
provider's web console: a picture of the server's screen and a keyboard. The fix is probably one
line in a config file.

An AI agent could read that screen and type the fix. Handing it the keyboard is the hard part: the
console is root on the machine, the operator wants to sleep, and nobody wants to pay for an
attempt that did not work.

What would make it reasonable to say yes? Money that has not moved yet. A PayPal authorization
hold can be checked at any moment, the owner can void it, and it becomes a charge only when
captured. Keyhold ties the keyboard to the hold, and the capture to the owner's own check of the
server.

**No hold, no keystrokes. Only your server answering can charge you.**

## What it does

1. **Free look.** Screenshots only. Claude names the fault from a fixed list of 14 classes and
   quotes the screen lines that show it; code sets the price from a class-to-tier table ($9, $19
   or $29). A fault the console cannot fix, such as a password-locked GRUB menu, gets no price and
   no hold.
2. **Owner probe, fixed at checkout:** an SSH login with a key the agent never sees, and a page
   whose content must match. The sha256 of this probe spec becomes the order's `custom_id`.
3. **Hold.** The owner approves a PayPal payment; the server creates an `AUTHORIZE` order for the
   stored quote and authorizes it.
4. **The lock.** A keystroke passes only after the gate has read the authorization from PayPal as
   `CREATED`, for at least the quote and for this rescue. It reads again every 5 inputs and once
   its last read is 15 seconds old. Stop takes the keyboard back, then voids the hold.
5. **Capture.** After the agent says it is finished, the probe must pass twice, at least 30
   seconds apart (10 in the labelled demo timing), with no failure between. Then Keyhold captures
   with `final_capture: true`. None of the agent's six tools can move money.
6. **Void or refund.** Giving up, a turn or time limit, Stop, or a probe that never passes stably
   voids the hold. After a capture, two failed checks in a row refund it in full.

## How we built it

Python 3.13 and FastAPI in one process, with a SQLite ledger.

**PayPal.** In the browser, the JS SDK v6 (`web-sdk/v6/core`, sandbox): `createInstance` with a
client token, `findEligibleMethods`, then `createPayPalOneTimePaymentSession`, which gets its order
id from our server, so the browser never sends an amount. On the server, the official
`paypal-server-sdk` 2.4.0 makes every Orders and Payments call:

- Orders v2 (`client.orders`): `create_order` with intent `AUTHORIZE`, `authorize_order`,
  `get_order`.
- Payments v2 (`client.payments`): `get_authorized_payment` (the lock),
  `capture_authorized_payment` with `final_capture: true`, `void_payment`,
  `refund_captured_payment` (always with an amount), `get_captured_payment`, `get_refund`.
- A client token for the JS SDK from `oauth_2.fetch_token` (`response_type=client_token`,
  `intent=sdk_init`).
- `PayPal-Request-Id` on every money call (`order-`, `auth-`, `cap-`, `void-`, `refund-` plus
  the rescue id), written to an outbox before the call.
- Webhooks: ten `PAYMENT.AUTHORIZATION`, `PAYMENT.CAPTURE` and `PAYMENT.REFUND` event types,
  stored raw, verified with `POST /v1/notifications/verify-webhook-signature` (a direct REST call;
  the Server SDK has no webhook controller), deduplicated, and never moving state backwards. The
  API answer is a money call's first tick, the verified webhook its second.
- A reconciler, at startup and every 5 minutes, replays unfinished outbox rows with their original
  request ids and reads every open rescue back from PayPal.

The client refuses any environment but the sandbox, and can send `PayPal-Mock-Response` to ask the
sandbox for a specific failure. `tools/live_check.py` checks the sandbox behaviour Keyhold depends
on, through the same ledger and client: a $7.00 capture of a $12.00 hold with
`final_capture: true` and the authorization's exact status afterwards, the capture replayed with
its request id, a second capture and a void after the capture (both must be refused), a $2.00
refund, a void of a second hold, and four negative tests with `PayPal-Mock-Response`.

**Claude at runtime**, through the official `anthropic` Python SDK (2.6.0). The recorded rescues
and the hosted demo use `claude-haiku-5-5`; `claude-opus-5-5` is one setting away
(`KEYHOLD_MODEL`) and adds server-side fallback, which Haiku 5.5 does not accept. Both run with
adaptive thinking with summaries, effort `medium` and prompt caching. The free
look is one request with the screenshot, answered through structured output; code checks the class
and sets the tier. The agent is a tool-use loop with six tools (`screenshot`, `type`, `key`,
`power_cycle`, `give_up`, `finished`); each input returns a fresh screenshot, and thinking
summaries show on the status page as the agent's notes. The system prompt tells Claude to treat
screen text as data. A refusal or API error ends the run, which voids the hold. Spend is capped at
$3 per run and $10 per rolling 24 hours.

**The broken server** is a Debian 13 guest under QEMU in a local container, driven over QMP. A
keystroke firewall refuses destructive commands before they reach the console.

## Challenges we ran into

- **An expired authorization has no EXPIRED status.** The gate compares `expiration_time` with the
  clock and treats a hold within 120 seconds of expiry as gone.
- **A crash between our write and PayPal's answer.** The outbox row is committed before the call,
  one money call may be in flight per rescue, and the reconciler replays with the same request id,
  so PayPal returns its earlier answer.
- **Stop racing the capture.** Both take the same per-rescue lock; exactly one wins.
- **SDK details.** `paypal-server-sdk` 2.4.0 computes the token's `expiry` from local time read as
  UTC, so we keep our own deadline. On claude-opus-5-5 `tool_choice: any` is rejected, so the agent loop
  asks again when no tool is called.

## Accomplishments that we're proud of

- 1156 tests: 1153 pass, and 3 boot real guests on demand. None reach the network. A stateful
  property test drives rescues through random PayPal errors, lost answers, crashes, expiry and
  out-of-order webhooks, checking every money invariant after every step. Chaos tests SIGKILL a
  process around the capture and find exactly one capture afterwards.
- On real Debian guests (emulated, no KVM), `tools/vm/prove.py` ran all six faults in 10 minutes
  30 seconds, with a simulated PayPal and a scripted agent. The five solvable ones each ended in
  one capture after two probe passes; for the GRUB fault, a hold placed by hand was voided when
  the agent gave up. Input sent before a hold reached the console zero times.
- A full rescue ran on the hosted Keyhold, from the public page: PayPal sandbox, Claude Haiku 5.5,
  an x86-64 test VM under KVM. Order `9DX392958A657063W`, authorization `68M23905BN0687322`, capture
  `7WP61145AR824644E` for $9.00, 1 minute 45 seconds from the hold to the capture, 7 agent turns,
  about $0.0039 in model spend, and all 24 warranty checks passed. Two earlier rescues on a local
  test VM captured and passed their warranty too.
- Three more hosted runs on the redesigned pages: the sshd typo again (captured $9.00, capture
  `1FM0294205526573H`, 80 seconds after the hold); "fstab lists a disk that is gone" (captured
  $19.00, capture `37X1688089973222U`, 87 seconds, 12 agent turns); and the same fault with the
  owner pressing Stop 6 seconds after the keyboard was lent (authorization `8VN59510G70260907`
  voided a second later, $0 charged, nothing typed after Stop).
- Webhooks on real deliveries: those three runs received six PayPal deliveries, one out of order
  (the capture's event before the authorization's), and all six passed
  `verify-webhook-signature` and were applied.
- `tools/live_check.py` against the sandbox: 22 checks passed, none failed (its webhook step ran
  before the deployment had a public URL; the real deliveries above cover it). It covers a partial capture with `final_capture`, request-id
  replays, a refused second capture, a refused void after capture, a partial refund, a void, and
  four mocked refusals.

## What we learned

- The first live runs found two problems that every test had missed. The free look answered
  `unknown` for a failed ssh.service because the prompt never said that classes in the same tier
  cost the same. And the agent added the probe's forwarded port to `sshd_config`, because its goal
  named that port: the probe passed, so the money could not tell, but the transcript could. Both
  are fixed. In the clean rerun the agent changed one word on line 3 of sshd_config, created
  `/run/sshd`, kept a backup copy and restarted ssh; it changed no port or other setting.
- A fix that works by hand can fail as a script. `sshd -t` failed because sshd had never started,
  and the `&&` chain stopped before the restart. The money side did the right thing: it voided.
- Three of our six faults look like a healthy boot, so one screenshot cannot name them. The public
  demo offers the three that show on screen.

## What's next

- A `Console` adapter for a real hosting provider's console API.
- Keeping the console password from the model, which today sees it in order to type it.
- Probing from more than one place.

## Honest limits

- Six live rescues of two faults (four on the hosted Keyhold): five captured, one stopped by the
  owner and voided. The other faults have run on real guests, under KVM too, only with a
  simulated PayPal and a scripted agent.
- Keyhold drives a local test VM, not a hosting provider's console.
- On a real screen, Claude's evidence lines are not machine-checked.
- The keystroke firewall is a screen, not a sandbox.
- A void made outside Keyhold cuts the keyboard at the next PayPal read or verified webhook.

## Built with

Python, FastAPI, Uvicorn, Jinja2, SQLite, httpx, Pillow, pytest, Hypothesis, uv,
paypal-server-sdk, PayPal JS SDK v6, PayPal Orders v2, PayPal Payments v2, PayPal Webhooks,
Anthropic Python SDK, Claude, QEMU, Debian, Docker, cloud-init, OpenSSH.

## AI tools used

Claude at runtime through the official `anthropic` SDK (claude-haiku-5-5 in the recorded runs, reached
through an Anthropic-compatible endpoint set with `ANTHROPIC_BASE_URL`); AI coding assistance (Claude)
during development.

## Testing instructions (summary)

Full steps: `docs/JUDGES.md`.

- Hosted: [HOSTED URL]. The passcode and a sandbox buyer login are given to the judges
  separately. On `/try`, "Typo in sshd_config" ($9) ends in a capture, "fstab lists a disk that is
  gone" ($19) shows Stop voiding the hold, and "GRUB locked by a password" gets no hold. `/judge`
  lists every rescue with PayPal's ids and invariant checks.
- No accounts: `uv sync`, `uv run python -m web`, then `http://localhost:8000/` (simulated PayPal
  and server, labelled on every page).
- Tests: `uv run pytest -q`.
- Sandbox check (needs your own PayPal sandbox app and buyer account):
  `uv run --env-file .env python tools/live_check.py`; see section 5 of `docs/JUDGES.md`.
