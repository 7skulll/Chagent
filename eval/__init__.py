"""Evaluation contracts: version manifests, cases, trials, running and reporting.

This package lives outside ``chagent`` on purpose: graders, cases and retained
expectations must not ship in the product wheel, and the trusted gate process is
meant to read them from a path the candidate source tree cannot write.

``runner`` is the one module that spends money: it builds a disposable workspace
from a case fixture, drives one real run, and grades it. ``prereg`` fixes what an
experiment promises before that happens, and ``report`` says what the resulting
counts support. Nothing here merges anything, and nothing here decides that a
candidate may ship — that is P5's gate, which does not exist yet.

``web`` / ``platform`` / ``platform_worker`` provide the independent local
operator UI and leased execution workflow; they reuse the same runner and never
auto-retry a paid slot after a crash.

The projections (``spans``, ``attribute``, ``replay``) remain read-only. A module
that pretended to more than it does would be the "declaration without
implementation" this project keeps fixing.
"""
