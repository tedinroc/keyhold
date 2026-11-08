# Hosted rescue 53f4549c-b4f5-4d51-8a7f-ea9b779ce79d

Exported from the ledger of the hosted Keyhold (`/try` on the public URL, live mode): the PayPal sandbox through the
official Server SDK, `KEYHOLD_WORLD=vm` on an x86-64 cloud VM with KVM (a Debian 13 amd64 test guest), Claude Haiku 5.5
through the official `anthropic` SDK and an Anthropic-compatible endpoint, judge-mode timing. The hold was approved by a
sandbox buyer with the JS SDK v6 button on the public page. Screenshots are left out; the console password was redacted
by Keyhold before it was stored. Times are UTC.

- Fault: Typo in sshd_config. Quote: sshd_config_error T1 $9.00, hash `1cc95eb9d9ce7b6d67f26f9352e54fb98cd8ac60e4d9a172e062e3a848d0a7db`
- PayPal: order `9DX392958A657063W`, authorization `68M23905BN0687322`, capture `7WP61145AR824644E` COMPLETED $9.00; final state WARRANTY_OK (24 warranty checks)
- Model spend (Keyhold's list-price estimate): agent: 7 call(s), $0.0032, 2818 output tokens; free look: 1 call(s), $0.0007, 738 output tokens

| Time | Event | Detail |
|---|---|---|
| 06:59:01 | quoted | sshd_config_error T1 $9.00 |
| 06:59:01 | free_look | sshd will not start because of an error in /etc/ssh/sshd_config. Quote T1, $9.00. |
| 07:01:41 | outbox | create_order pending, PayPal-Request-Id order-53f4549c-b4f5-4d51-8a7f-ea9b779ce79d |
| 07:01:41 | paypal_call | create_order with PayPal-Request-Id order-53f4549c-b4f5-4d51-8a7f-ea9b779ce79d |
| 07:01:43 | paypal_ok | order 9DX392958A657063W PAYER_ACTION_REQUIRED |
| 07:01:43 | state | QUOTED -> HOLD_PENDING |
| 07:06:33 | outbox | authorize pending, PayPal-Request-Id auth-53f4549c-b4f5-4d51-8a7f-ea9b779ce79d |
| 07:06:33 | paypal_call | authorize with PayPal-Request-Id auth-53f4549c-b4f5-4d51-8a7f-ea9b779ce79d |
| 07:06:34 | paypal_ok | authorization 68M23905BN0687322 CREATED $9.00 |
| 07:06:34 | state | HOLD_PENDING -> HELD |
| 07:06:34 | gate_check | PayPal authorization 68M23905BN0687322 CREATED $9.00 |
| 07:06:34 | state | HELD -> ACTIVE: keyboard lent to the agent |
| 07:06:37 | model_spend | agent: 4 input, 3075 cache-write, 0 cache-read and 172 output tokens, about $0.0005 |
| 07:06:37 | agent_note | The screen is showing a login prompt for "keyhold-demo," and I need to sign in as root with the password I've been given. My instructions say to enter the password only when a password prompt appears, and this one comes right after the username, so I'll type it now. |
| 07:06:37 | agent_type | root\n |
| 07:06:41 | model_spend | agent: 2 input, 1533 cache-write, 3075 cache-read and 165 output tokens, about $0.0003 |
| 07:06:43 | agent_type | [console password]\n |
| 07:06:45 | gate_check | PayPal authorization 68M23905BN0687322 CREATED $9.00 |
| 07:06:51 | agent_type | systemctl status ssh.service --no-pager \| cat; journalctl -u ssh -b --no-pager \| tail -n 30; sshd -t; echo DONE-T\n |
| 07:06:56 | model_spend | agent: 2 input, 1537 cache-write, 4608 cache-read and 336 output tokens, about $0.0004 |
| 07:06:56 | agent_note | I'm checking line 3 of sshd_config, where "PermitRootLogn" is misspelled, and I plan to back up the file first. Rather than guess the intended value, I'll look at the file before changing it to "PermitRootLogin". I also want to check the web page on port 80 later. |
| 07:07:06 | agent_type | cp /etc/ssh/sshd_config /etc/ssh/sshd_config.keyhold-bak; head -n 6 /etc/ssh/sshd_config \| cat -A \| cut -c1-120; grep -n -i '^[^#]' /etc/ssh/sshd_config \| cat; echo DONE-V\n |
| 07:07:08 | gate_check | PayPal authorization 68M23905BN0687322 CREATED $9.00 |
| 07:07:11 | model_spend | agent: 2 input, 1009 cache-write, 6145 cache-read and 394 output tokens, about $0.0004 |
| 07:07:11 | agent_note | I'm correcting the typo on line 3 from "PermitRootLogn" to "PermitRootLogin", keeping the value "no" as it is. Then I'll test, restart, and verify the config. PasswordAuthentication stays "no" since the owner logs in with keys, so I'm leaving that line alone. |
| 07:07:31 | agent_type | sed -i '3s/^PermitRootLogn no$/PermitRootLogin no/' /etc/ssh/sshd_config && sed -n 3p /etc/ssh/sshd_config && sshd -t && echo SSHD-T-OK; systemctl reset-failed ssh.service; systemctl start ssh.service; systemctl is-active ssh; systemctl status ssh.service --no-pager \| cat \| head -n 12; ss -ltnp \| cat; systemctl is-active demo-page.socket demo-page.service; echo DONE-F\n |
| 07:07:33 | gate_check | PayPal authorization 68M23905BN0687322 CREATED $9.00 |
| 07:07:40 | model_spend | agent: 2 input, 1067 cache-write, 7154 cache-read and 1236 output tokens, about $0.0008 |
| 07:07:40 | agent_note | I'm checking why the sed edit didn't take: the pattern expected "PermitRootLogn no" at the start and end of the line, but the line seems to match. I'm wondering if there's hidden whitespace or a carriage return, since I'd earlier looked at the line with cat -A.  I'm now looking at the sshd output more closely: the status shows the service active with ExecStartPre returning 0, which seems inconsistent with the "Missing privilege separation directory" message from sshd -t. I'm also trying to reconcile which line in the head output actually reads "PermitRootLogn no".  I'm checking why the sed ... |
| 07:07:49 | agent_type | grep -n 'PermitRoot' /etc/ssh/sshd_config \| cat -A; sshd -t; echo "rc=$?"; ls -ld /run/sshd; curl -sS -m 5 http://localhost/ \| head -n 5; echo DONE-G\n |
| 07:07:51 | gate_check | PayPal authorization 68M23905BN0687322 CREATED $9.00 |
| 07:07:54 | model_spend | agent: 2 input, 1909 cache-write, 8221 cache-read and 237 output tokens, about $0.0004 |
| 07:07:54 | agent_note | The sed edit does look applied now: line 3 reads "PermitRootLogin no", and sshd's config test passed. SSH is running and listening on port 22, and the web page is serving content. Next I'll confirm the ssh service is active with systemctl. |
| 07:07:58 | agent_type | systemctl is-active ssh; systemctl --failed --no-pager \| cat; echo DONE-H\n |
| 07:08:02 | model_spend | agent: 2 input, 910 cache-write, 10130 cache-read and 278 output tokens, about $0.0004 |
| 07:08:02 | agent_finished | Root cause: /etc/ssh/sshd_config line 3 had a misspelled directive "PermitRootLogn no", so sshd exited with "Bad configuration option" and ssh.service failed to start. I backed up the file to /etc/ssh/sshd_config.keyhold-bak, corrected line 3 to "PermitRootLogin no" (value unchanged), reset the failed state and started ssh. Verified: sshd -t exits 0, systemctl is-active ssh reports active, sshd listens on 0.0.0.0:22 and [::]:22, no failed units, and http://localhost/ serves the demo page ("keyhold-demo: the page is being served."). Nothing else was changed. |
| 07:08:02 | gate_revoked | the agent session ended (done) |
| 07:08:02 | agent_result | done: Root cause: /etc/ssh/sshd_config line 3 had a misspelled directive "PermitRootLogn no", so sshd exited with "Bad configuration option" and ssh.service failed to start. I backed up the file to /etc/ssh/sshd_config.keyhold-bak, corrected line 3 to "PermitRootLogin no" (value unchanged), reset the failed state and started ssh. Verified: sshd -t exits 0, systemctl is-active ssh reports active, sshd listens on 0.0.0.0:22 and [::]:22, no failed units, and http://localhost/ serves the demo page ("keyhold-demo: the page is being served."). Nothing else was changed. |
| 07:08:02 | state | ACTIVE -> PROBING: the agent called finished; the owner probe decides |
| 07:08:03 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:08:08 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:08:13 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:08:18 | probe | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:08:18 | probe_stable | stable: 2 passes 14.6841 s apart, no failure between |
| 07:08:18 | outbox | capture pending, PayPal-Request-Id cap-53f4549c-b4f5-4d51-8a7f-ea9b779ce79d |
| 07:08:18 | paypal_call | capture with PayPal-Request-Id cap-53f4549c-b4f5-4d51-8a7f-ea9b779ce79d |
| 07:08:19 | paypal_ok | capture 7WP61145AR824644E COMPLETED $9.00 |
| 07:08:19 | state | PROBING -> CAPTURED: probe passed |
| 07:08:19 | warranty_started | Warranty (judge mode, compressed): re-probed every minute for 24 minutes; each minute stands in for an hour. Two failed checks in a row refund the full capture. |
| 07:09:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:10:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:11:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:12:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:13:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:14:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:15:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:16:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:17:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:18:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:19:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:20:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:21:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:22:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:23:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:24:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:25:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:26:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:27:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:28:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:29:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:30:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:31:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:32:19 | warranty_check | ssh ok: logged in as probe; http ok: HTTP 200, sha256 matches |
| 07:32:19 | state | CAPTURED -> WARRANTY_OK: window ended: 24 checks, never 2 failures in a row |
