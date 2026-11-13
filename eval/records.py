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
    started_at: str | None = None
    ended_at: str | None = None
    run_id: str | None = None
    usage: dict = field(default_factory=dict)
    graders: tuple[GraderResult, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise TrialInvalid(f"unknown trial status: {self.status}")
        if not EXPERIMENT.match(self.experiment):
            raise TrialInvalid(f"invalid experiment name: {self.experiment!r}")
        if self.graders_declared < 0:
            raise TrialInvalid("graders_declared must not be negative")
        ids = [grader.grader_id for grader in self.graders]
        if len(ids) != len(set(ids)):
            raise TrialInvalid("duplicate grader result")

    @property
    def verdict(self) -> str:
        """The aggregate outcome, and the only place the aggregation rule lives.

        A missing grader is ``inconclusive``, never ``pass``: silently dropping
        the check that would have failed is how a broken trial becomes a green
        one. ``infra_error`` outranks a failure because the failure may not be
        the candidate's.
        """
        results = [grader.result for grader in self.graders]
        if "infra_error" in results:
            return "infra_error"
        if len(self.graders) < self.graders_declared:
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
        if not EXPERIMENT.match(experiment):
            raise TrialInvalid(f"invalid experiment name: {experiment!r}")
        return self.root / experiment

    def write(self, trial: TrialRecord) -> Path:
        directory = self.directory(trial.experiment)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{trial.trial_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(trial.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        os.replace(temporary, path)
        # One append per trial, written with a single syscall so a reader never
        # sees half a line. The record file is authoritative; the index is a
        # convenience that can be rebuilt from the directory.
        line = json.dumps({"trial_id": trial.trial_id, "case_id": trial.case_id,
                           "case_version": trial.case_version, "status": trial.status,
                           "verdict": trial.verdict, "started_at": trial.started_at,
                           "run_id": trial.run_id}, ensure_ascii=False, sort_keys=True) + "\n"
        with open(directory / "index.jsonl", "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        return path

    def read(self, experiment: str) -> list[TrialRecord]:
        directory = self.directory(experiment)
        if not directory.is_dir():
            return []
        return [TrialRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(directory.glob("*.json"))]


def new_trial(case, manifest: dict, experiment: str, *, trial_id: str | None = None,
              status: str = "completed", run_id: str | None = None,
              started_at: str | None = None, ended_at: str | None = None,
              usage: dict | None = None) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id or str(uuid4()),
        experiment=experiment,
        case_id=case.id,
        case_version=case.version,
        case_sha256=case.sha256,
        status=status,
        manifest=manifest,
        graders_declared=len(case.graders),
        started_at=started_at,
        ended_at=ended_at,
        run_id=run_id,
        usage=usage or {},
    )
