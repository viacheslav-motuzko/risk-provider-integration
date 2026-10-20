"""Targeted mutation check: does the suite notice when an invariant is broken?

Each entry sabotages one invariant in the production code, runs the whole suite and restores the file. A sabotage
that leaves the suite green is a missing or weak test. Run with `make mutate` (needs Postgres); add an entry for
every invariant a stage introduces. Not production code.
"""

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TICKETS = "apps/risk_integration/persistence/tickets.py"
DB = "apps/risk_integration/persistence/db.py"
MACHINE = "apps/risk_integration/domain/machine.py"
ERRORS = "apps/risk_integration/persistence/errors.py"
ADAPTER = "apps/risk_integration/adapters/monolith.py"
SERVICES = "apps/risk_integration/services.py"
BETTING = "apps/betting/services.py"
WALLETS = "apps/wallets/services.py"

TIMEOUT_S = 120


@dataclass(frozen=True)
class Mutation:
    name: str
    sabotage: str
    path: str
    edits: tuple[tuple[str, str], ...]


MUTATIONS = (
    Mutation(
        "lease-owner",
        "send transition ignores the lease owner",
        TICKETS,
        (("        rows = rows.filter(locked_by=owner)\n", "        rows = rows\n"),),
    ),
    Mutation(
        "cas-state",
        "transition ignores the expected state",
        TICKETS,
        (("Q(ticket_id=ticket_id, state__in=rule.sources)", "Q(ticket_id=ticket_id)"),),
    ),
    Mutation(
        "lease-not-cleared",
        "transition does not clear the lease",
        TICKETS,
        (('rows.update(locked_by="", locked_until=None, **values)', "rows.update(**values)"),),
    ),
    Mutation(
        "no-skip-locked",
        "claim without SKIP LOCKED",
        TICKETS,
        (("select_for_update(skip_locked=True)", "select_for_update()"),),
    ),
    Mutation(
        "expired-lease-stuck",
        "an expired lease is not claimable",
        TICKETS,
        (("Q(locked_until__isnull=True) | Q(locked_until__lt=Now())", "Q(locked_until__isnull=True)"),),
    ),
    Mutation(
        "send-past-deadline",
        "claims ignore the deadline",
        TICKETS,
        (("return Q(state=state, deadline_at__gt=Now())", "return Q(state=state)"),),
    ),
    Mutation(
        "retransmit-forever",
        "the retransmit point is not cleared by a re-send",
        TICKETS,
        (('        values["retransmit_at"] = None\n', "        pass\n"),),
    ),
    Mutation(
        "deadline-not-rechecked",
        "the deadline cancellation trusts its caller",
        TICKETS,
        (("extra = Q(deadline_at__lte=Now()) if trigger is Trigger.DEADLINE_PASSED else None", "extra = None"),),
    ),
    Mutation(
        "resend-no-paging",
        "the reconnect re-send ignores `since`",
        TICKETS,
        (("_before_deadline(State.SENT) & Q(last_sent_at__lt=since)", "_before_deadline(State.SENT)"),),
    ),
    Mutation(
        "loss-window-open",
        "the final re-send ignores the window start",
        TICKETS,
        (("last_sent_at__gte=window_start, ", ""),),
    ),
    Mutation(
        "create-outside-tx",
        "create_ticket runs outside a transaction",
        TICKETS,
        (
            (
                '        raise RuntimeError("create_ticket must run inside the transaction that creates the bet")\n',
                "        pass\n",
            ),
        ),
    ),
    Mutation(
        "reply-misses-pre-send",
        "ACCEPT does not accept PENDING_SEND (reply before the relay's commit)",
        MACHINE,
        (
            (
                "Trigger.ACCEPT_APPLIED: Rule(AWAITING_RECOMMENDATION,",
                "Trigger.ACCEPT_APPLIED: Rule(frozenset({State.SENT}),",
            ),
        ),
    ),
    Mutation(
        "refund-captured",
        "APPLY_FAILED also from CONFIRM_PENDING (refunds a captured stake)",
        MACHINE,
        (
            (
                "Trigger.APPLY_FAILED: Rule(AWAITING_RECOMMENDATION,",
                "Trigger.APPLY_FAILED: Rule(AWAITING_RECOMMENDATION | {State.CONFIRM_PENDING},",
            ),
        ),
    ),
    Mutation(
        "unknown-ticket-escalates",
        "UNKNOWN_TICKET on a cancellation escalates",
        MACHINE,
        (("if ref is MessageRef.REQUEST_CANCELLATION and unknown_ticket:", "if False:"),),
    ),
    Mutation(
        "permit-on-coroutine-exit",
        "the run_db permit is released when the caller stops waiting",
        DB,
        (
            ("        work.add_done_callback(lambda _: loop.call_soon_threadsafe(self._permits.release))\n", ""),
            (
                '            raise OutcomeUnknown(getattr(fn, "__name__", repr(fn))) from exc\n',
                '            raise OutcomeUnknown(getattr(fn, "__name__", repr(fn))) from exc\n'
                "        finally:\n"
                "            self._permits.release()\n",
            ),
        ),
    ),
    Mutation(
        "no-context-copy",
        "context variables are not copied into the worker",
        DB,
        (("submit(ctx.run, self._unit_of_work,", "submit(self._unit_of_work,"),),
    ),
    Mutation(
        "session-timeouts",
        "timeouts set for the session instead of SET LOCAL",
        DB,
        (("%s, true)", "%s, false)"),),
    ),
    Mutation(
        "await-before-statement",
        "the await timeout may be below the statement timeout",
        DB,
        (("if await_timeout <= statement_timeout:", "if False:"),),
    ),
    Mutation(
        "operational-unclassified",
        "OperationalError is not transient",
        ERRORS,
        (("OutcomeUnknown | OperationalError | InterfaceError", "OutcomeUnknown | InterfaceError"),),
    ),
    Mutation(
        "apply-without-lock",
        "deciding does not lock the ticket",
        TICKETS,
        (("RiskTicket.objects.select_for_update().filter(due)", "RiskTicket.objects.filter(due)"),),
    ),
    Mutation(
        "compensate-before-deadline",
        "the deadline cancellation compensates a ticket that is not due",
        TICKETS,
        (("        due &= Q(deadline_at__lte=Now())\n", "        pass\n"),),
    ),
    Mutation(
        "ticket-outside-bet-tx",
        "place_bet creates the ticket after the bet's transaction",
        BETTING,
        (
            (
                "        ticket_id = enqueue_ticket(bet_id=bet.pk, snapshot=snapshot)\n",
                "    ticket_id = enqueue_ticket(bet_id=bet.pk, snapshot=snapshot)\n",
            ),
        ),
    ),
    Mutation(
        "rejection-keeps-stake",
        "a domain rejection does not release the stake",
        BETTING,
        (("            wallets.release(bet_id)\n            return Rejection", "            return Rejection"),),
    ),
    Mutation(
        "void-accepted-bet",
        "compensation voids an accepted bet",
        BETTING,
        (("if state not in (BetState.VOIDED, BetState.REJECTED):", "if state is None:"),),
    ),
    Mutation(
        "release-any-hold",
        "release is not conditional on HELD (double credit, refund of a captured stake)",
        WALLETS,
        (
            (
                "filter(bet_id=bet_id, state=HoldState.HELD).update(state=HoldState.RELEASED)",
                "filter(bet_id=bet_id).update(state=HoldState.RELEASED)",
            ),
        ),
    ),
    Mutation(
        "capture-any-hold",
        "capture is not conditional on HELD (captures a released stake)",
        WALLETS,
        (
            (
                "filter(bet_id=bet_id, state=HoldState.HELD).update(state=HoldState.CAPTURED)",
                "filter(bet_id=bet_id).update(state=HoldState.CAPTURED)",
            ),
        ),
    ),
    Mutation(
        "locked-miss-silent",
        "a transition that misses under the decision lock is not raised (effect commits without its transition)",
        SERVICES,
        (("    if not applied:\n", "    if False:\n"),),
    ),
    Mutation(
        "adapter-untranslated",
        "the adapter does not turn a broken monolith invariant into PermanentApplyError",
        ADAPTER,
        (("    except (betting.BetStateError, HoldStateError) as exc:", "    except PermanentApplyError as exc:"),),
    ),
)


def main(selected: list[str]) -> int:
    mutations = [m for m in MUTATIONS if not selected or m.name in selected]
    paths = sorted({m.path for m in mutations})
    before = _digest(paths)
    results = [(m, *_run(m)) for m in mutations]
    if _digest(paths) != before:
        sys.stderr.write("working tree changed: restore failed, check `git diff`\n")
        return 2
    lines = ["| Sabotage | Caught by |", "|---|---|"]
    for mutation, verdict, failed in results:
        lines.append(
            f"| `{mutation.name}`: {mutation.sabotage} | {verdict}{': ' if failed else ''}{', '.join(failed)} |"
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 1 if any(verdict == "SURVIVED" for _, verdict, _ in results) else 0


def _run(mutation: Mutation) -> tuple[str, list[str]]:
    path = Path(mutation.path)
    original = path.read_text()
    mutated = original
    for old, new in mutation.edits:
        if old not in mutated:
            raise SystemExit(f"{mutation.name}: pattern not found in {mutation.path}: {old!r}")
        mutated = mutated.replace(old, new)
    path.write_text(mutated)
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", "-q", "-rf", "--no-header", "-p", "no:cacheprovider"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"caught (hang > {TIMEOUT_S}s)", []
    finally:
        path.write_text(original)
    if proc.returncode == 0:
        return "SURVIVED", []
    return "caught", _failed_tests(proc.stdout) or [f"(exit {proc.returncode}, no test named)"]


def _failed_tests(output: str) -> list[str]:
    names = re.findall(r"^(?:FAILED|ERROR) \S+?::(\S+)", output, re.MULTILINE)
    return sorted({f"`{re.sub(r'\[.*\]$', '[...]', n)}`" for n in names})


def _digest(paths: list[str]) -> str:
    return hashlib.sha256(b"".join(Path(p).read_bytes() for p in paths)).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
