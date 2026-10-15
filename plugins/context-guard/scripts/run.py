"""Launcher for the context-guard hooks: `python run.py <Stop|PostToolUse|SessionStart>`.

On Python older than 3.9 it silently exits with code 0 without importing the other modules.
Any handler exception also yields exit code 0 and no output: the context-guard hook must never break
the session.
"""
# NOTE: this file is deliberately ASCII-only (an exception to the Russian-comments rule): the version
# gate must compile under any Python, where non-ASCII source without an encoding line is a SyntaxError.
import os
import sys

HANDLERS = {
    "Stop": "cg_stop",
    "PostToolUse": "cg_posttool",
    "SessionStart": "cg_session_start",
}


def main():
    """Pick the handler by the first argument and call its main()."""
    if sys.version_info < (3, 9):
        return
    if len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        module = __import__(HANDLERS[sys.argv[1]])
        module.main()
    except Exception:  # noqa: BLE001 - by contract any error must not break the session
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
