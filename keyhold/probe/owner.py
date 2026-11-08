"""run_probe(spec, ssh=..., http=...) -> ProbeResult.

Pass = SSH login (BatchMode subprocess with the hidden probe key) AND the page content matches.
Stable pass = two passes at least 30 s apart with no failure between (flapping rule).
Also holds the warranty schedule: re-probe hourly for 24 h (minutes in judge mode).

This is the module the rest of Keyhold imports. It re-exports:

- ``ProbeSpec`` / ``ProbeResult`` (defined in ``keyhold.types``) and the checkout helpers
  ``build_spec``, ``check_spec``, ``parse_public_key``, ``authorized_keys_line``;
- the transports ``SubprocessSsh`` and ``HttpxFetcher`` (and their protocols);
- the stable-pass rule (``StableRule``, ``StableTracker``, ``wait_for_stable_pass``);
- the warranty (``WarrantyPlan``, ``WarrantySchedule``, ``run_warranty``).

Nothing here can capture, void or refund. The probe only answers "is the owner's server up";
the orchestrator turns a stable pass into a capture and a REFUND decision into a refund.
A doubtful probe (crash, timeout, wrong status, size limit) is always a failure, never a pass.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

import httpx

from keyhold.types import Clock, ProbeResult, ProbeSpec, SystemClock

from .spec import (
    ProbeSpecError,
    PublicKey,
    authorized_keys_line,
    build_spec,
    check_spec,
    parse_public_key,
)
from .stable import StableOutcome, StableRule, StableTracker, wait_for_stable_pass
from .transports import (
    HostKeyPolicy,
    HttpFetch,
    HttpTransport,
    HttpxFetcher,
    ProbeConfigError,
    SshCheck,
    SshTransport,
    SubprocessSsh,
    ensure_probe_key,
    known_hosts_path,
)
from .warranty import (
    WarrantyCheck,
    WarrantyClosed,
    WarrantyDecision,
    WarrantyPlan,
    WarrantySchedule,
    WarrantyVerdict,
    run_warranty,
)

__all__ = [
    "JUDGE_POLICY",
    "STANDARD_POLICY",
    "HostKeyPolicy",
    "HttpFetch",
    "HttpTransport",
    "HttpxFetcher",
    "ProbeConfigError",
    "ProbePolicy",
    "ProbeResult",
    "ProbeSpec",
    "ProbeSpecError",
    "Prober",
    "PublicKey",
    "SshCheck",
    "SshTransport",
    "StableOutcome",
    "StableRule",
    "StableTracker",
    "SubprocessSsh",
    "WarrantyCheck",
    "WarrantyClosed",
    "WarrantyDecision",
    "WarrantyPlan",
    "WarrantySchedule",
    "WarrantyVerdict",
    "authorized_keys_line",
    "build_spec",
    "check_page",
    "check_spec",
    "ensure_probe_key",
    "known_hosts_path",
    "parse_public_key",
    "probe_policy",
    "run_probe",
    "run_warranty",
    "wait_for_stable_pass",
]


# --------------------------------------------------------------------------------------------
# One probe
# --------------------------------------------------------------------------------------------


def check_page(spec: ProbeSpec, page: HttpFetch) -> tuple[bool, str]:
    """Does ``page`` meet the spec? Needs a 2xx status and the expected text or sha256.

    Redirects are not followed and count as a failure: the URL fixed at checkout must answer.
    The sha256 covers the whole body, so a body cut at the size limit cannot match.
    """
    status = page.status
    if 300 <= status < 400:
        return False, f"HTTP {status} redirect (not followed; the checkout URL must answer itself)"
    if not 200 <= status < 300:
        return False, f"HTTP {status}"
    if spec.expect_sha256 is not None:
        if page.truncated:
            return False, f"HTTP {status}, page larger than the size limit, sha256 not checked"
        digest = hashlib.sha256(page.body).hexdigest()
        if hmac.compare_digest(digest, spec.expect_sha256.lower()):
            return True, f"HTTP {status}, sha256 matches"
        return False, f"HTTP {status}, sha256 {digest[:12]}... does not match"
    needle = (spec.expect_substring or "").encode("utf-8")
    if not needle.strip():
        return False, "no expected text is set"
    if needle in page.body:
        return True, f"HTTP {status}, expected text found"
    return False, f"HTTP {status}, expected text not found"


def _explain_http_error(exc: BaseException) -> str:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timed out"
    if "CERTIFICATE" in str(exc).upper():
        return "TLS certificate not trusted"
    if isinstance(exc, (httpx.ConnectError, ConnectionError)):
        return "could not connect"
    if isinstance(exc, httpx.HTTPError):
        return f"request failed ({type(exc).__name__})"
    return f"check crashed ({type(exc).__name__})"


def run_probe(
    spec: ProbeSpec,
    *,
    ssh: SshTransport,
    http: HttpTransport | None = None,
    clock: Clock | None = None,
) -> ProbeResult:
    """Run both checks once and report. Never raises for a server or network problem.

    Both checks always run, so the status page can show which half failed.
    ``ssh`` is required: it carries the probe key and the rescue's known_hosts file
    (``SubprocessSsh.from_env(rescue_id)`` in production). ``http`` defaults to ``HttpxFetcher()``.
    """
    clock = clock or SystemClock()
    http = http if http is not None else HttpxFetcher()

    try:
        ssh_check = ssh.login(spec)
        ssh_ok, ssh_detail = bool(ssh_check.ok), ssh_check.detail
    except Exception as exc:  # noqa: BLE001 - a transport bug is a failed check, not a pass
        ssh_ok, ssh_detail = False, f"check crashed ({type(exc).__name__})"

    try:
        http_ok, http_detail = check_page(spec, http.get(spec.url))
    except Exception as exc:  # noqa: BLE001
        http_ok, http_detail = False, _explain_http_error(exc)

    detail = f"ssh {'ok' if ssh_ok else 'failed'}: {ssh_detail}; http {'ok' if http_ok else 'failed'}: {http_detail}"
    return ProbeResult(ok=ssh_ok and http_ok, ssh_ok=ssh_ok, http_ok=http_ok, detail=detail, at=clock.now())


@dataclass
class Prober:
    """A spec bound to its transports: ``Prober(spec, ssh, http, clock)()`` runs one probe.
    This is the ``probe`` callable that ``wait_for_stable_pass`` and ``run_warranty`` take."""

    spec: ProbeSpec
    ssh: SshTransport
    http: HttpTransport | None = None
    clock: Clock | None = field(default=None)

    def __call__(self) -> ProbeResult:
        return run_probe(self.spec, ssh=self.ssh, http=self.http, clock=self.clock)


# --------------------------------------------------------------------------------------------
# Timing policy (standard and judge mode)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbePolicy:
    """All probe timing in one place.

    ``interval_s``: time between probes while waiting for a stable pass.
    ``deadline_s``: how long after the agent finishes the server has to reach a stable pass
    before the hold is voided.
    """

    judge_mode: bool
    stable: StableRule
    interval_s: float
    deadline_s: float
    warranty: WarrantyPlan

    def __post_init__(self) -> None:
        if self.interval_s <= 0 or self.deadline_s <= 0:
            raise ValueError("interval_s and deadline_s must be positive")
        if self.deadline_s < self.stable.min_gap_s:
            raise ValueError("deadline_s is shorter than the stable-pass gap, so no capture could happen")

    @property
    def label(self) -> str:
        capture = f"Capture after {self.stable.label}."
        if self.judge_mode:
            return f"Judge mode, compressed timing. {capture} {self.warranty.label}"
        return f"{capture} {self.warranty.label}"


STANDARD_POLICY = ProbePolicy(
    judge_mode=False,
    stable=StableRule(min_gap_s=30.0, passes=2),
    interval_s=10.0,
    deadline_s=600.0,
    warranty=WarrantyPlan(),
)

JUDGE_POLICY = ProbePolicy(
    judge_mode=True,
    stable=StableRule(min_gap_s=10.0, passes=2),
    interval_s=5.0,
    deadline_s=300.0,
    warranty=WarrantyPlan.judge(),
)


def probe_policy(judge_mode: bool) -> ProbePolicy:
    return JUDGE_POLICY if judge_mode else STANDARD_POLICY
