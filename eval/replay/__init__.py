"""Replay: pure logic, isolated stateful calls, and model cassettes.

P3 of ``docs/智能体测评与自我迭代方案.md``. Three tiers, kept apart on purpose,
because they prove different things and fail in different ways:

``pure``      (``pure.py``)
    Fixed input, deterministic function, hand-authored expectation. No model,
    no database, no workspace. It proves a *contract* — path projection, retry
    classification — still behaves as documented. It cannot prove the agent
    does anything.

``sandbox``   (``sandbox.py``)
    One recorded call re-executed in a disposable directory rebuilt from a
    fixture. It proves the state change and the error contract for that input,
    in that fixture. It cannot prove the run that produced the record will
    happen again: the fixture is a reconstruction, not the original disk.

``cassette``  (``cassette.py``)
    Stored responses replayed against matching requests. It proves framework
    behaviour under fixed responses, and stops at the first request that no
    longer matches. It cannot prove a new model is better, and it does not
    rebuild text deltas, which were never stored.

What is deliberately absent: there is no runner, so nothing in this package
schedules a whole run, grants an approval, or reaches the network. A call that
would touch the host, a service or a database is *refused* with a reason by
``plan.py`` rather than simulated — historical arguments are replay candidates,
not fixtures, and an approval decision is evidence, never a reusable grant.

Submodules are imported by name (``from eval.replay import plan`` would bind the
function, not the module), and this file stays docstring-only so importing the
package never pulls the product tree in.
"""
