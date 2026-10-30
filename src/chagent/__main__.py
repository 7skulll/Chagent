"""Default entry point: the local web app.

``python -m chagent`` and the ``chagent`` console script both land in the
browser. The command-line runner still exists as ``python -m chagent.cli`` but
it is frozen: new behaviour belongs to the web app.
"""

from chagent.web.__main__ import main

if __name__ == "__main__":
    main()
