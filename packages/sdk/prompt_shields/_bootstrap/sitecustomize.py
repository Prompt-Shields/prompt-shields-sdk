"""Applies `prompt_shields.instrument()` at interpreter startup.

Put first on `PYTHONPATH` by `prompt-shields-instrument` (see
`prompt_shields.autoinstrument`). It first runs the `sitecustomize` it
shadows, if any, then instruments from `PS_*` environment variables. It never
raises: an error is logged as a warning and the app runs uninstrumented.
Standard library only up to the `prompt_shields` import, so the chained
`sitecustomize` runs even when `prompt_shields` cannot be imported.
"""


def _ps_chain_sitecustomize() -> None:
    import importlib.machinery
    import importlib.util
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))

    def other(entry):
        try:
            return not os.path.samefile(entry or os.curdir, here)
        except OSError:
            return True

    spec = importlib.machinery.PathFinder.find_spec(
        "sitecustomize", [p for p in sys.path if other(p)])
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    ours = sys.modules.get("sitecustomize")
    sys.modules["sitecustomize"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if ours is not None:
            sys.modules["sitecustomize"] = ours
        import logging

        logging.getLogger("prompt_shields.autoinstrument").warning(
            "prompt-shields-instrument: error in the existing sitecustomize "
            "(%s)", spec.origin, exc_info=True)


try:
    _ps_chain_sitecustomize()
except Exception:  # noqa: BLE001, S110 - never stop the host app
    pass

try:
    from prompt_shields.autoinstrument import bootstrap as _ps_bootstrap
except Exception as _ps_exc:  # noqa: BLE001 - prompt_shields not importable
    import sys as _ps_sys

    _ps_sys.stderr.write(
        f"prompt-shields-instrument: not instrumenting ({_ps_exc!r})\n")
else:
    _ps_bootstrap()
