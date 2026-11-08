# Keyhold: the film

A 1:58 film for the submission. Everything in the middle of it is real: Keyhold's own pages and
console screenshots, recorded from the owner's browser on the hosted Keyhold on 2026-11-08 (UTC),
with the PayPal sandbox and Claude Haiku 5.5. The opening, the title, the three rules, the
"under the hood" diagram and the end card are motion graphics.

## The runs it shows

| Scene | Run | What is on screen |
|---|---|---|
| A real run | [bf5a2f31](evidence/rescue-hosted-bf5a2f31.md), "Typo in sshd_config" | the Try page and the click on "Break this server"; the free look; the $9.00 quote with the screen line it is based on; the real "Pay with PayPal" button and PayPal's own window (sign-in, then "Complete Purchase", with PayPal's note that the merchant requested an authorization); "Hold approved · lending the keyboard"; the console while Claude types, and the Key moments rows where PayPal is re-checked |
| The fix, close up | the same run | the agent's own 800×600 console screenshots: `line 3: Bad configuration option: PermitRootLogn`, the one-word `sed` fix with `SYNTAX_OK`, and `active` |
| The money | the same run | "How your $9.00 moved": keyboard lent then locked, the owner's check passing twice, the capture 1 min 37 s after the hold, and the warranty starting; a label adds that this run's warranty later passed 6 of 6 (20:08:18 UTC) |
| Stop | [23ed2251](evidence/rescue-hosted-23ed2251.md), "fstab lists a disk that is gone" | $19.00 on hold, the owner's real click on "Stop and void the hold", and the page that follows: "Stopped. $0 charged … Nothing was typed after Stop" |
| Under the hood | ids from both runs, labelled as such | Orders v2 create and authorize, the keyboard gate, then Payments v2 capture, void, and refund (of a capture); every money call's PayPal-Request-Id; 14 of 14 real webhook deliveries verified |
| Ledger | the hosted `/judge` page | every run and where its money went |

Footage that plays faster than real time shows a "N×" badge. Highlight rings, the reveal of the
rows on the "moved" page one by one, and blurs over the sandbox buyer's e-mail (found by OCR in
every frame where it shows, on Keyhold's page and in PayPal's window) are the only things drawn
over the footage.

## How it is made

The film lives outside this repository (`keyhold-film/`): one deterministic HTML scene rendered
frame by frame in headless Chrome, the owner's recorded screencast frames as image sequences, a
synthetic narrator (VoxCPM2, Apache-2.0; designed from a text description, no real person's
voice), synthesized sound effects, and a Mixkit music track under the Mixkit free licence. A
layout checker probes every 0.1 s for text that leaves the frame, overlapping overlays and
dissolves between two text images; an OCR pass checks every second for e-mail addresses, hosts
and account names.

## Narration

1. Two in the morning. Your server just stopped answering.
2. SSH is down. The only way in is the console: a screen, and a keyboard with root on it.
3. An AI could fix this in a minute. But would you hand it the keys?
4. Meet Keyhold. It lends the keyboard under a PayPal hold.
5. No hold, no keyboard. No proof, no charge. And one press of Stop means you pay nothing.
6. Here's a real run, start to finish. Claude takes a free look at the screen and names the fault. The price is fixed: nine dollars.
7. You approve the hold in PayPal. Nothing is charged yet. The moment PayPal says the hold is live, the keyboard unlocks.
8. Claude reads the screen and types, while Keyhold keeps checking with PayPal that the hold is still live.
9. The cause: a typo on line three. Claude fixes it, restarts SSH, and gives the keyboard back.
10. But Claude doesn't get to say it's fixed. Your own check does: your SSH login and your web page, twice.
11. Both pass. Under two minutes after the hold, and only then, PayPal captures the nine dollars.
12. And a warranty keeps checking. If the server falls over again, the money goes back.
13. Another run: nineteen dollars on hold. Changed your mind? Press Stop. The keyboard locks, the hold is voided, and you pay zero.
14. Under the hood: PayPal Orders and Payments, with authorize, capture, void, and refund, through the official server SDK. Every money call carries a request ID, and every webhook is verified with PayPal.
15. Every run lands in a public ledger, with more than a thousand tests behind it.
16. Keyhold. Pay when it's back.

"Two in the morning" is the scenario, not the time of the recorded runs.
