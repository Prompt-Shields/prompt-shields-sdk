"""`prompt-shields-instrument`: an unmodified app run under the entry point
emits events; the same app run without it emits none.

The app is a script that never imports prompt_shields: it calls the real
openai SDK over a mocked HTTP transport. Each run is a subprocess (the entry
point execs the command) reporting to a local HTTP collector.
"""

import os
import subprocess
import sys
import textwrap

import pytest
from prompt_shields import autoinstrument

from . import test_exit_flush
from .test_exit_flush import SDK_DIR, _Collector

collector = test_exit_flush.collector  # the local HTTP collector fixture

pytest.importorskip("openai")

APP = textwrap.dedent("""
    import sys

    import httpx
    import openai

    http = getattr(openai._base_client, "httpx2", None) or httpx
    body = {"id": "c", "object": "chat.completion", "created": 0,
            "model": "gpt-4o-2024-08-06",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                      "total_tokens": 10}}
    transport = http.MockTransport(lambda r: http.Response(200, json=body))
    client = openai.OpenAI(api_key="sk", http_client=http.Client(transport=transport))
    reply = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}])
    print("app:", reply.choices[0].message.content, *sys.argv[1:])
""")


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "app.py"
    path.write_text(APP)
    return str(path)


def _env(url, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and not k.startswith("PS_")
           and k != "PYTHONPATH"}
    env.update(PYTHONPATH=SDK_DIR, NO_PROXY="*", PS_API_KEY="ps_key",
               PS_COLLECTOR_URL=url, PS_USE_CASE="support-bot", **extra)
    return env


def _run(args, env, cwd=None):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return result


def _entry_point(*args):
    return [sys.executable, "-m", "prompt_shields.autoinstrument", *args]


def test_unmodified_app_emits_events_only_under_the_entry_point(collector, app):
    # Without the entry point: the app works and nothing is reported.
    result = _run([sys.executable, app, "--flag"], _env(collector))
    assert result.stdout.strip() == "app: hi --flag"
    assert _Collector.received == []

    # Under it: same app, same output, one event with env configuration.
    result = _run(_entry_point(sys.executable, app, "--flag"), _env(collector))
    assert result.stdout.strip() == "app: hi --flag"
    assert len(_Collector.received) == 1
    event = _Collector.received[0]
    assert event["vendor"] == "openai"
    assert event["model"] == "gpt-4o"
    assert event["tokens_in"] == 7
    assert event["tokens_out"] == 3
    assert event["use_case_name"] == "support-bot"


def test_a_bare_script_runs_with_this_interpreter(collector, app):
    result = _run(_entry_point(app), _env(collector))
    assert result.stdout.strip() == "app: hi"
    assert len(_Collector.received) == 1


def test_vendor_selection_from_the_environment(collector, app):
    _run(_entry_point(app), _env(collector, PS_INSTRUMENT_VENDORS="anthropic"))
    assert _Collector.received == []


def test_a_missing_api_key_warns_and_the_app_still_runs(collector, app):
    env = _env(collector)
    del env["PS_API_KEY"]
    result = _run(_entry_point(app), env)
    assert result.stdout.strip() == "app: hi"
    assert "not instrumenting" in result.stderr
    assert "PS_API_KEY" in result.stderr
    assert _Collector.received == []


def test_an_existing_sitecustomize_still_runs(collector, app, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "import os\nos.environ['PS_OWNER'] = 'from-sitecustomize'\n"
        "print('chained sitecustomize')\n")
    env = _env(collector)
    env["PYTHONPATH"] = os.pathsep.join([str(site), SDK_DIR])
    result = _run(_entry_point(app), env)
    # It ran once in the launcher and once in the app, before instrument()
    # (which read the variable it set).
    assert result.stdout.splitlines() == [
        "chained sitecustomize", "chained sitecustomize", "app: hi"]
    assert len(_Collector.received) == 1
    assert _Collector.received[0]["owner_email"] == "from-sitecustomize"


def test_a_failing_sitecustomize_does_not_stop_the_app(collector, app, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text("raise RuntimeError('broken site')\n")
    env = _env(collector)
    env["PYTHONPATH"] = os.pathsep.join([str(site), SDK_DIR])
    result = _run(_entry_point(app), env)
    assert result.stdout.strip() == "app: hi"
    assert "broken site" in result.stderr
    assert len(_Collector.received) == 1


def test_an_unimportable_sdk_does_not_stop_the_app(tmp_path):
    # The child interpreter finds a prompt_shields that fails to import: the
    # app runs, with a warning.
    broken = tmp_path / "broken" / "prompt_shields"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise ImportError('no sdk here')\n")
    script = tmp_path / "plain.py"
    script.write_text("print('plain app')\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = os.pathsep.join(
        [autoinstrument.BOOTSTRAP_DIR, str(broken.parent)])
    result = _run([sys.executable, str(script)], env, cwd=str(tmp_path))
    assert result.stdout.strip() == "plain app"
    assert "not instrumenting" in result.stderr
    assert "no sdk here" in result.stderr


def test_usage_and_unknown_command(capsys):
    assert autoinstrument.main([]) == 2
    assert "usage: prompt-shields-instrument" in capsys.readouterr().err
    assert autoinstrument.main(["--help"]) == 0
    assert "usage: prompt-shields-instrument" in capsys.readouterr().out
    old = os.environ.get("PYTHONPATH")
    try:
        assert autoinstrument.main(["--", "no-such-command-ps-test"]) == 127
        assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == (
            autoinstrument.BOOTSTRAP_DIR)
    finally:
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old
    assert "no-such-command-ps-test" in capsys.readouterr().err


def test_the_console_script_is_declared():
    import tomllib

    with open(os.path.join(SDK_DIR, "pyproject.toml"), "rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]
    assert scripts["prompt-shields-instrument"] == (
        "prompt_shields.autoinstrument:main")
