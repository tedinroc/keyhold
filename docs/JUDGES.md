# Testing Keyhold

Keyhold lends a broken server's console keyboard to an AI agent only while a PayPal authorization
hold is live, and captures the hold only after the owner's own check of the server passes. This
page lists what to click and what you should see. Money moves only in the PayPal sandbox (test
money), or in a simulated PayPal in offline mode. No real money moves.

## What you need

| Item | Value |
|---|---|
| Hosted app | `{{HOSTED_URL}}` |
| Passcode for `/try` | `{{TRY_PASSCODE}}` |
| PayPal sandbox buyer email | `{{SANDBOX_BUYER_EMAIL}}` |
| PayPal sandbox buyer password | `{{SANDBOX_BUYER_PASSWORD}}` |

The buyer is a PayPal sandbox personal account with sandbox test money. Use it only in the PayPal
window that the app opens, and allow pop-ups for the site if the browser asks.

The banners at the top of every page say what is real in this deployment:

- **PayPal sandbox**: orders, holds, captures, voids and refunds go to the PayPal sandbox.
- **Offline mode**: PayPal is simulated in memory (FakePayPal); there is no PayPal window.
- **Simulated server**: the broken server is a scripted console and the agent replays a recorded
  script, with no model call. Without this banner the server is a fresh test VM and the agent is
  Claude.

Timing is in judge mode: capture after two probe passes at least 10 seconds apart, and warranty
checks every minute for 24 minutes (each minute stands in for an hour). A deployment can shorten
the warranty with `KEYHOLD_WARRANTY_CHECKS` and `KEYHOLD_WARRANTY_INTERVAL_S`; 6 checks 20 seconds
apart (about 2 minutes) keep one capture from holding a shared test server for 24 minutes. The
pages state the plan in force, for example "re-checked every 20 s, 6 times (compressed for the
demo; the real plan is 24 hourly checks)".

## 1. Capture: the server is fixed, so the hold is charged (T1, $9)

1. Open `{{HOSTED_URL}}/`. It explains the lock on one screen.
2. Open **Try a rescue** (`/try`), enter the passcode and press **Unlock**.
3. Press **Break this server** on the **Start here** card, **Typo in sshd_config**.
4. Every run and status page leads with one sentence and the progress strip Quote, Hold,
   Keyboard, Probe, then Charged or Voided, with a plate under it that says whether the keyboard
   is locked or lent, and why. The run page boots a fresh copy of the broken server and takes the
   free look: screenshots only, no key is pressed. When the quote is ready you see:
   - $9.00, fixed price, tier T1, and the diagnosis;
   - **Seen on the screen**: the evidence quoted from the screen, for example
     `[FAILED] Failed to start ssh.service - OpenBSD Secure Shell server.`, over the free-look
     screenshot cropped to its text;
   - **Plan** and **Audit details** (closed folds): the plan, the fault class `sshd_config_error`,
     the quote hash, and the owner probe fixed for this rescue (SSH user `probe`, the probe key's
     fingerprint, the page check with its expected text or sha256, and the spec hash that is sent
     as the order's `custom_id`).
5. Press the PayPal button in **Approve a $9.00 hold**. The sandbox buyer login is right next to
   it, with Copy buttons. In the PayPal window, log in and approve the payment. You have 15
   minutes before an unpaid quote expires.
6. The page reports the order, then that PayPal holds $9.00, and opens the live status page.
7. On the status page:
   - The plate reads **Keyboard lent: hold live**, with PayPal's last answer, when it was checked
     and how many checks so far. The money box shows $9.00 on hold with the one **Stop** button.
   - **Key moments**, beside the console, follows the lock checks, the last few lines the agent
     typed (the console password shows as `[console password]`), firewall refusals, the probe and
     PayPal's answers. **Full log** (a fold) lists every row: each gate check ("PayPal authorization
     … CREATED $9.00: keyboard allowed"), every line typed, the agent's notes and every probe.
   - After the agent calls `finished`, the probe runs every 5 seconds. Two passes at least 10
     seconds apart, with no failure between, lead to the capture.
   - **PayPal, read live** shows each object as PayPal returns it now: its id, PayPal's own status
     word, what that status means, the amount, **PayPal answered** (the time of the API answer,
     the first tick) and **Webhook** (the verified webhook, the second tick; the order has none).
     After the capture: the authorization `CAPTURED` and a capture `COMPLETED` for $9.00 with
     `final_capture`. Sandbox webhooks can arrive well after the API answer; the page keeps
     streaming for up to 3 minutes after the run ends to catch them.
   - **Audit details** (a fold) holds each call's `PayPal-Request-Id` (`order-…`, `auth-…`,
     `cap-…` followed by the rescue id), the full ids, the owner probe and the **Ledger checks**
     (no invariant problems).
8. The page then reads **Fixed. Charged $9.00.** and tells **How your $9.00 moved**. The warranty
   re-probes the server on the plan the page states. You do not need to wait: it keeps running on
   the server and `/judge` shows the outcome later (`WARRANTY_OK`, or `REFUNDED` after two failed
   checks in a row). The page offers the next path; while the warranty keeps a single test VM, its
   button says when that path opens, and `/try` marks the Stop path card **Next**.

With the recorded agent in offline mode, approval to capture took 21 seconds in our run. With
Claude on a test VM it takes longer: each turn is one model request plus a screenshot, up to 30
turns and 20 minutes.

## 2. Stop: the owner takes the keyboard back, so the hold is voided (T2, $19)

1. Go back to `/try` and press **Break this server** on **fstab lists a disk that is gone**. The
   quote is `fstab_bad_entry`, T2, $19.00.
2. Approve the hold in the PayPal window as before.
3. While the agent is typing, press **Stop and void the hold** on the status page. One press is
   enough. Stop also works while the probe runs, as long as the capture has not happened. The
   recorded agent of the simulated world is fast: press Stop within about 20 seconds of approving.
4. The page reads **Stopped. $0 charged.** Key moments show "You pressed Stop. Keyboard locked,
   void sent." and then the hold released. The **Full log** has "keyboard locked: the owner pressed
   Stop", the void with `PayPal-Request-Id void-…`, and the state change in words (the raw
   `ACTIVE -> VOIDED` is its tooltip and is in **Audit details**). **PayPal, read live** shows the
   authorization `VOIDED`. No capture exists. The void's second tick appears when the
   `PAYMENT.AUTHORIZATION.VOIDED` webhook is verified.

In the simulated world the recorded agent types `mkfs.ext4 /dev/vdb` a few steps in. The
keystroke firewall refuses it before it reaches the console; the refusal shows as a callout above
**Key moments** and in the **Full log**.

## 3. Honest stop: a fault the console cannot fix gets no hold

1. On `/try`, press **Break this server** on **GRUB locked by a password**.
2. The free look should read the GRUB `Enter username:` prompt and answer `console_locked`. The
   page then says **No hold offered**: nothing is held, nothing is charged, and no PayPal button
   is shown.

If the model names a priced class instead, a hold is offered; the agent is then expected to call
`give_up`, which voids it. That path (a hold, then `give_up`, then a void) is covered by
`tests/e2e/test_rescue_e2e.py::test_give_up_voids_the_hold` and by the real-guest proof in
[REAL_VM.md](REAL_VM.md).

## 4. The ledger

- `{{HOSTED_URL}}/judge` lists every rescue first: fault, time, the outcome in words (the raw
  state is in its tooltip), the amount, the API and webhook ticks, and its invariant checks, with
  the order, authorization, capture and refund ids in a fold per row. Under the table: totals
  held, captured and refunded, a bookkeeping line (invariant problems, money calls in flight),
  the runs that were offered no hold, and a fold with the webhook outcomes (`applied`,
  `duplicate`, `unverified` and others) and the reconciler's last pass.
- `{{HOSTED_URL}}/judge.json` is the same data as JSON.
- `{{HOSTED_URL}}/healthz` reports the mode, the world, whether webhooks can be verified, money
  calls in flight, and unprocessed or unverified webhook deliveries.

## 5. Optional: check the PayPal sandbox yourself

This needs your own PayPal developer account (a sandbox REST app and a sandbox personal account),
but not the hosted app, a VM or a model. Test money only.

```
uv sync
cp .env.example .env      # fill in PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET
uv run --env-file .env python tools/live_check.py
```

The script uses Keyhold's own ledger (`Payments`, with its outbox and request ids) and
`PayPalClient`, and refuses any PayPal environment but the sandbox.

1. It prints a PayPal link for a $12.00 hold. Open it, log in with the sandbox personal account
   and approve. PayPal may leave the page where it is afterwards; the script reads the order every
   5 seconds and moves on when it reads `APPROVED` (it waits up to 15 minutes).
2. The first hold's steps then run on their own. Each line starts with PASS, FAIL or SKIP (SKIP:
   a step it needs did not pass).
3. A second link appears, for a second $12.00 hold. Approve it the same way. That hold is voided.
4. The negative tests and the ledger check follow, and the last lines name the run folder.

What a PASS means for the steps that matter most:

| Step | PASS means |
|---|---|
| `hold1_lease_grants` | `HoldLease` read the authorization from PayPal and granted input: `CREATED`, $12.00, and this rescue's order id, `custom_id` (probe spec hash) and invoice id all came back |
| `capture_7_final` | $7.00 captured with `final_capture: true` through the ledger (`cap-…` request id); the rescue is `CAPTURED` |
| `auth_after_final_capture` | PayPal shows the authorization as `CAPTURED` or `PARTIALLY_CAPTURED`; the line gives its exact `status` and `status_details`, and says when that status differs from the one the ledger recorded (`summary.md` lists both even when the step fails) |
| `capture_replay_same_id` | the same capture call with the same `PayPal-Request-Id` returned the same capture, and PayPal lists one capture |
| `second_capture_refused` | a capture of the $5.00 that `final_capture` released was refused; the line names PayPal's issue code |
| `void_after_capture_refused` | a void of the captured authorization was refused |
| `refund_2` | $2.00 refunded (a replay returned the same refund); the ledger read it back and the rescue stays `CAPTURED` |
| `hold2_voided` | the second hold was voided through the ledger (`void-…`); PayPal shows `VOIDED` |
| `mock_*` (four steps) | with `PayPal-Mock-Response`, authorize, capture, void and refund each failed with the issue asked for, and `PayPalClient` reported it as a refusal |
| `mocks_moved_no_money` | after the negative tests PayPal lists the same authorizations, captures and refunds on the first order, and the same status for the second hold, as before them |
| `ledger_invariants` | no money invariant problem in either rescue |

The run folder `runs/live-check/<UTC time>/` holds `summary.md` (one line per step, then
PayPal's exact answers), `report.json` (every id and request id) and the check's ledger
(`ledger.sqlite3`). To check webhook verification too, set `PAYPAL_WEBHOOK_ID` and add
`--webhook-event` with the app's ledger (`var/keyhold.db`, newest stored delivery) or a JSON file
holding a received delivery's `headers` and `body`; `--webhook-only` skips the holds.

## If something gets in the way

- **The test server is in use**: there is one test server at a time. A test VM stays up through
  its warranty, because the warranty re-probes it. `/try` names the run that holds it, links to
  **Watch it live**, says when a warranty or an unpaid quote frees it, and turns the buttons back
  on by itself. When the warranty is your own, it says so, with the check count and the time.
- **Runs left this hour**: each address may start a few runs per hour (4 unless the deployment
  sets `KEYHOLD_TRY_RUNS_PER_HOUR`; `/try` shows how many are left). A run cancelled before its
  hold counts too. `/try` marks the paths this browser has already done, so the next one is the
  marked card. Once the runs are used, `/try` turns the buttons off, says when the next run is
  allowed, and links to finished runs.
- **Model budget**: `/try` stops starting runs when the model budget for the last 24 hours is
  nearly used up.
- **Quote expired**: an unpaid quote releases the server after 15 minutes. Nothing was held.
- **PayPal window closed**: the page says nothing is held. Press the PayPal button again.

## How this maps to the judging criteria

Each row names something you can check in the repository or on `/try`.

| Criterion | What to look at |
|---|---|
| Technological Implementation | PayPal through the official Server SDK (`keyhold/pay/client.py`): Orders v2 with intent AUTHORIZE, authorize, capture with `final_capture`, void and refund. Every money call carries a PayPal-Request-Id that is written to an outbox before the call (`keyhold/pay/ledger.py`), and a reconciler replays it after a crash (`keyhold/pay/reconciler.py`). Webhooks are verified with `verify-webhook-signature`, deduplicated and never move a rescue backwards (`keyhold/pay/webhooks.py`). Checkout uses JS SDK v6 (`web/static/checkout.js`). Claude runs through the official SDK with structured output, refusal handling and server-side fallback (`keyhold/agent/model.py`). Property tests, SIGKILL chaos tests and real Debian guests (`docs/REAL_VM.md`) back the money rules. |
| Design | The lock is PayPal's own state, not a flag in Keyhold's database: before input, and again every few actions, the gate reads the authorization back from PayPal and cuts the keyboard if it is not a live hold of the quoted amount (`keyhold/pay/gate.py`). Stop is a void. The status page shows the order, authorization and capture ids as PayPal reports them, next to every keystroke and every firewall refusal. |
| Potential Impact | A server owner locked out of SSH gets a fixed price before anyone touches the keyboard, pays only if their own check of their own server passes, and gets a full refund if it falls over again within the warranty. The agent never sees the probe's private key and has no tool that can charge (`keyhold/agent/loop.py`, `keyhold/probe/owner.py`). |
| Innovation | A PayPal authorization hold works as the permission for an AI agent's keyboard: no live hold, no keystrokes. The payer owns the only capture trigger. A fault the console cannot fix ends without a charge: no hold if the free look sees it (the GRUB fault on `/try`), a void if it shows only after the hold. |
| Presentation | On `/try` you break a fresh server, read the quote and its evidence, approve the sandbox hold yourself and watch it end in a capture or a void. `/judge` lists every run with its PayPal ids. Sections 1 to 3 above take about ten minutes. |

## What the browser cannot trigger, and where it is tested

The test suite runs without any account or network (`uv run pytest -q`: 1156 tests, 1153 pass, 3
are skipped because they boot real guests).

| Case | Test |
|---|---|
| The server falls over during the warranty: full refund | `tests/e2e/test_rescue_e2e.py::test_relapse_during_the_warranty_refunds_in_full` |
| PayPal voids the hold mid-run: the keyboard is cut | `tests/e2e/test_rescue_e2e.py::test_a_hold_voided_at_paypal_mid_run_cuts_the_keyboard` |
| Crash between the outbox write and PayPal's answer: exactly one capture | `tests/pay/test_pay_chaos.py` (kills a child process with SIGKILL) |
| The probe flaps: no capture | `tests/e2e/test_rescue_e2e.py::test_a_probe_that_never_passes_stably_never_captures` |
| Webhooks replayed in any order: no state change | `tests/pay/test_pay_properties.py::test_webhook_replays_never_change_state` |
| Random mix of errors, crashes, webhooks and reconciler passes: every money invariant holds | `tests/pay/test_pay_properties.py::TestMoneyMachine` |
| The sandbox check of section 5, run end to end against FakePayPal with a scripted approval, and against variants that ignore mock headers or keep a hold open after `final_capture` (each must be reported as a FAIL) | `tests/pay/test_live_check.py` |

## Running it yourself without accounts

```
uv sync
uv run python -m web
```

Then open `http://localhost:8000/`. With no PayPal credentials the app runs in offline mode with
the simulated server, so no account, key or network is needed, and `/try` needs no passcode. The
same three faults work. Instead of the PayPal window there is an **Approve the hold (simulated
PayPal)** button that goes through the same server endpoints. FakePayPal sends signed webhook
events about 4 seconds later, through the same verification path, so the second ticks show too.
The README explains how to run against the PayPal sandbox.
