# Hosted rescue 8cf5a71c-9f39-4747-9f67-7a61b8694311

Typo in sshd_config, captured. Exported from the ledger of the hosted Keyhold (live mode, PayPal sandbox through the official Server SDK, an x86-64 test VM under KVM, Claude Haiku 5.5 through the official `anthropic` SDK and an Anthropic-compatible endpoint, the redesigned pages). The hold was approved by a sandbox buyer with the JS SDK v6 button. Judge-mode timing on this deployment: the warranty re-probes 6 times, 20 s apart (compressed; the real plan is 24 hourly checks). Screenshots are left out; the console password was redacted by Keyhold before it was stored. Times are UTC.

- Quote: sshd_config_error T1 $9.00
- PayPal: order `01G48719H6433361U`, authorization `17712837C59118918`, capture `1FM0294205526573H` COMPLETED $9.00; final state WARRANTY_OK (6 warranty checks)
- Hold to capture: 80 s
- Model spend (Keyhold's list-price estimate): agent: 7 call(s), $0.0023; free look: 1 call(s), $0.0007
- Webhooks received and verified with verify-webhook-signature: PAYMENT.CAPTURE.COMPLETED (verified, applied, 16:40:41); PAYMENT.AUTHORIZATION.CREATED (verified, applied, 16:40:47)

| Time | Event | Detail |
|---|---|---|
| 16:38:08 | quoted | sshd_config_error T1 $9.00 |
| 16:38:08 | free_look | sshd will not start because of an error in /etc/ssh/sshd_config. Quote T1, $9.00. |
| 16:38:19 | outbox | create_order pending, PayPal-Request-Id order-8cf5a71c-9f39-4747-9f67-7a61b8694311 |
| 16:38:19 | paypal_call | create_order with PayPal-Request-Id order-8cf5a71c-9f39-4747-9f67-7a61b8694311 |
| 16:38:20 | paypal_ok | order 01G48719H6433361U PAYER_ACTION_REQUIRED |
| 16:38:20 | state | QUOTED -> HOLD_PENDING |
| 16:39:00 | outbox | authorize pending, PayPal-Request-Id auth-8cf5a71c-9f39-4747-9f67-7a61b8694311 |
| 16:39:00 | paypal_call | authorize with PayPal-Request-Id auth-8cf5a71c-9f39-4747-9f67-7a61b8694311 |
| 16:39:01 | paypal_ok | authorization 17712837C59118918 CREATED $9.00 |
| 16:39:01 | state | HOLD_PENDING -> HELD |
| 16:39:02 | gate_check | PayPal authorization 17712837C59118918 CREATED $9.00 |
| 16:39:02 | state | HELD -> ACTIVE: keyboard lent to the agent |
| 16:39:04 | model_spend | agent: 4 input, 3058 cache-write, 0 cache-read and 137 output tokens, about $0.0005 |
| 16:39:04 | agent_note | I'll log in at the console first. The password is in the goal, and I can only type it at a password prompt, so I'll enter root at the "keyhold-demo login:" prompt and then wait for the password prompt before typing it. |
| 16:39:04 | agent_type | root\n |
| 16:39:08 | model_spend | agent: 2 input, 1498 cache-write, 3058 cache-read and 145 output tokens, about $0.0003 |
| 16:39:10 | agent_type | [console password]\n |
| 16:39:12 | gate_check | PayPal authorization 17712837C59118918 CREATED $9.00 |
| 16:39:16 | agent_type | journalctl -u ssh -n 50 --no-pager \| cat; sshd -t; echo RC=$?\n |
| 16:39:20 | model_spend | agent: 2 input, 1517 cache-write, 4556 cache-read and 234 output tokens, about $0.0004 |
| 16:39:20 | agent_note | I'm checking line 3 of the config, where "PermitRootLogn" looks like a typo. I'll look at the first few lines with cat -A first, after making a backup. |
| 16:39:27 | agent_type | cp /etc/ssh/sshd_config /etc/ssh/sshd_config.keyhold-bak; sed -n '1,6p' /etc/ssh/sshd_config \| cat -n; grep -n Port /etc/ssh/sshd_config\n |
| 16:39:29 | gate_check | PayPal authorization 17712837C59118918 CREATED $9.00 |
| 16:39:32 | model_spend | agent: 2 input, 907 cache-write, 6073 cache-read and 271 output tokens, about $0.0003 |
| 16:39:32 | agent_note | I'm fixing the typo on line 3, changing "PermitRootLogn" to "PermitRootLogin," and I'll use sed on that line alone. Line 15 is a commented-out "#Port 22," which is already the default, so I'll leave it untouched. |
| 16:39:39 | agent_type | sed -i '3s/^PermitRootLogn no$/PermitRootLogin no/' /etc/ssh/sshd_config; sed -n '3p' /etc/ssh/sshd_config; sshd -t; echo RC=$?\n |
| 16:39:44 | model_spend | agent: 2 input, 944 cache-write, 6980 cache-read and 252 output tokens, about $0.0003 |
| 16:39:44 | agent_note | I've got a missing privilege separation directory for sshd, and creating /run/sshd with mkdir -p is a safe fix that destroys no data. I also want to glance at the demo-page.socket web page, which may be fine, and then restart ssh. |
| 16:39:51 | agent_type | mkdir -p /run/sshd && chmod 0755 /run/sshd; sshd -t; echo RC=$?; systemctl restart ssh.service; systemctl is-active ssh; ss -ltnp \| cat\n |
| 16:39:54 | gate_check | PayPal authorization 17712837C59118918 CREATED $9.00 |
| 16:39:56 | model_spend | agent: 2 input, 925 cache-write, 7924 cache-read and 155 output tokens, about $0.0003 |
| 16:39:56 | agent_note | SSH is running and listening on port 22, and port 80 is also open via the systemd demo-page socket. I'll check the web page quickly with curl. |
| 16:40:00 | agent_type | curl -s http://127.0.0.1/ \| head -5; systemctl is-active ssh demo-page.socket\n |
| 16:40:05 | model_spend | agent: 2 input, 828 cache-write, 8849 cache-read and 265 output tokens, about $0.0003 |
| 16:40:05 | agent_finished | Root cause: /etc/ssh/sshd_config line 3 had a typo, 'PermitRootLogn no', which made sshd refuse to start. I backed up the file to /etc/ssh/sshd_config.keyhold-bak and corrected only that line to 'PermitRootLogin no'. sshd -t then flagged a missing privilege-separation directory, so I created /run/sshd (mode 0755). I restarted ssh: it is active, listening on 0.0.0.0:22 and [::]:22, and sshd -t passes. Port 22 was not changed. The demo web page on port 80 answers locally with its expected content, and demo-page.socket is active. |
| 16:40:05 | gate_revoked | the agent session ended (done) |
| 16:40:05 | agent_result | done: Root cause: /etc/ssh/sshd_config line 3 had a typo, 'PermitRootLogn no', which made sshd refuse to start. I backed up the file to /etc/ssh/sshd_config.keyhold-bak and corrected only that line to 'PermitRootLogin no'. sshd -t then flagged a missing privilege-separation directory, so I created /run/sshd (mode 0755). I restarted ssh: it is active, listening on 0.0.0.0:22 and [::]:22, and sshd -t passes. Port 22 was not changed. The demo web page on port 80 answers locally with its expected content, and demo-page.socket is active. |
| 16:40:05 | state | ACTIVE -> PROBING: the agent called finished; the owner probe decides |
| 16:40:05 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:40:10 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:40:15 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:40:20 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:40:20 | probe_stable | stable: 2 passes 14.7094 s apart, no failure between |
| 16:40:20 | outbox | capture pending, PayPal-Request-Id cap-8cf5a71c-9f39-4747-9f67-7a61b8694311 |
| 16:40:20 | paypal_call | capture with PayPal-Request-Id cap-8cf5a71c-9f39-4747-9f67-7a61b8694311 |
| 16:40:21 | paypal_ok | capture 1FM0294205526573H COMPLETED $9.00 |
| 16:40:21 | state | PROBING -> CAPTURED: probe passed |
| 16:40:21 | warranty_started | Warranty (judge mode, compressed): re-probed every 20 seconds, 6 times (2 minutes in all); the real plan is 24 hourly checks. Two failed checks in a row refund the full capture. |
| 16:40:42 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:41:02 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:41:22 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:41:42 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:42:02 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:42:22 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 16:42:22 | state | CAPTURED -> WARRANTY_OK: window ended: 6 checks, never 2 failures in a row |
