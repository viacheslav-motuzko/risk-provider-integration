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
RELAY = "apps/risk_integration/runtime/relay.py"
DECISIONS = "apps/risk_integration/runtime/decisions.py"
INBOUND = "apps/risk_integration/runtime/inbound.py"
SUPERVISOR = "apps/risk_integration/runtime/supervisor.py"
SWEEPER = "apps/risk_integration/runtime/sweeper.py"
CONNECTION = "apps/risk_integration/runtime/connection.py"
BACKOFF = "apps/risk_integration/runtime/backoff.py"
WS_CLIENT = "apps/risk_integration/transport/ws_client.py"
HEALTH = "apps/risk_integration/runtime/health.py"
APP = "apps/risk_integration/runtime/app.py"
COMMAND = "apps/risk_integration/management/commands/run_risk_provider.py"

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
        (
            (
                "(OutcomeUnknown, NotAdmitted, OperationalError, InterfaceError)",
                "(OutcomeUnknown, NotAdmitted, InterfaceError)",
            ),
        ),
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
    Mutation(
        "admission-unbounded",
        "the wait for a run_db permit is not bounded",
        DB,
        (("async with asyncio.timeout(self._admission_timeout):", "async with asyncio.timeout(None):"),),
    ),
    Mutation(
        "not-admitted-unclassified",
        "a refused admission is not transient (fails the process)",
        ERRORS,
        (
            (
                "(OutcomeUnknown, NotAdmitted, OperationalError, InterfaceError)",
                "(OutcomeUnknown, OperationalError, InterfaceError)",
            ),
        ),
    ),
    Mutation(
        "lock-timeout-unlabelled",
        "a wait on the ticket lock past lock_timeout reads as a database error",
        ERRORS,
        (("    if isinstance(exc.__cause__, pg.LockNotAvailable):", "    if False:"),),
    ),
    Mutation(
        "resend-skipped",
        "a new session does not re-send what the previous one may have lost",
        RELAY,
        (("                    resend = await self._resend_owed()\n", "                    resend = deque()\n"),),
    ),
    Mutation(
        "loss-window-from-connect",
        "the final re-send window ends at the connect, not at the last final send",
        RELAY,
        (("window_start=last_final - settings.loss_window", "window_start=since - settings.loss_window"),),
    ),
    Mutation(
        "commit-before-send",
        "the send is committed without waiting for the frame to be written",
        RELAY,
        (
            (
                "            await session.send(encode(message, ts=datetime.now(UTC)))",
                "            asyncio.ensure_future(session.send(encode(message, ts=datetime.now(UTC))))",
            ),
        ),
    ),
    Mutation(
        "transient-compensates",
        "a transient apply failure compensates and cancels instead of waiting for the retransmission",
        DECISIONS,
        (("                if failure is ApplyFailure.TRANSIENT:\n", "                if False:\n"),),
    ),
    Mutation(
        "unclassified-swallowed",
        "an unclassified apply failure is swallowed instead of ending the process",
        DECISIONS,
        (
            (
                "                    raise\n                if failure is ApplyFailure.TRANSIENT:",
                "                    return\n                if failure is ApplyFailure.TRANSIENT:",
            ),
        ),
    ),
    Mutation(
        "permanent-not-compensated",
        "a permanent apply failure leaves the stake held instead of compensating",
        DECISIONS,
        (("                await self.compensate(ticket_id, Trigger.APPLY_FAILED)\n", "                pass\n"),),
    ),
    Mutation(
        "refused-ticket-escalates",
        "a refused TICKET is escalated instead of compensated",
        INBOUND,
        (("    elif trigger is Trigger.TICKET_REFUSED:", "    elif False:"),),
    ),
    Mutation(
        "refusal-ignored",
        "a refused confirm or cancellation is not escalated",
        INBOUND,
        (("        await decisions.escalate(error.ticket_id, trigger, error_code=error.code)\n", "        pass\n"),),
    ),
    Mutation(
        "returning-loop-ok",
        "a loop that returns is not treated as a failure",
        SUPERVISOR,
        (("    await loop()\n    raise LoopReturned(name)\n", "    await loop()\n"),),
    ),
    Mutation(
        "deadline-ignored",
        "the sweeper finds expired tickets but does not cancel them",
        SWEEPER,
        (
            (
                "                if not await self._decisions.compensate(ticket_id, Trigger.DEADLINE_PASSED):\n",
                "                if False:\n",
            ),
        ),
    ),
    Mutation(
        "failed-compensation-starves",
        "the sweeper keeps picking tickets whose compensation just failed, ahead of the ones behind them",
        SWEEPER,
        (("exclude=self._recently_failed()", "exclude=()"),),
    ),
    Mutation(
        "escalation-not-retried",
        "a transient failure of an escalation gives up at once (the provider does not repeat its error)",
        DECISIONS,
        (("if attempt >= self._escalation_attempts:", "if True:"),),
    ),
    Mutation(
        "session-end-exits",
        "a session that ends or is refused ends the process instead of reconnecting",
        CONNECTION,
        (("        return ended.outcome, lasted\n", "        raise\n"),),
    ),
    Mutation(
        "backpressure-unlabelled",
        "a session dropped while the inbound queue was full is not told apart",
        CONNECTION,
        (("queue_full=c.queue.full()", "queue_full=False"),),
    ),
    Mutation(
        "planned-not-immediate",
        "a planned close of a stable session waits like a transient failure",
        BACKOFF,
        (("            case SessionOutcome.PLANNED if stable:\n                return timedelta(0)\n", ""),),
    ),
    Mutation(
        "planned-always-immediate",
        "a planned close reconnects at once even after a session that did not last",
        BACKOFF,
        (("case SessionOutcome.PLANNED if stable:", "case SessionOutcome.PLANNED:"),),
    ),
    Mutation(
        "displaced-immediate",
        "a displaced session reconnects at once",
        BACKOFF,
        (("return self._jittered(p.displaced)", "return timedelta(0)"),),
    ),
    Mutation(
        "standby-grows",
        "a refused standby backs off further and further instead of retrying soon",
        BACKOFF,
        (
            (
                "return self._jittered(p.standby_retry)",
                "return self._jittered(self._grown(p.standby_retry, p.retry_max))",
            ),
        ),
    ),
    Mutation(
        "auth-short-backoff",
        "a refused token is retried on the short transient curve",
        BACKOFF,
        (("self._grown(p.auth_initial, p.auth_max)", "self._grown(p.retry_initial, p.retry_max)"),),
    ),
    Mutation(
        "streak-reset-at-handshake",
        "any opened session resets the backoff, so accept-then-close loops fast",
        BACKOFF,
        (("stable = lasted is not None and lasted >= self._policy.stable_after", "stable = lasted is not None"),),
    ),
    Mutation(
        "streak-across-outcomes",
        "a new outcome continues the previous streak (a standby that took over drops to the cap)",
        BACKOFF,
        (("if stable or outcome != self._outcome:", "if stable:"),),
    ),
    Mutation(
        "token-unbounded",
        "the token fetch has no timeout",
        WS_CLIENT,
        (("asyncio.timeout(within.total_seconds())", "asyncio.timeout(None)"),),
    ),
    Mutation(
        "leases-kept-at-session-end",
        "a relay whose session ends keeps the leases of its unsent batch",
        RELAY,
        (("            await self._release(batch)\n", "            pass\n"),),
    ),
    Mutation(
        "cancelled-claim-kept",
        "a claim that commits after the relay is cancelled keeps its leases",
        RELAY,
        (("return await asyncio.shield(claiming)", "return await claiming"),),
    ),
    Mutation(
        "release-any-owner",
        "releasing a batch also frees leases another relay holds",
        TICKETS,
        (("filter(ticket_id__in=ticket_ids, locked_by=owner)", "filter(ticket_id__in=ticket_ids)"),),
    ),
    Mutation(
        "liveness-blind",
        "liveness never reports a stale loop",
        HEALTH,
        (("return LoopState.STALE if deadline < now else LoopState.OK", "return LoopState.OK"),),
    ),
    Mutation(
        "sweeper-silent-on-db-error",
        "the sweeper beats only after handling a ticket, so a database outage reads as a hung loop",
        SWEEPER,
        (
            ("            self._heartbeat.beat()\n            try:", "            try:"),
            ("            self._heartbeat.beat(extra=self._settings.interval)\n", ""),
        ),
    ),
    Mutation(
        "relay-silent-on-db-error",
        "the relay beats only after a send, so a database outage reads as a hung loop",
        RELAY,
        (("                self._heartbeat.beat()\n                try:", "                try:"),),
    ),
    Mutation(
        "ready-while-draining",
        "a draining pod still reports ready",
        HEALTH,
        (
            (
                "return self._started and not draining and db_reachable, detail",
                "return self._started and db_reachable, detail",
            ),
        ),
    ),
    Mutation(
        "ready-without-db",
        "readiness ignores whether the database was reached lately",
        HEALTH,
        (("db_reachable = last is not None and self._clock() - last <= self._ready_db_within", "db_reachable = True"),),
    ),
    Mutation(
        "db-success-not-recorded",
        "the runner never records a committed unit of work, so no pod is ever ready",
        DB,
        (("            self._last_success = time.monotonic()\n", ""),),
    ),
    Mutation(
        "start-with-pending-migrations",
        "the loops start while migrations are pending",
        APP,
        (("                if not pending:", "                if True:"),),
    ),
    Mutation(
        "session-ignores-shutdown",
        "a shutdown request does not stop the session, so the reader and the relay go on",
        CONNECTION,
        (('                "stop": shutdown.stop_session,\n', ""),),
    ),
    Mutation(
        "drain-skips-queue",
        "the drain does not wait for the appliers to finish what was read",
        CONNECTION,
        (("            await queue.join()\n", ""),),
    ),
    Mutation(
        "applier-no-task-done",
        "an applier never marks a message done, so the drain cannot see the queue finish",
        INBOUND,
        (("            queue.task_done()\n", "            pass\n"),),
    ),
    Mutation(
        "drain-skips-flush",
        "the drain does not send the final messages the appliers committed",
        CONNECTION,
        (("            flushed = await relay.flush(session)", "            flushed = 0"),),
    ),
    Mutation(
        "flush-sends-tickets",
        "a draining flush also claims PENDING_SEND tickets",
        TICKETS,
        (
            (
                "return _claim(Q(state__in=FINAL_PENDING), owner=owner",
                "return _claim(Q(state__in=FINAL_PENDING | {State.PENDING_SEND}), owner=owner",
            ),
        ),
    ),
    Mutation(
        "drain-closes-normally",
        "a draining process closes its session with 1000 instead of 1001",
        CONNECTION,
        (('await session.close(CLOSE_GOING_AWAY, "shutting down")', 'await session.close(1000, "shutting down")'),),
    ),
    Mutation(
        "shutdown-waits-out-backoff",
        "a shutdown request without a session waits out the reconnect delay",
        CONNECTION,
        (
            (
                "        await c.shutdown.sleep(delay.total_seconds())",
                "        await asyncio.sleep(delay.total_seconds())",
            ),
        ),
    ),
    Mutation(
        "sigterm-unhandled",
        "the event loop installs no SIGTERM handler, so SIGTERM exits without a drain",
        COMMAND,
        (("        loop.add_signal_handler(sig, app.shutdown.request, sig.name)", "        pass"),),
    ),
    Mutation(
        "decision-time-from-now",
        "the time to decision is not measured from the ticket's creation",
        TICKETS,
        (
            (
                'ExpressionWrapper(Now() - F("created_at"), output_field=DurationField())',
                "ExpressionWrapper(Now() - Now(), output_field=DurationField())",
            ),
        ),
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
