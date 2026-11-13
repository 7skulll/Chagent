"""Trial records: one attempt at one case, and the file store it lands in.

A trial is not "the run finished". The run's terminal status is control flow; the
verdict comes from graders reading the environment, the artifacts and — where the
task is textual — the report. Keeping those two in one record is what lets a
later reader see "completed but failed", which is the ordinary shape of a wrong
answer delivered confidently.

Storage is one JSON file per trial plus an append-only JSONL index. Files diff and
travel with a checkout; a database would add a migration, a role and a cleanup
duty before the first comparison has been run.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
from uuid import uuid4

OUTCOMES = ("pass", "fail", "inconclusive", "infra_error")
STATUSES = ("completed", "failed", "cancelled", "interrupted", "infra_error")
EXPERIMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TRIAL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")


class TrialInvalid(ValueError):
    pass


@dataclass(frozen=True)
class GraderResult:
    grader_id: str
    result: str
    evidence: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.result not in OUTCOMES:
            raise TrialInvalid(f"unknown grader outcome: {self.result}")
        # Round-tripping through JSON hands back a list; a record read from disk
        # must compare equal to the one that was written.
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_json(self) -> dict:
        return {"grader_id": self.grader_id, "result": self.result,
                "evidence": list(self.evidence), "notes": self.notes}


@dataclass(frozen=True)
class TrialRecord:
    trial_id: str
    experiment: str
    case_id: str
    case_version: int
    case_sha256: str
    status: str
    manifest: dict
    graders_declared: int
    grader_ids: tuple[str, ...] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    run_id: str | None = None
    usage: dict = field(default_factory=dict)
    graders: tuple[GraderResult, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.trial_id, str) or not TRIAL_ID.fullmatch(self.trial_id):
            raise TrialInvalid("trial_id must be a bounded filename identifier")
        if self.status not in STATUSES:
            raise TrialInvalid(f"unknown trial status: {self.status}")
        if not EXPERIMENT.fullmatch(self.experiment):
            raise TrialInvalid(f"invalid experiment name: {self.experiment!r}")
        if self.graders_declared < 0:
            raise TrialInvalid("graders_declared must not be negative")
        ids = [grader.grader_id for grader in self.graders]
        if len(ids) != len(set(ids)):
            raise TrialInvalid("duplicate grader result")
        if self.grader_ids is not None:
            object.__setattr__(self, "grader_ids", tuple(self.grader_ids))
            if (len(self.grader_ids) != self.graders_declared or
                    len(set(self.grader_ids)) != len(self.grader_ids) or
                    any(not isinstance(identity, str) or not identity for identity in self.grader_ids)):
                raise TrialInvalid("declared grader identities do not match the count")
            if set(ids) - set(self.grader_ids):
                raise TrialInvalid("result from undeclared grader")

    @property
    def verdict(self) -> str:
        """The aggregate outcome, and the only place the aggregation rule lives.

        A missing grader is ``inconclusive``, never ``pass``: silently dropping
        the check that would have failed is how a broken trial becomes a green
        one. ``infra_error`` outranks a failure because the failure may not be
        the candidate's.
        """
        results = [grader.result for grader in self.graders]
        if (self.manifest.get("harness", {}).get("cleanup_verified") is False
                or self.status == "infra_error" or "infra_error" in results):
            return "infra_error"
        if self.manifest.get("harness", {}).get("budget_violation"):
            return "fail"
        if self.grader_ids is None or {g.grader_id for g in self.graders} != set(self.grader_ids):
            return "inconclusive"
        if not results:
            return "inconclusive"
        if "fail" in results:
            return "fail"
        if "inconclusive" in results:
            return "inconclusive"
        return "pass"

    def with_graders(self, graders: tuple[GraderResult, ...]) -> "TrialRecord":
        return replace(self, graders=graders)

    def to_json(self) -> dict:
        return {
            "record_version": 2,
            "trial_id": self.trial_id,
            "experiment": self.experiment,
            "case": {"id": self.case_id, "version": self.case_version, "sha256": self.case_sha256},
            "status": self.status,
            "verdict": self.verdict,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "run_id": self.run_id,
            "usage": self.usage,
            "graders_declared": self.graders_declared,
            "grader_ids": list(self.grader_ids) if self.grader_ids is not None else None,
            "graders": [grader.to_json() for grader in self.graders],
            "manifest": self.manifest,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, document: dict) -> "TrialRecord":
        case = document.get("case") or {}
        graders = tuple(GraderResult(**grader) for grader in document.get("graders") or ())
        return cls(
            trial_id=document["trial_id"],
            experiment=document["experiment"],
            case_id=case["id"],
            case_version=case["version"],
            case_sha256=case["sha256"],
            status=document["status"],
            manifest=document.get("manifest") or {},
            graders_declared=document.get("graders_declared", len(graders)),
            grader_ids=document.get("grader_ids"),
            started_at=document.get("started_at"),
            ended_at=document.get("ended_at"),
            run_id=document.get("run_id"),
            usage=document.get("usage") or {},
            graders=graders,
            notes=document.get("notes", ""),
        )


class TrialStore:
    """``<root>/<experiment>/<trial_id>.json`` plus ``index.jsonl`` beside it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def directory(self, experiment: str) -> Path:
        if not EXPERIMENT.fullmatch(experiment):
            raise TrialInvalid(f"invalid experiment name: {experiment!r}")
        directory = self.root / experiment
        if not directory.resolve().is_relative_to(self.root.resolve()):
            raise TrialInvalid("experiment directory escapes the result root")
        return directory

    def write(self, trial: TrialRecord) -> Path:
        directory = self.directory(trial.experiment)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{trial.trial_id}.json"
        from eval.integrity import publish

        try:
            publish(path, trial.to_json())
        except ValueError as error:
            raise TrialInvalid(str(error)) from error
        # One append per trial, written with a single syscall so a reader never
        # sees half a line. The record file is authoritative; the index is a
        # convenience that can be rebuilt from the directory.
        line = json.dumps({"trial_id": trial.trial_id, "case_id": trial.case_id,
                           "case_version": trial.case_version, "status": trial.status,
                           "verdict": trial.verdict, "started_at": trial.started_at,
                           "run_id": trial.run_id}, ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(directory / "index.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        return path

    def read(self, experiment: str, *, ignore: tuple[str, ...] = ()) -> list[TrialRecord]:
        """Every trial record in the experiment directory.

        ``ignore`` names files that live beside the trials but are not trials —
        the preregistration, for one. It is a parameter rather than a silent
        parse failure: a file this store cannot read is either a trial that must
        be seen or a mistake the caller has to name.
        """
        directory = self.directory(experiment)
        if not directory.is_dir():
            return []
        return [TrialRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(directory.glob("*.json"))
                if path.name not in ignore]


def new_trial(case, manifest: dict, experiment: str, *, trial_id: str | None = None,
              status: str = "completed", run_id: str | None = None,
              started_at: str | None = None, ended_at: str | None = None,
              usage: dict | None = None) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id if trial_id is not None else str(uuid4()),
        experiment=experiment,
        case_id=case.id,
        case_version=case.version,
        case_sha256=case.sha256,
        status=status,
        manifest=manifest,
        graders_declared=len(case.graders),
        grader_ids=tuple(grader["id"] for grader in case.graders),
        started_at=started_at,
        ended_at=ended_at,
        run_id=run_id,
        usage=usage or {},
    )
