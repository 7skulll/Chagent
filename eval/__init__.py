"""Evaluation contracts: version manifests, cases, trials and read-only evidence.

P0 of ``docs/智能体测评与自我迭代方案.md``. This package lives outside
``chagent`` on purpose: graders, cases and retained expectations must not ship in
the product wheel, and the trusted gate process is meant to read them from a path
the candidate source tree cannot write.

What is here now is the vocabulary — how a trial says which version, case and
judging rule produced it — plus read-only projections (timeline, attribution,
replay). There is still no runner: nothing here schedules a run or decides that a
candidate may merge, and ``replay`` replays one call or one recorded request
sequence rather than a whole run. A module that pretended otherwise would be the
"declaration without implementation" this project keeps fixing.
"""
