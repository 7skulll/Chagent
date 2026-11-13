"""Which recorded calls may be replayed, and why the rest may not.

Replay is a decision, not a loop over history. Three questions decide it, in this
order, because the earlier answer is the one that matters when several apply:

1. **Did a person decide this call, or did it never finish?** A denied, expired or
   unwatched approval is evidence about a decision, never a grant to execute the
   same thing later. An unknown outcome may already have happened; re-running it
   could duplicate the effect. Both are refused before their tool is even named.
2. **Would replaying leave this process?** Host commands, network reads, services,
   container reads and database rows are refused with the reason. The plan's rule
   is that external side effects are not replayed from history by default.
3. **Is the starting state actually known?** A recorded argument is a candidate,
   not a fixture. Without a case fixture that contains the file an ``edit_file``
   touched, the replay is refused as ``missing_initial_state`` — inventing an
   empty file would produce a confident wrong answer.

The output keeps every refusal that applies, not just the first: a denied
``exec_command`` is both an approval decision and a host side effect, and
reporting one of them would suggest the other had been considered and cleared.
Initial state is the exception — it is only checked for a call that is otherwise
eligible, because a call that cannot run has nothing to restore a starting file
for, and adding "no fixture" to every external call would bury the real reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from eval.replay import catalog
from eval.replay.sandbox import Fixture, FixtureInvalid, path_problems, required_state

PLAN_VERSION = 1
REPOSITORY = Path(__file__).resolve().parent.parent.parent

#: Refusal codes and what each one means. Kept together so the CLI, the document
#: and the acceptance record cannot drift into three vocabularies.
REFUSALS: dict[str, str] = {
    "approval_decision":
        "a person's decision (or its absence) is not a reusable grant; the call never ran",
    "outcome_unknown":
        "the recorded call started without a confirmed result; replaying may duplicate it",
    "external_side_effect":
        "executes on the host, reaches the network, or touches a service or container",
    "requires_database":
        "initial state lives in database rows, which a fixture cannot rebuild",
    "requires_run_context":
        "mutates a run's tool registry; there is no run to hold the change",
    "undeclared_tier":
        "no declaration in this checkout, so replay eligibility is unknown",
    "unsafe_path":
        "argument would leave the replay directory or passes through a link",
    "missing_fixture":
        "no case fixture was supplied, so the starting files are unknown",
    "missing_initial_state":
        "the fixture does not contain a path this call needs before it runs",
}



@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str

    def to_json(self) -> dict:
        return {"code": self.code, "detail": self.detail, "meaning": REFUSALS.get(self.code, "")}


@dataclass(frozen=True)
class CallPlan:
    index: int
    call_id: str
    tool: str
    tier: str
    refusals: tuple[Refusal, ...]
    required_state: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.refusals

    def to_json(self) -> dict:
        return {
            "index": self.index, "call_id": self.call_id, "tool": self.tool,
            "tier": self.tier, "eligible": self.eligible,
            "required_state": list(self.required_state),
            "refusals": [refusal.to_json() for refusal in self.refusals],
        }


def plan_call(call: dict, fixture: Fixture | None, *,
              decisions: dict[str, str] | None = None) -> CallPlan:
    """Decide one recorded call. ``decisions`` maps tool record id to approval status."""
    name = str(call.get("name") or "")
    record = str(call.get("id") or "")
    refusal: list[Refusal] = []
    required: tuple[str, ...] = ()

    decision = (decisions or {}).get(record)
    if decision in ("denied", "expired", "cancelled", "unwatched"):
        refusal.append(Refusal("approval_decision",
                               f"approval was {decision}; the tool never executed"))
    if str(call.get("status") or "") == "unknown" or _truthy(call.get("outcome_unknown")):
        refusal.append(Refusal("outcome_unknown",
                               "tool_calls.status is unknown or the result reports an "
                               "unconfirmed outcome"))

    external = catalog.external_reason(name)
    database = catalog.database_reason(name)
    context = catalog.context_reason(name)
    facts = catalog.facts(name)
    if external:
        refusal.append(Refusal("external_side_effect", external))
        tier = "external"
    elif database:
        refusal.append(Refusal("requires_database", database))
        tier = "stateful"
    elif context:
        refusal.append(Refusal("requires_run_context", context))
        tier = "stateful"
    elif name == "echo":
        # No workspace, no database, no network: the call is its own fixture.
        tier = "pure"
    elif facts is None:
        refusal.append(Refusal("undeclared_tier",
                               f"{name or '<unnamed>'} is not registered in a work-mode face"))
        tier = "unknown"
    elif facts.requires_host_execution:
        refusal.append(Refusal("external_side_effect", "the tool declares host execution"))
        tier = "external"
    elif not facts.requires_workspace:
        refusal.append(Refusal("missing_initial_state",
                               "the tool reads state outside the workspace fixture"))
        tier = "stateful"
    else:
        tier = "stateful"

    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    unsafe = path_problems(name, arguments)
    if unsafe:
        refusal.append(Refusal("unsafe_path", "; ".join(unsafe)))

    if tier == "stateful" and not refusal:
        required = required_state(name, arguments)
        if fixture is None:
            refusal.append(Refusal("missing_fixture",
                                   "no case fixture: the starting files are unknown"))
        else:
            absent = [path for path in required if not fixture.covers(path)]
            if absent:
                refusal.append(Refusal(
                    "missing_initial_state",
                    "fixture does not contain " + ", ".join(sorted(absent)[:3])))

    return CallPlan(int(call.get("index", 0)), record, name, tier, tuple(refusal), required)


def plan(calls, fixture: Fixture | None, *, run_id: str | None = None,
         decisions: dict[str, str] | None = None, fixture_note: str = "") -> dict:
    plans = [plan_call({**call, "index": index}, fixture, decisions=decisions)
             for index, call in enumerate(calls)]
    by_refusal: dict[str, int] = {}
    for item in plans:
        for refusal in item.refusals:
            by_refusal[refusal.code] = by_refusal.get(refusal.code, 0) + 1
    warnings = [
        "A call can carry more than one refusal, so by_refusal counts codes, not calls.",
        "Refusal is the result: no call in this plan was executed by planning it.",
    ]
    if fixture is None:
        warnings.append("No fixture was supplied; every stateful call is refused for "
                        "missing initial state rather than replayed against an empty directory.")
    elif fixture_note:
        warnings.append(fixture_note)
    document = {
        "plan_version": PLAN_VERSION,
        "run_id": run_id,
        "fixture": ({"files": len(fixture.files), "note": fixture_note}
                    if fixture is not None else None),
        "counts": {
            "calls": len(plans),
            "eligible": sum(1 for item in plans if item.eligible),
            "refused": sum(1 for item in plans if not item.eligible),
            "by_tier": _count(item.tier for item in plans),
            "by_refusal": dict(sorted(by_refusal.items())),
        },
        "calls": [item.to_json() for item in plans],
        "warnings": warnings,
    }
    return document


def format_plan(document: dict, *, limit: int = 200) -> str:
    counts = document["counts"]
    lines = [f"run {document['run_id'] or '<none>'}  {counts['calls']} recorded call(s): "
             f"{counts['eligible']} replayable, {counts['refused']} refused"]
    tiers = ", ".join(f"{name}={value}" for name, value in sorted(counts["by_tier"].items()))
    lines.append(f"tiers  {tiers or 'none'}")
    if counts["by_refusal"]:
        lines.append("refusals  " + ", ".join(f"{code}={value}"
                                              for code, value in counts["by_refusal"].items()))
    shown = 0
    for call in document["calls"]:
        if call["eligible"] and shown >= limit:
            continue
        codes = ", ".join(refusal["code"] for refusal in call["refusals"]) or "replayable"
        lines.append(f"  {call['index']:>3} {call['tool']:<18} {call['tier']:<9} {codes}")
        if call["refusals"]:
            for refusal in call["refusals"]:
                lines.append(f"        {refusal['code']}: {refusal['detail']}")
        shown += 1
        if shown >= limit:
            lines.append(f"  … {len(document['calls']) - shown} more call(s)")
            break
    for warning in document["warnings"]:
        lines.append("note: " + warning)
    return "\n".join(lines) + "\n"


def fixture_for_case(case: dict, *, repository: Path = REPOSITORY) -> tuple[Fixture | None, str]:
    """The case's declared starting state, or why it cannot be rebuilt here."""
    declared = case.get("fixture") if isinstance(case.get("fixture"), dict) else None
    if declared is None:
        return None, "the case declares no fixture"
    kind = declared.get("kind")
    if kind == "empty":
        return Fixture({}), "empty fixture from the case document"
    if kind == "tarball":
        return None, ("tarball fixtures are not unpacked: safe extraction needs its own "
                      "traversal and link checks, so this replay refuses instead")
    path = declared.get("path")
    if not isinstance(path, str) or not path:
        return None, "the case fixture declares no path"
    source = repository.resolve()
    for component in Path(path).parts:
        source = source / component
        if source.is_symlink():
            return None, "the case fixture path contains a symbolic link"
    root = (repository / path).resolve()
    if not root.is_relative_to(repository.resolve()):
        return None, "the case fixture path escapes the checkout"
    try:
        fixture = Fixture.from_directory(root)
    except FixtureInvalid as error:
        return None, f"the case fixture cannot be rebuilt: {error}"
    return fixture, f"{len(fixture.files)} file(s) rebuilt from {path}"


class ReplayPlanning:
    """Read-only load of one run's calls, then the pure eligibility rules."""

    def __init__(self, database) -> None:
        self.database = database

    async def plan(self, run_id, *, case: dict | None = None) -> dict:
        target = UUID(str(run_id))
        async with self.database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            calls, decisions = await self._load(connection, target)
        fixture, note = fixture_for_case(case) if case else (None, "no case supplied")
        return plan(calls, fixture, run_id=str(target), decisions=decisions, fixture_note=note)

    @staticmethod
    async def _load(connection, run_id: UUID) -> tuple[list[dict], dict[str, str]]:
        # Arguments are read because eligibility depends on the path they name.
        # The result body is not: what matters is whether a result exists and
        # whether it was a decision instead of an execution.
        cursor = await connection.execute(
            "SELECT t.id, t.call_id, t.name, t.status, t.attempt, t.call_index, t.arguments, "
            "t.result->>'outcome_unknown' AS outcome_unknown "
            "FROM tool_calls t LEFT JOIN model_calls m ON m.id = t.model_call_id "
            "WHERE t.run_id = %s "
            "ORDER BY m.iteration NULLS LAST, t.call_index, t.created_at", (run_id,))
        calls = [dict(row) for row in await cursor.fetchall()]
        cursor = await connection.execute(
            "SELECT tool_record_id, status FROM approval_requests "
            "WHERE run_id = %s AND tool_record_id IS NOT NULL", (run_id,))
        decisions = {str(row["tool_record_id"]): row["status"] for row in await cursor.fetchall()}
        return calls, decisions


def _truthy(value) -> bool:
    return value is True or value == "true"


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def load_case(path: Path) -> dict:
    from eval import casefile

    loaded = casefile.load(Path(path))
    return {**loaded.to_json(), "sha256": loaded.sha256}


__all__ = [
    "CallPlan", "PLAN_VERSION", "REFUSALS", "Refusal",
    "ReplayPlanning", "fixture_for_case", "format_plan", "load_case", "plan",
    "plan_call",
]
