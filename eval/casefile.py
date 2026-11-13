"""Case documents: validation, loading and the hash a trial binds to.

A case is a question, not an answer. It carries the task, the starting fixture,
the authority it may run under, the budget, and the conditions that decide
whether the attempt passed. Trials never write back into a case: if the task or
the judging rule changes, ``version`` changes and old results stay attached to
the version that produced them.

The schema is JSON because ``jsonschema`` is already a dependency and because a
case has to survive being read by a gate process that will not import the
candidate's code.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from eval.manifests import CaseInvalid, sha256_json

SCHEMA_PATH = Path(__file__).with_name("schemas") / "case.schema.json"


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(document: dict) -> None:
    """Raise ``CaseInvalid`` with every violation, not only the first.

    One error at a time turns editing a case into a guessing game; the whole list
    is short enough to print and fixes all of it in one pass.
    """
    if not isinstance(document, dict):
        raise CaseInvalid("case must be a JSON object")
    problems = sorted(_validator().iter_errors(document), key=lambda error: list(error.path))
    if problems:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in problems[:8])
        raise CaseInvalid(f"{len(problems)} schema violation(s): {details}")


@dataclass(frozen=True)
class Case:
    id: str
    version: int
    title: str
    capability: str
    prompt: str
    authority: dict
    budget: dict
    graders: tuple[dict, ...]
    cleanup: str
    sha256: str
    fixture: dict | None = None
    notes: str = ""

    @property
    def key(self) -> str:
        """Identity that a result is allowed to be compared within."""
        return f"{self.id}@{self.version}"

    def to_json(self) -> dict:
        document = {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "capability": self.capability,
            "prompt": self.prompt,
            "authority": self.authority,
            "budget": self.budget,
            "graders": list(self.graders),
            "cleanup": self.cleanup,
        }
        if self.fixture is not None:
            document["fixture"] = self.fixture
        if self.notes:
            document["notes"] = self.notes
        return document


def load(path: Path) -> Case:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseInvalid(f"{path}: {error}") from None
    validate(document)
    graders = tuple(document["graders"])
    seen = [grader["id"] for grader in graders]
    if len(seen) != len(set(seen)):
        raise CaseInvalid(f"{path}: duplicate grader id")
    return Case(
        id=document["id"],
        version=document["version"],
        title=document["title"],
        capability=document["capability"],
        prompt=document["prompt"],
        authority=document["authority"],
        budget=document["budget"],
        graders=graders,
        cleanup=document["cleanup"],
        # The hash describes the document, so a re-indented file is still the
        # same case and a changed expectation is not.
        sha256=sha256_json(document),
        fixture=document.get("fixture"),
        notes=document.get("notes", ""),
    )


def load_all(directory: Path) -> list[Case]:
    """Every ``*.json`` case in a directory, refused if two share an identity."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    cases = [load(path) for path in sorted(directory.glob("*.json"))]
    keys = [case.key for case in cases]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise CaseInvalid(f"duplicate case identity: {', '.join(duplicates)}")
    return cases


def describe(case: Case) -> str:
    return f"{case.key}  {case.capability:<20} {case.title}  sha256={case.sha256[:12]}"


__all__ = ["Case", "CaseInvalid", "describe", "load", "load_all", "validate"]
