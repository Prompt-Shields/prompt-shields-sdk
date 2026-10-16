"""`prompt-shields-instrument`: run an unmodified Python app under `instrument()`.

    PS_API_KEY=ps_... prompt-shields-instrument python app.py --port 8080
    PS_API_KEY=ps_... prompt-shields-instrument app.py --port 8080
    PS_API_KEY=ps_... python -m prompt_shields.autoinstrument gunicorn app:wsgi

Like `opentelemetry-instrument`, it puts a bootstrap directory holding a
`sitecustomize.py` first on `PYTHONPATH` and execs the command. Every Python
interpreter started under it (the command and its Python children) imports
that `sitecustomize` at startup, which calls `prompt_shields.instrument()`
with the configuration in `PS_*` environment variables (see `instrument()`),
plus `PS_INSTRUMENT_VENDORS` (comma-separated, e.g. `openai,anthropic`).

The bootstrap never stops the app: a missing `PS_API_KEY` or any other error
is logged as a warning and the app runs uninstrumented. A `sitecustomize`
that was already on the path (a distribution's, a virtualenv's) still runs,
before `instrument()`. Interpreters started with `-I` or `-E` ignore
`PYTHONPATH` and are not instrumented.
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger("prompt_shields.autoinstrument")

BOOTSTRAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "_bootstrap")
VENDORS_ENV = "PS_INSTRUMENT_VENDORS"

USAGE = """\
usage: prompt-shields-instrument COMMAND [ARGS ...]

Run COMMAND with prompt_shields.instrument() applied at interpreter startup.
COMMAND is a program (python, gunicorn, uvicorn, ...) or a .py script, which
is run with this interpreter. Configuration comes from the environment:
PS_API_KEY (required), PS_COLLECTOR_URL, PS_USE_CASE, PS_BUSINESS_UNIT,
PS_OWNER, PS_DATA_CLASSIFICATION, PS_ENVIRONMENT, PS_CALLING_SERVICE,
PS_ATLAS_URL, PS_ATLAS_API_KEY, PS_INSTRUMENT_VENDORS (e.g. openai,anthropic).
"""


def bootstrap() -> bool:
    """Called by the bootstrap `sitecustomize`. Never raises.

    Returns True when this call instrumented the interpreter.
    """
    try:
        import prompt_shields

        vendors = os.environ.get(VENDORS_ENV)
        kwargs = {}
        if vendors is not None and vendors.strip():
            kwargs["vendors"] = tuple(
                v.strip() for v in vendors.split(",") if v.strip())
        return prompt_shields.instrument(**kwargs)
    except Exception as exc:  # never stop the host app
        logger.warning("prompt-shields-instrument: not instrumenting (%s)", exc,
                       exc_info=not isinstance(exc, ValueError))
        return False


def _command(args: list[str]) -> list[str]:
    """A `.py` script (an existing file) runs with this interpreter."""
    if args[0].endswith(".py") and os.path.isfile(args[0]):
        return [sys.executable, *args]
    return args


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    if not args or args[0] in ("-h", "--help"):
        stream = sys.stdout if args else sys.stderr
        stream.write(USAGE)
        return 0 if args else 2

    pythonpath = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = (
        BOOTSTRAP_DIR + os.pathsep + pythonpath if pythonpath else BOOTSTRAP_DIR)
    command = _command(args)
    if os.name == "nt":  # no exec that replaces the process on Windows
        try:
            return subprocess.call(command)
        except OSError as exc:
            sys.stderr.write(f"prompt-shields-instrument: {command[0]}: {exc}\n")
            return 127
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"prompt-shields-instrument: {command[0]}: {exc}\n")
        return 127


if __name__ == "__main__":
    sys.exit(main())
