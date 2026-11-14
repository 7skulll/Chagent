"""Baseline against candidate, with the intervals that make a claim honest.

The plan forbids two things that are easy to do by accident: calling a handful of
runs "stable", and reading a difference out of two numbers without asking how
much of it could be noise. So this module reports, for every case and arm, the
counts it actually observed, a Wilson interval on each arm's pass rate, and a
Newcombe hybrid-score interval on the difference. A direction is only named when
that interval excludes zero *and* the difference clears the threshold the
protocol fixed beforehand.

Three kinds of absence are kept apart, because collapsing them is how a
convenient number appears. **Missing** slots were preregistered and never ran.
**Excluded** trials hit an infrastructure error and are out of the denominator
but stay in the report. **Inconclusive** trials ran and the graders could not
decide — a case with a prose-only grader lands here, and it is not a pass.

Nothing here decides pass or fail on its own; it reads trials that were already
graded. That is why the same code can be run by the gate later without trusting
the thing it measures.
"""

from __future__ import annotations

from math import sqrt
from typing import Iterable, Mapping

REPORT_VERSION = 3
Z = 1.96
#: A difference smaller than this is reported as "no difference observed" rather
#: than as a movement, regardless of what an interval says: at three repeats per
#: cell a single flip is a third of the rate.
NOISE_FLOOR = 0.0


def wilson(successes: int, total: int, z: float = Z) -> tuple[float, float] | None:
    """Score interval for one proportion; ``None`` when there is no denominator."""
    if total <= 0:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    spread = z * sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (round(max(0.0, centre - spread), 4), round(min(1.0, centre + spread), 4))


def newcombe(first: tuple[float, float] | None, second: tuple[float, float] | None,
             rate_first: float, rate_second: float) -> tuple[float, float] | None:
    """Hybrid score interval for ``rate_second - rate_first``.

    The unpaired form: the spread of each arm's Wilson interval is combined, so
    an arm with three observations widens the difference instead of being
    treated as precisely as an arm with thirty.
    """
    if first is None or second is None:
        return None
    low_first, high_first = first
    low_second, high_second = second
    delta = rate_second - rate_first
    lower = delta - sqrt((rate_second - low_second) ** 2 + (high_first - rate_first) ** 2)
    upper = delta + sqrt((high_second - rate_second) ** 2 + (rate_first - low_first) ** 2)
    return (round(max(-1.0, lower), 4), round(min(1.0, upper), 4))


def _cell(trials: Iterable, planned: int) -> dict:
    trials = list(trials)
    verdicts = [trial.verdict for trial in trials]
    passes = verdicts.count("pass")
    fails = verdicts.count("fail")
    inconclusive = verdicts.count("inconclusive")
    infra = verdicts.count("infra_error")
    denominator = passes + fails
    rate = passes / denominator if denominator else None
    return {
        "planned": planned,
        "observed": len(trials),
        "pass": passes,
        "fail": fails,
        "inconclusive": inconclusive,
        "infra_error": infra,
        "denominator": denominator,
        "rate": rate,
        "interval": wilson(passes, denominator),
        "verdicts": verdicts,
    }


def _direction(delta: dict, thresholds: Mapping) -> str:
    rate = delta["rate_candidate_minus_baseline"]
    interval = delta["interval"]
    if rate is None or interval is None:
        return "inconclusive"
    low, high = interval
    if low > 0 and rate >= float(thresholds.get("improve_points", 0)):
        return "improved"
    if high < 0 and -rate >= float(thresholds.get("regress_points", 0)):
        return "regressed"
    if low > 0:
        return "higher_but_below_threshold"
    if high < 0:
        return "lower_but_below_threshold"
    return "no_difference_observed"


def build(protocol, trials) -> dict:
    trials = [trial for trial in trials if trial.experiment == protocol.experiment]
    planned = {(case["id"] + "@" + str(case["version"])) for case in protocol.cases}
    arm_names = [arm.name for arm in protocol.arms]

    by_case: dict[str, dict[str, list]] = {key: {arm: [] for arm in arm_names} for key in planned}
    stray = []
    for trial in trials:
        key = f"{trial.case_id}@{trial.case_version}"
        arm = str(trial.manifest.get("arm", ""))
        if key not in by_case or arm not in by_case[key]:
            stray.append({"trial": trial.trial_id, "case": key, "arm": arm})
            continue
        by_case[key][arm].append(trial)

    cases = []
    for key in sorted(by_case):
        cells = {arm: _cell(by_case[key][arm], protocol.repeats) for arm in arm_names}
        baseline, candidate = cells[arm_names[0]], cells[arm_names[1]]
        delta = {"rate_candidate_minus_baseline": None, "interval": None, "direction": "inconclusive"}
        if baseline["rate"] is not None and candidate["rate"] is not None:
            change = candidate["rate"] - baseline["rate"]
            delta = {
                "rate_candidate_minus_baseline": change,
                "interval": newcombe(baseline["interval"], candidate["interval"],
                                     baseline["rate"], candidate["rate"]),
                "direction": _direction(
                    {"rate_candidate_minus_baseline": change,
                     "interval": newcombe(baseline["interval"], candidate["interval"],
                                          baseline["rate"], candidate["rate"])},
                    protocol.thresholds),
            }
        cases.append({"case": key, "arms": cells, "delta": delta})

    excluded = [{"trial": trial.trial_id, "case": f"{trial.case_id}@{trial.case_version}",
                 "arm": str(trial.manifest.get("arm", "")), "verdict": trial.verdict,
                 "status": trial.status,
                 "error": trial.manifest.get("harness", {}).get("error")}
                for trial in trials if trial.verdict in {"infra_error", "inconclusive"}]
    missing = [
        {"case": key, "arm": arm,
         "missing": protocol.repeats - len(by_case[key][arm])}
        for key in sorted(by_case) for arm in arm_names
        if len(by_case[key][arm]) < protocol.repeats
    ]
    unstable = [
        {"case": item["case"], "arm": arm,
         "verdicts": item["arms"][arm]["verdicts"]}
        for item in cases for arm in arm_names
        if len({v for v in item["arms"][arm]["verdicts"] if v in {"pass", "fail"}}) > 1
    ]
    directions = [item["delta"]["direction"] for item in cases]
    underpowered = [
        {"case": item["case"], "arm": arm, "observed": item["arms"][arm]["observed"]}
        for item in cases for arm in arm_names
        if item["arms"][arm]["denominator"] < protocol.min_repeats_for_stability
    ]
    invalid = _invalid_trials(protocol, trials)
    claim, reason = _claim(missing, directions)
    if invalid:
        claim, reason = "incomparable", "trial bindings or execution identities do not match the preregistration"
        for item in cases:
            item["delta"] = {"rate_candidate_minus_baseline": None, "interval": None,
                             "direction": "inconclusive"}
    if underpowered and not missing and not invalid:
        claim, reason = "inconclusive", "fewer decidable trials than the preregistered minimum"
    budget = _budget(protocol, trials)
    if not invalid and not missing and (budget['cost']['estimated_usd'] is None or budget['cost']['over_cap']
                        or budget['violations']):
        claim, reason = "inconclusive", "cost is unmeasured or a preregistered budget was violated"

    directions = [item['delta']['direction'] for item in cases]
    return {
        "report_version": REPORT_VERSION,
        "experiment": protocol.experiment,
        "protocol_sha256": protocol.sha256,
        "claim": claim,
        "claim_reason": reason,
        "schedule": {
            "planned": protocol.slots,
            "observed": sum(cell["observed"] for item in cases for cell in item["arms"].values()),
            "repeats": protocol.repeats,
            "min_repeats_for_stability": protocol.min_repeats_for_stability,
            "order": protocol.order,
        },
        "cases": cases,
        "counts": {
            "improved": directions.count("improved"),
            "regressed": directions.count("regressed"),
            "no_difference": directions.count("no_difference_observed"),
            "inconclusive": directions.count("inconclusive"),
            "higher_but_below_threshold": directions.count("higher_but_below_threshold"),
            "lower_but_below_threshold": directions.count("lower_but_below_threshold"),
        },
        "missing": missing,
        "excluded": excluded,
        "unstable": unstable,
        "stray": stray,
        "budget": budget,
        "invalid": invalid,
        "limitations": _limitations(protocol, underpowered),
    }


def _invalid_trials(protocol, trials):
    from eval.manifests import sha256_json
    from eval.integrity import valid_identity
    errors, seen_ids, seen_slots, identities = [], set(), set(), set()
    declared = {(c['id'], c['version']): c for c in protocol.cases}
    arms = {arm.name: arm for arm in protocol.arms}
    endpoints = {}
    for trial in trials:
        def bad(message):
            errors.append({'trial': trial.trial_id, 'reason': message})
        key = (trial.case_id, trial.case_version)
        arm_name = trial.manifest.get('arm')
        arm = arms.get(arm_name)
        if key not in declared or arm is None:
            bad('undeclared case or arm')
            continue
        if trial.manifest.get('protocol_sha256') != protocol.sha256:
            bad('protocol hash mismatch')
        expected = declared[key]
        if trial.case_sha256 != expected['sha256'] or trial.manifest.get('case') != expected:
            bad('case hash or version mismatch')
        if trial.trial_id in seen_ids:
            bad('duplicate trial identity')
        seen_ids.add(trial.trial_id)
        repeat = trial.manifest.get('repeat')
        if type(repeat) is not int or not 0 <= repeat < protocol.repeats:
            bad('missing or out-of-range repeat slot')
        else:
            slot = (key, arm_name, repeat)
            if slot in seen_slots:
                bad('duplicate repeat slot')
            seen_slots.add(slot)
        model = trial.manifest.get('model') or {}
        if model.get('provider') != arm.provider or model.get('requested_id') != arm.model:
            bad('requested model does not match arm')
        endpoints.setdefault(arm_name, set()).add(model.get('endpoint_sha256'))
        execution = trial.manifest.get('execution') or {}
        identity = execution.get('identity')
        digest = execution.get('identity_sha256')
        if (execution.get('version') != 3 or not valid_identity(identity)
                or digest != sha256_json(identity) or not identity.get('repo', {}).get('commit')):
            bad('missing or invalid execution identity')
        else:
            identities.add(digest)
            if execution.get('end_identity_sha256') != digest:
                bad('execution changed during the trial')
        if trial.manifest.get('harness', {}).get('client_kind') != 'live':
            bad('trial is not identified as real model evidence')
        if trial.manifest.get('harness', {}).get('cleanup_verified') is not True:
            bad('cleanup not verified')
    if len(identities) > 1:
        errors.append({'trial': '*', 'reason': 'code or runtime changed during the experiment'})
    if any(len(values) > 1 for values in endpoints.values()):
        errors.append({'trial': '*', 'reason': 'model endpoint changed within an arm'})
    return errors


def _claim(missing, directions) -> tuple[str, str]:
    # Missing slots are checked first because they are the only reason a cell can
    # hold fewer observations than the protocol promised; reporting "insufficient"
    # there would describe a choice that was never made.
    if missing:
        absent = sum(item["missing"] for item in missing)
        return "incomplete", (f"{absent} preregistered trial(s) have no record; an incomplete "
                              "schedule supports no comparison")
    if directions.count("regressed"):
        return "regression_detected", "at least one case moved the wrong way"
    if directions.count("improved"):
        return "improvement_supported", ("at least one case crossed the improvement threshold and none crossed "
                                         "the regression threshold at the preregistered repeat count")
    if set(directions) <= {"inconclusive"}:
        return "inconclusive", "no case produced a decidable denominator"
    return "no_difference_observed", ("no case crossed a preregistered direction threshold; consult each "
                                      "case's interval, including directions below the threshold")


def _budget(protocol, trials) -> dict:
    turns = [trial.usage.get("model_calls", 0) for trial in trials]
    tools = [trial.usage.get("tool_calls", 0) for trial in trials]
    wall = [trial.usage.get("wall_seconds", 0) for trial in trials]
    binding = [{"trial": trial.trial_id, "error": trial.manifest.get("harness", {}).get("error"),
                "status": trial.status}
               for trial in trials
               if trial.status in {"failed", "cancelled"}]
    costs = [(trial.usage.get("cost") or {}) for trial in trials]
    from eval.budgets import known_cost
    estimated = [item.get("usd") for item in costs if known_cost(item.get("usd"))]
    complete_cost = bool(costs) and len(estimated) == len(costs)
    violations = [trial.trial_id for trial in trials if any(
        trial.usage.get(field, 0) > protocol.budget[limit]
        for field, limit in (("model_calls", "max_turns"), ("tool_calls", "max_tool_calls"),
                             ("wall_seconds", "wall_seconds")))]
    tokens = [trial.usage.get("tokens") for trial in trials if trial.usage.get("tokens")]
    return {
        "declared": protocol.budget,
        "observed": {
            "max_turns": max(turns) if turns else None,
            "max_tool_calls": max(tools) if tools else None,
            "max_wall_seconds": max(wall) if wall else None,
            "total_turns": sum(turns),
            "total_tool_calls": sum(tools),
        },
        "binding": binding,
        "violations": violations,
        "cost": {
            "estimated_usd": round(sum(estimated), 6) if complete_cost else None,
            "known_subtotal_usd": round(sum(estimated), 6),
            "unknown_trials": len(costs) - len(estimated),
            "source": costs[0].get("source") if costs else "no trial recorded a cost",
            "tokens": {"input": sum(item.get("input", 0) for item in tokens),
                       "output": sum(item.get("output", 0) for item in tokens),
                       "cache_read": sum(item.get("cache_read", 0) for item in tokens),
                       "cache_write": sum(item.get("cache_write", 0) for item in tokens)} if tokens else None,
            "over_cap": (sum(estimated) > protocol.budget["total_cost_usd"]
                         if complete_cost or sum(estimated) > protocol.budget["total_cost_usd"] else None),
        },
        "headroom": ("a budget that was never approached cannot be calibrated from this run"
                     if not binding else
                     "at least one trial ended on a budget limit; the declared budget is binding"),
    }


def _limitations(protocol, underpowered) -> list[str]:
    limits = ["The comparison is between arms of one model-driven run; it says nothing about "
              "a different task, a different machine or a different prompt.",
              "A model alias that the provider resolves to a moving backend is recorded by the "
              "name that was requested; when the response does not name an immutable version, "
              "the arms are not pinned to an immutable model.",
              "A pass rate with a denominator of three has an interval nearly as wide as the "
              "range from 0 to 1; the interval column is the claim, the point estimate is not."]
    if underpowered:
        limits.append(f"{len(underpowered)} cell(s) ran fewer than "
                      f"{protocol.min_repeats_for_stability} trial(s); no stability is claimed.")
    if protocol.pricing is None:
        limits.append("No pricing was preregistered, so cost is unknown rather than zero.")
    return limits


def _table(header: tuple, rows: list, aligns: tuple) -> list[str]:
    """Lay a table out with one column width per table, not one width per report.

    Fixed widths used to run the columns into each other: a case id longer than its
    column pushed into the baseline cell, and a direction word longer than its column
    pushed into the candidate cell.  Widening a constant only moves the cliff, so the
    width is measured from the content and the last column is left unpadded.
    """
    widths = [max(len(row[index]) for row in (header, *rows)) for index in range(len(header))]
    lines = []
    for row in (header, *rows):
        cells = [text if index == len(header) - 1 else
                 (text.rjust(widths[index]) if aligns[index] == ">" else text.ljust(widths[index]))
                 for index, text in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
    return lines


def format_report(document: dict) -> str:
    lines = [
        f"experiment {document['experiment']}  claim: {document['claim']}",
        f"  {document['claim_reason']}",
        f"schedule   {document['schedule']['observed']}/{document['schedule']['planned']} "
        f"trial(s), {document['schedule']['repeats']} repeat(s) planned per cell",
        "",
    ]
    arms = list(document["cases"][0]["arms"]) if document["cases"] else ["baseline", "candidate"]
    rows = []
    for item in document["cases"]:
        cells = item["arms"]
        first, second = cells[arms[0]], cells[arms[1]]

        def cell_text(cell: dict) -> str:
            if cell["rate"] is None:
                return f"{cell['pass']}/{cell['denominator']} n/a"
            low, high = cell["interval"]
            return f"{cell['pass']}/{cell['denominator']} [{low:.2f},{high:.2f}]"

        delta = item["delta"]
        if delta["rate_candidate_minus_baseline"] is None:
            delta_text = "n/a"
        else:
            low, high = delta["interval"]
            delta_text = (f"{delta['rate_candidate_minus_baseline']:+.2f} "
                          f"[{low:+.2f},{high:+.2f}] {delta['direction']}")
        rows.append((item["case"], cell_text(first), cell_text(second), delta_text))
    lines.extend(_table(("case", "baseline", "candidate", "delta [95%]"), rows, ("<", ">", ">", "<")))
    for problem in document.get("invalid", ()):
        lines.append(f"invalid: {problem['trial']}: {problem['reason']}")
    if document["missing"]:
        lines.append("")
        lines.append("missing: " + ", ".join(f"{item['case']}/{item['arm']} -{item['missing']}"
                                             for item in document["missing"][:6]))
    if document["excluded"]:
        lines.append("excluded: " + ", ".join(f"{item['trial']}={item['verdict']}"
                                              for item in document["excluded"][:6]))
    if document["unstable"]:
        lines.append("unstable: " + ", ".join(f"{item['case']}/{item['arm']}={item['verdicts']}"
                                              for item in document["unstable"][:4]))
    budget = document["budget"]
    lines.append("")
    lines.append(f"budget     declared {budget['declared']}")
    lines.append(f"           observed {budget['observed']}")
    lines.append(f"           cost {budget['cost']['estimated_usd']} "
                 f"({budget['cost']['source']}), tokens {budget['cost']['tokens']}")
    lines.append("")
    for limit in document["limitations"]:
        lines.append("limit: " + limit)
    return "\n".join(lines) + "\n"


__all__ = ["NOISE_FLOOR", "REPORT_VERSION", "build", "format_report", "newcombe", "wilson"]
