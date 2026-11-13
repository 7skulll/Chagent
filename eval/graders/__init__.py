"""Graders: the checks that decide whether a trial met the case.

A case declares *what* must hold (``expects``); a grader is the code that
decides *whether* it holds. The two are deliberately separate: a case is data a
reviewer reads, and a grader is code that must be tested, versioned and — in P5 —
kept out of reach of the candidate. If a case only carried prose, "the model
said it passed" would be the only available reading, which is exactly what this
project refuses.

Only deterministic checks are executable in P4. ``model_rubric`` and
``human_review`` are declared in cases but return ``inconclusive`` here, with the
reason attached: a rubric that has not been calibrated against human labels
cannot decide anything, and letting it decide would make an uncalibrated judge
the gate. That is a narrowing, not an oversight — see the scheme's section 3.1.

A grader that raises is ``infra_error``, never ``fail``. A broken check is not
evidence about the candidate.
"""

from __future__ import annotations

from eval.graders.deterministic import CHECKS, Facts, format_findings, grade_all

__all__ = ["CHECKS", "Facts", "format_findings", "grade_all"]
