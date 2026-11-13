"""Bundled workflows, exposed by name without opening the source filesystem."""

from importlib.resources import files


SKILL_NAMES = (
    "project-explorer", "debug-and-fix", "website-builder", "project-verifier", "run-diagnostician",
    "document-synthesizer", "change-reviewer", "task-handoff",
)


def read_skill(name: str) -> str:
    if name not in SKILL_NAMES:
        raise ValueError(f"Unknown builtin skill: {name}")
    return files(__package__).joinpath(name, "SKILL.md").read_text(encoding="utf-8")


def skill_catalog() -> str:
    # Bundled frontmatter deliberately uses plain, single-line string fields.
    # This is not a parser or discovery mechanism for user-supplied YAML.
    entries = []
    for name in SKILL_NAMES:
        header = read_skill(name).split("---", 2)[1]
        fields = dict(line.split(": ", 1) for line in header.strip().splitlines())
        if fields["name"] != name or not fields["description"].strip():
            raise ValueError(f"Invalid bundled skill metadata: {name}")
        entries.append(f"{name}: {fields['description']}")
    return "\n".join(entries)
