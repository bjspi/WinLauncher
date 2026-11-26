"""The one time limit for tests that wait for something to happen promptly.

These tests tell "happens" from "never happens" (a cancelled search that keeps
running, a UI that freezes). On shared CI runners a process can pause for a
noticeable part of a second, so a tight limit only adds random failures; it
catches no bug a generous one misses.
"""

PROMPT_LIMIT_S = 5.0
# A child WinLauncher process to start up or finish: it imports Qt and builds the
# whole application, which a busy runner can stretch from seconds to far more.
PROCESS_LIMIT_S = 60.0
