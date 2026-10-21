"""The one time limit for tests that wait for something to happen promptly.

These tests tell "happens" from "never happens" (a cancelled search that keeps
running, a UI that freezes). On shared CI runners a process can pause for a
noticeable part of a second, so a tight limit only adds random failures; it
catches no bug a generous one misses.
"""

PROMPT_LIMIT_S = 5.0
