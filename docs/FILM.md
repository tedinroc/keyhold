# Keyhold: the film (2:50)

A shot list, voiceover and recording plan for the submission video. Length 2:50 (170 seconds).
The hook lands in the first 10 seconds, the product is on screen at 0:20, and the middle of the
film is one uncut take of a real rescue in the PayPal sandbox: hold, console, probe, capture.

Values in square brackets and capitals, such as [REAL CLAUDE RUN TIME], are not known yet. They
come from the live runs below. Nothing on screen may be mocked up: every id, amount, status and
console screen comes from a real run, and every claim in the voiceover can be checked in this
repository or on screen. The 2 a.m. opening is a scenario, not a recorded event.

## Runs to record

The web app runs one test VM at a time, and a captured rescue keeps its VM for the 24-minute
warranty (judge mode). Record in this order so each run frees the VM for the next.

| Run | What | Settings | Used in |
|---|---|---|---|
| A | "Typo in sshd_config" ($9) through capture, then the server stopped during the warranty | live mode, `KEYHOLD_WORLD=vm`, judge mode | shots 1, 2, 5, 6, 9 |
| B | "fstab lists a disk that is gone" ($19), Stop pressed while the agent types | same | shot 7 |
| C | "GRUB locked by a password", free look only | same | shot 8 |
| D | `uv run python tools/vm/prove.py grub-password` in a terminal | real test VM, simulated PayPal, scripted agent | shot 8 |
| E | a rescue killed during the capture and restarted (see shot 10) | live mode, needs a demo pause that is not built yet | shot 10 |

Before run A:

- `.env` holds the sandbox app's credentials and `PAYPAL_WEBHOOK_ID`, and the app is reachable
  over HTTPS so webhooks verify (without them the second ticks never show). `/healthz` should say
  webhooks can be verified.
- Raise `KEYHOLD_MODEL_SPEND_CAP_USD` and `KEYHOLD_TRY_RUNS_PER_HOUR` for the recording day if
  rehearsals are needed; the defaults ($10 per 24 hours, 4 runs per hour) stop runs otherwise.
- Log the sandbox buyer in to PayPal in the recording browser profile beforehand, so the
  password is never typed on camera. If PayPal asks again during a take, blur the field.
- Run `uv run --env-file .env python tools/live_check.py` once with the same `.env` (README,
  "PayPal sandbox", step 4). It needs two buyer approvals and keeps its own ledger under
  `runs/live-check/`, so its two holds are not rescues on `/judge`. Read its `summary.md` first: a
  FAIL in a hold, capture, void or refund step means run A may not go as planned.
- Rehearse run A once off camera and note [REAL CLAUDE RUN TIME] (approval to capture). It sets
  the playback speed of the uncut take.
- Terminal commands in shots 1, 9 and 10 run on the machine that runs Keyhold and its test VMs.
- Record shot 1 in its own recording while run A's quote waits for checkout (an unpaid quote
  lasts 15 minutes), then start the browser recording for shots 5 and 6 on the quote.

## Shot list

"Live" means a screen recording of the real run as it happened. "UI" means a still or short
hold captured from a real run's pages after the fact. "Edit" means text built in the edit.

| # | Time | Picture | Source tag | How |
|---|---|---|---|---|
| 1 | 0:00-0:05 | Terminal, dark, large type, prompt `$ ` only. `ssh web1` (an alias in the SSH config for run A's forwarded port) fails with whatever ssh really prints. | test VM | Live |
| 2 | 0:05-0:10 | Hard cut to run A's console screen at boot, the `[FAILED] Failed to start ssh.service` line readable. Still frame. | test VM | UI (run A's free-look screenshot) |
| 3 | 0:10-0:20 | Black. "An AI agent could type the fix." lands, then "That keyboard is root." lands under it, each on its VO line. | | Edit |
| 4 | 0:20-0:30 | Keyhold's home page: "No hold, no keystrokes. Only your server answering can charge you." At 0:26, cut to `/try` with "Typo in sshd_config" and its $9 price. | | UI |
| 5 | 0:30-0:46 | Run A's quote: `sshd_config_error`, T1, $9.00, the evidence line, the quote hash. At 0:38, hard cut to the owner probe panel: user `probe`, key fingerprint, page check, spec hash sent as `custom_id`. | PayPal sandbox, Claude, test VM | Live (opening of the uncut recording) |
| 6 | 0:46-1:44 | **The uncut take** (below). | PayPal sandbox, Claude, test VM | Live, one take |
| 7 | 1:44-2:00 | Run B's status page while the agent types. Press "Stop and void the hold" once. Key moments and the Full log fold show: "keyboard locked: the owner pressed Stop", the void with `void-…`, `ACTIVE -> VOIDED`; "PayPal, read live" shows the authorization `VOIDED`. 1x around the click. | PayPal sandbox, Claude, test VM | Live |
| 8 | 2:00-2:18 | Run C: the free look answers `console_locked` and the page says "No hold offered" (to 2:08). Cut to run D's terminal: the hold line (order, authorization, $19.00), then "rescue ended: VOIDED: the agent gave up: …". | run C: PayPal sandbox, Claude, test VM. Run D: test VM, simulated PayPal, scripted agent | Live |
| 9 | 2:18-2:30 | Run A again, in its warranty. Terminal: `docker stop` of run A's VM (`docker ps --filter label=keyhold.vm`). Status page: two warranty checks fail, a refund with `refund-…`, the rescue `REFUNDED`, and the refund under "PayPal, read live". Rehearse once off camera. | PayPal sandbox, test VM | Live, time-lapse |
| 10 | 2:30-2:42 | Run E (see "Shot 10" below): the status page after the second probe pass, `kill -9` in a terminal, the restart, then `/judge` with one capture for the rescue and its `cap-…` request id. | PayPal sandbox, test VM | Live |
| 11 | 2:42-2:50 | Black. "No hold, no keystrokes." then "Only your server answering can charge you." Under them, fixed: "Keyhold", "PayPal Orders v2, Payments v2, webhooks, JS SDK v6, Server SDK. Claude.", [REPO URL]. | | Edit |

The source tag sits in the same corner, at the same size, on every recorded shot. It says what is
real in that shot and nothing else.

### Shot 6: the uncut take

One continuous screen recording from the press of the PayPal button to the capture, with no cut.
Shot 5 comes from the same recording, before the take begins. From the button press on, the edit
may change speed but never skip time:

| Part | Speed | What must be readable |
|---|---|---|
| PayPal window, approve | 1x | the amount, $9.00 |
| Run page: the order (intent `AUTHORIZE`, $9.00), then the authorization id. Status page: the authorization `CREATED` $9.00 under "PayPal, read live", and the gate line "PayPal authorization … CREATED $9.00: keyboard allowed" | 1x | the order id and authorization id for at least 2 seconds |
| Claude at the console: typed lines, console thumbnails, the agent's notes | [REAL CLAUDE RUN TIME] / about 25 s, for example 8x | nothing in particular; the console visibly changes |
| `finished`, then the owner probe: results until two passes at least 10 s apart | 2x | each probe line as it lands |
| Capture: authorization `CAPTURED`, capture `COMPLETED` $9.00, `final_capture` true, `cap-…` under Audit details, "PayPal-Request-Ids: two ticks per money call" | 1x, then hold 3 s on the last frame | the capture id and `cap-…` |

- Every speed change is shown by the speed label (for example "8x"), and a running clock of real
  elapsed time sits beside it. Both stay in one place and update in place with tabular figures.
- Arrange the browser before the take so the parts above are on screen without scrolling. The
  status page works at phone width, so two windows of it side by side, each at a different
  section, is one way. Do not scroll during the take.
- Sandbox webhooks can take 8-25 seconds and sometimes a minute or two. If the capture's second
  tick lands inside the take, keep it. If not, do not wait for it in the take; the status page
  keeps streaming for 3 minutes after the run, and a later still of the ticks table can go in
  shot 10 or 11 as UI.
- The VO line about what Claude typed must match what this take shows. Write it after the take.

### Shot 10: kill after the probe passes, one capture

What this shot must show: the probe passed, PayPal made the capture, Keyhold died before it
recorded the answer, and after a restart there is still exactly one capture.

How the code behaves today: the rescue runs on a thread inside the one web process, so the kill
is `kill -9` of that process. At startup the reconciler replays an unfinished capture with its
original request id `cap-{rescue_id}`, and PayPal answers with the capture it already made. But a
kill that lands after the probe passes and before the capture's outbox row is written ends
differently: the startup pass treats the rescue as orphaned and voids the hold (nothing is
charged). The window between the outbox write and PayPal's answer is one HTTP call long, too
short to hit by hand.

So this shot needs, before recording, a small opt-in pause that does not exist at this commit:
in judge mode only, behind its own setting, wait a few seconds after PayPal answers the capture
and before the ledger records it, and say so on the status page. The source tag for the shot then
adds "kill timed with a demo pause". Rehearse it twice, including what the restarted process does
with run E's VM.

If the pause is not built, use the fallback: record `uv run pytest tests/pay/test_pay_chaos.py -v`
in a terminal. Six of its seven tests kill a child process with SIGKILL around the capture
(before the call, after PayPal made the capture, halfway through the ledger write, and repeated
kills), then check for exactly one capture; the seventh is the no-kill control. Source
tag: "test suite, simulated PayPal". Use the fallback VO line below.

## Voiceover

About 130 words per minute. Each line starts at its shot's time and must end at least a second
before the next shot. Total: about 265 words, roughly 2 minutes of speech in 2:50.

| Shot | Time | Words | Line |
|---|---|---|---|
| 1-2 | 0:00 | 19 | Two a.m. Your server stopped answering SSH. The last way in: the web console. A screen and a keyboard. |
| 3 | 0:10 | 16 | An AI agent could type the fix. But that keyboard is root, and you are asleep. |
| 4 | 0:20 | 15 | Keyhold puts a PayPal authorization hold in charge of that keyboard. No hold, no keystrokes. |
| 5 | 0:30 | 31 | It starts with a free look: screenshots, no keys. Claude names the fault and quotes the line that proves it. Code sets the price. You set the check that decides success. |
| 6 | 0:46 | 79 | You approve a hold, not a payment. (pause) Before Claude gets the keyboard, Keyhold asks PayPal whether the hold is live, and asks again every five inputs. (pause) Claude logs in, finds the typo in the SSH config, and restarts the service. [MATCH THE TAKE] (pause) Now your check runs: an SSH login with a key the agent never sees, and your page. It has to pass twice. (pause) Then, and only then, Keyhold captures. Nine dollars. Those ids are read live from the PayPal sandbox. |
| 7 | 1:44 | 21 | Change your mind? Stop takes the keyboard back first, then voids the hold. Nothing is charged, and PayPal shows it voided. |
| 8 | 2:00 | 31 | Some faults cannot be fixed from a console. Claude says so, and no hold is offered. If one surfaces after the hold, the agent gives up, and the hold is voided. |
| 9 | 2:18 | 23 | After a capture, your check keeps running. If the server falls over again, two failed checks in a row refund you in full. |
| 10 | 2:30 | 18 | Kill Keyhold mid-capture. On restart it replays the same request id, and PayPal still has exactly one capture. |
| 10, fallback | 2:30 | 17 | In the tests we kill Keyhold mid-capture, again and again. Each time, there is exactly one capture. |
| 11 | 2:42 | 11 | No hold, no keystrokes. Only your server answering can charge you. |

Notes:

- Shot 8's "Claude says so" is true only if run C's free look answered `console_locked`. If it
  named a priced class, re-run C or cut that sentence.
- The "(pause)" marks in shot 6 line up with the take: approve, gate line, typing, probe, capture.
- Record the voiceover after picture lock, by a person. The final sound mix is decided by ear.

## Live, from the UI, or built in the edit

| Must be recorded live (a screen recording of the real run, never rebuilt) | Can be captured from the UI after the run | Built in the edit |
|---|---|---|
| the ssh failure (shot 1) | run A's console screenshot (shot 2) | the two lines in shot 3 |
| the quote and the whole uncut take (shots 5-6) | the home page and `/try` (shot 4) | the closing card (shot 11) |
| Stop on run B (shot 7) | `/judge` rows and the "PayPal answered" and "Webhook" ticks, from the same runs | source tags, speed labels, clocks |
| run C's free look and run D's terminal (shot 8) | optional: the sandbox dashboard's record of the same capture, with account names and emails blurred | |
| `docker stop` and the refund (shot 9) | | |
| the kill and restart, or the chaos tests (shot 10) | | |

Nothing in the edit may show an id, amount or status that a run did not produce. No animated
mock-ups of the status page.

## On-screen text never moves after it lands

- Text added in the edit appears with a hard cut or a fade of 200 ms or less, then stays at a
  whole-pixel position, pixel-still, until it leaves by a cut or fade. No slides, no drift, no
  type-on that moves the line, no parallax.
- No push-ins, pans, zooms or Ken Burns moves on any frame that has text, recorded UI included.
  To show a detail, cut between two fixed framings of the same recording.
- Speed labels and clocks change their digits in place, in tabular figures, at a fixed position.
- In recordings, do not scroll while text that matters is on screen. The app's own live updates
  (new log lines, status changes) are the content and stay as recorded.
- Check: in every still section, two frames one second apart must be identical outside the live
  UI. Diff them before export.

## Screen hygiene

Nothing below may appear in any frame. Check every shot before export.

- Secrets: the `/try` passcode, the sandbox buyer's password, PayPal client id or secret, the
  webhook id, the Anthropic API key, the console password (the app already shows it as
  `[console password]`; keep it that way).
- Identity: a terminal prompt with a user or machine name, a local file path (the last line of
  `prove.py` prints the absolute path of its results folder; end shot 8 before it), browser
  bookmarks, extensions, other tabs, notifications.
- Addresses: IP addresses and hostnames, including in ssh messages. Use the `web1` alias, and if
  ssh's own message prints an address, cut before it. The browser address bar is hidden, or shows
  only [HOSTED URL].

## Before export

- [ ] Hook complete by 0:10; Keyhold's page on screen by 0:20; total 2:50.
- [ ] Shot 6 is one recording with no time removed; every speed change is labelled.
- [ ] The order, authorization and capture ids in shot 6 are readable for at least 2 seconds and
      match run A's `/judge.json`: [SANDBOX ORDER ID], [SANDBOX AUTHORIZATION ID],
      [SANDBOX CAPTURE ID].
- [ ] Every recorded shot carries its source tag.
- [ ] Frame-diff check passed for still sections.
- [ ] Screen hygiene checked frame by frame.
- [ ] Every VO line matches its picture; shot 6's typing line rewritten to match the take.
