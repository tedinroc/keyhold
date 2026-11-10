# Keyhold: Devpost submission text

The text submitted on Devpost on 2026-11-09. Everything in it can be checked in this repository.

- Film (1:58): https://youtu.be/NKffZGEgC5w
- Live demo: https://keyhold-demo.pages.dev
- Code: https://github.com/tedinroc/keyhold

The live-run figures come from [docs/evidence/](evidence/README.md). Test counts below are from
`uv run pytest -q` on 2026-11-09: 1163 collected, 1160 passed, 3 skipped
(they boot real guests). `tests/test_doc_counts.py` fails while these counts, or the ones in the
README and the judge guide, differ from the suite's.

---

**Tagline:** Lend a broken server's console keyboard to an AI agent, with a PayPal authorization hold in charge of it. No hold, no keystrokes; only your server answering can charge you.

## Inspiration

I used to look after my own servers, and often that meant heading out in the middle of the night to fix one on site. If your server lives at a hosting provider, its web console saves the trip: a picture of the screen, a keyboard, and behind them, root. But someone still has to be awake to use it.

Often the cause is a configuration change: in [a 2026 survey](https://datacenter.uptimeinstitute.com/rs/711-RIA-145/images/2026.AnnualOutageAnalysis.pdf), 51% of data-center operators who had major IT system or software outages named configuration or change management among their most common causes.

An AI agent could read that screen and type the fix. But hand it root on trust? Pay for a failed attempt? Even shopping gives people pause: in [a 2026 survey PayPal co-released](https://www.commerce.com/en-GB/press/64-of-uk-consumers-want-to-use-agentic-ai-for-shopping-according-to-new-research-from-commerce-and-paypal/), 43% of UK online shoppers were concerned about "AI purchasing a product without the shopper's approval."

What would make yes reasonable? Money that has not moved yet. A PayPal authorization hold can be read anytime, voided anytime, and is no charge until captured. One hold can be the leash on the keyboard and the contract: proof of the fix, then payment.

That is the permission I want for any agent that acts or spends for me: visible, revocable with one press, paid only on proof.

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

- 1163 tests: 1160 pass, and 3 boot real guests on demand. None reach the network. A stateful
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
- Four more hosted runs recorded from the owner's own browser for the film: two $9.00 captures
  (the film's is `0JP43729YN445900W`, 97 seconds after the hold) and two Stops. The second Stop
  (authorization `4XY289162B198180C`) was voided 0.9 seconds after Stop with nothing typed after it.
- Webhooks on real deliveries: fourteen PayPal deliveries across the seven hosted runs since the
  webhook was registered, one out of order (a capture's event before its authorization's), and
  all fourteen passed `verify-webhook-signature` and were applied.
- `tools/live_check.py` against the sandbox: 22 checks passed, none failed (its webhook step ran
  before the deployment had a public URL; the real deliveries above cover it). It covers a partial capture with `final_capture`, request-id
  replays, a refused second capture, a refused void after capture, a partial refund, a void, and
  four mocked refusals.

## What we learned

- Recording a real Stop from the owner's browser found a gap no test had: the gate checked the
  hold once per input, so a long command Claude had started a moment before Stop finished
  typing, Enter included, 5.7 seconds after it. Now text goes out 8 keys at a time with the hold
  checked before each batch and before every Enter, so Stop (or a void from anywhere) cuts a
  command mid-line and it never runs. The page had also said "Nothing was typed after Stop"; it
  now measures from the moment Stop took the keyboard.

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

## What's next for Keyhold

- A `Console` adapter for a real hosting provider's console API.
- Keeping the console password from the model, which today sees it in order to type it.
- Probing from more than one place.

**Where it stands today, honestly:**

- Ten live rescues of two faults (eight on the hosted Keyhold): seven captured, three stopped by
  the owner and voided. The other faults have run on real guests, under KVM too, only with a
  simulated PayPal and a scripted agent.
- Keyhold drives a local test VM, not a hosting provider's console.
- On a real screen, Claude's evidence lines are not machine-checked.
- The keystroke firewall is a screen, not a sandbox.
- A void made outside Keyhold cuts the keyboard at the next PayPal read or verified webhook.

## Built with

python, fastapi, sqlite, paypal, paypal-server-sdk, paypal-js-sdk-v6, paypal-orders-v2, paypal-payments-v2, paypal-webhooks, claude, claude-haiku-5-5, anthropic, qemu, kvm, debian, pytest, hypothesis, uvicorn, httpx, docker, cloudflare-pages

## How it meets the main technology requirement

PayPal (sandbox). Every money step of the product is a PayPal call:
- PayPal JS SDK v6: the owner's "Pay with PayPal" button. Our server creates the order, so the browser never sends an amount; the owner approves in PayPal's own window.
- Orders v2, through the official paypal-server-sdk (2.4.0): create_order with intent AUTHORIZE for the fixed quote ($9, $19 or $29), authorize_order, get_order. The order's custom_id is the sha256 of the owner's own server check.
- Payments v2: get_authorized_payment is the keyboard lock. A keystroke reaches the console only after Keyhold has read the authorization from PayPal as CREATED, for at least the quote. It reads again every 5 inputs and once its last read is 15 seconds old, and text goes out 8 keys at a time, with a local Stop/void check (no PayPal call) before each batch and before Enter. capture_authorized_payment with final_capture: true runs only after the owner's own SSH login and web page check pass twice; void_payment on Stop, give-up or a time or turn limit; refund_captured_payment if the server falls over again during the warranty.
- PayPal-Request-Id on every money call (written to an outbox first and replayed by a reconciler); a client token from the OAuth token endpoint for the JS SDK; webhooks for PAYMENT.AUTHORIZATION, PAYMENT.CAPTURE and PAYMENT.REFUND events, verified with verify-webhook-signature (14 of 14 real deliveries verified); PayPal-Mock-Response for negative tests (tools/live_check.py: 22 checks passed against the sandbox).

AI: Claude Haiku 5.5 (claude-haiku-5-5), through the official Anthropic Python SDK (in the recorded runs, reached through an Anthropic-compatible endpoint set with ANTHROPIC_BASE_URL):
- Free look: Claude reads a screenshot of the broken server's console, names the fault from a fixed list of 14 classes and quotes the screen lines that show it (structured output). Code, not the model, sets the price from the class. A fault the console cannot fix gets no price and no hold. On the hosted demo, 11 free looks: all 9 quotes matched the planted fault, and both looks at the password-locked GRUB menu got no price and no hold.
- Rescue agent: a tool-use loop with six tools (screenshot, type, key, power_cycle, give_up, finished). Claude reads the console and types the fix; its thinking summaries show on the status page as its notes. None of its tools can move money, and a refusal or API error voids the hold. claude-opus-5-5 is one setting away.

How the two meet: the AI's access to a root console is decided by PayPal's live authorization state, and the money moves only when the owner's own check proves the fix.

## AI tools used

- Claude Haiku 5.5 (claude-haiku-5-5), through the official Anthropic Python SDK and an Anthropic-compatible endpoint: the product's runtime AI (the free-look diagnosis and the rescue agent).
- Claude (Claude Code): AI coding assistance during development: code, tests, reviews, docs, and the scripts that build the demo film.
- VoxCPM2 (open-source text-to-speech, Apache-2.0): the synthetic narrator voice in the demo film.
- Whisper large-v3-turbo (mlx-whisper, on device): word timings for the film's edit and captions.
- macOS Vision OCR (on device): finding the sandbox buyer's e-mail in every film frame so it could be blurred, and a final pass that fails the film if any e-mail address or account name is still readable.

## Testing instructions (summary)

Full steps: `docs/JUDGES.md`.

- Hosted: https://keyhold-demo.pages.dev. The Devpost testing instructions (judges only) give a
  one-click judge link that unlocks `/try` without the passcode, and the PayPal sandbox buyer
  login. On `/try`, "Typo in sshd_config" ($9) ends in a capture about 1 to 2 minutes after the
  hold; on "fstab lists a disk that is gone" ($19), pressing Stop within the first minute after
  approving voids the hold; "GRUB locked by a password" ends at the free look with no price and
  no hold. `/judge` lists every rescue with PayPal's ids and invariant checks.
- The hosted demo runs two test servers at once; after a capture the server stays with its run for
  about 2 minutes of warranty checks. Each address can start 6 runs per hour there (a cancelled
  run counts), and an unpaid quote expires after 15 minutes.
- No accounts: `uv sync`, `uv run python -m web`, then `http://localhost:8000/` (simulated PayPal
  and server, labelled on every page).
- Tests: `uv run pytest -q`.
- Sandbox check (needs your own PayPal sandbox app and buyer account):
  `uv run --env-file .env python tools/live_check.py`; see section 5 of `docs/JUDGES.md`.
