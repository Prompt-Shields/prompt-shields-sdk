import json
import shutil
from pathlib import Path

import pytest
from prompt_shields.scan import main, scan_path, scan_text, to_sarif

FIXTURE = Path(__file__).parent / "fixtures" / "scan_repo"
FAKE_SECRET = "fake-secret-value-do-not-print-0123456789"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The static fixture repo plus files that must be skipped."""
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    # Secrets file: must never be opened or echoed.
    (root / ".env").write_text(f"OPENAI_API_KEY={FAKE_SECRET}\n")
    (root / ".env.local").write_text(f"ANTHROPIC_API_KEY={FAKE_SECRET}\n")
    # Skipped directories.
    for d in (".git", "node_modules/openai", ".venv/lib", "custom_env"):
        (root / d).mkdir(parents=True)
    (root / "node_modules/openai/index.js").write_text(
        'import x from "openai";\nconst m = "gpt-4-turbo";\n'
    )
    (root / ".git/hook.py").write_text("import openai\n")
    (root / ".venv/lib/x.py").write_text("import cohere\n")
    (root / "custom_env/pyvenv.cfg").write_text("home = /usr\n")
    (root / "custom_env/y.py").write_text("import cohere\n")
    # Binary file with a source extension.
    (root / "blob.py").write_bytes(b"\x00\x01import openai\x00")
    return root


def _kinds(report, kind):
    return {(f["vendor"], f["value"]) for f in report["findings"] if f["kind"] == kind}


def test_python_imports_clients_models(repo):
    report = scan_path(repo)
    imports = _kinds(report, "import")
    assert ("openai", "openai") in imports
    assert ("anthropic", "anthropic") in imports
    assert ("google", "google.genai") in imports
    assert ("litellm", "litellm") in imports
    assert ("openai", "langchain_openai") in imports
    assert ("llama_index", "llama_index.core") in imports

    clients = _kinds(report, "client")
    assert ("openai", "OpenAI") in clients
    assert ("anthropic", "anthropic.Anthropic") in clients
    assert ("google", "genai.Client") in clients
    assert ("bedrock", "boto3.client('bedrock-runtime')") in clients
    assert ("litellm", "litellm.completion") in clients

    models = _kinds(report, "model")
    for expected in [
        ("openai", "gpt-4o-mini"),
        ("anthropic", "claude-sonnet-4-5"),
        ("bedrock", "anthropic.claude-3-haiku-20240307-v1:0"),
        ("openai", "gpt-4.1"),
        ("mistral", "mistral-large-latest"),
    ]:
        assert expected in models


def test_other_languages(repo):
    report = scan_path(repo)
    by_lang = {(f["language"], f["kind"], f["value"]) for f in report["findings"]}
    assert ("typescript", "import", "@anthropic-ai/sdk") in by_lang
    assert ("typescript", "import", "@aws-sdk/client-bedrock-runtime") in by_lang
    assert ("typescript", "import", "openai") in by_lang
    assert ("typescript", "client", "BedrockRuntimeClient") in by_lang
    assert ("typescript", "model", "gemini-2.5-flash") in by_lang
    assert (
        "java",
        "import",
        "com.anthropic.client.okhttp.AnthropicOkHttpClient",
    ) in by_lang
    assert ("java", "client", "AnthropicOkHttpClient") in by_lang
    assert ("java", "model", "claude-opus-4-1") in by_lang
    assert ("go", "import", "github.com/openai/openai-go") in by_lang
    assert ("go", "env_var", "OPENAI_API_KEY") in by_lang
    assert ("go", "model", "gpt-4o") in by_lang
    assert ("config", "endpoint", "https://api.mistral.ai/v1") in by_lang
    assert ("config", "env_var", "MISTRAL_API_KEY") in by_lang


def test_endpoints_drop_query_strings(repo):
    endpoints = _kinds(scan_path(repo), "endpoint")
    assert ("openai", "https://api.openai.com/v1") in endpoints
    assert (
        "google",
        "https://generativelanguage.googleapis.com/v1beta/models",
    ) in endpoints
    assert not any("?" in value for _, value in endpoints)


def test_skips_secrets_vendored_venvs_and_binaries(repo):
    report = scan_path(repo)
    paths = {f["path"] for f in report["findings"]}
    assert not any(
        p.startswith((".git/", "node_modules/", ".venv/", "custom_env/", ".env"))
        for p in paths
    )
    assert "blob.py" not in paths
    assert "notes.txt" not in paths
    assert FAKE_SECRET not in json.dumps(report)
    assert ("cohere", "cohere") not in _kinds(report, "import")


def test_size_bound(repo):
    big = repo / "big.py"
    big.write_text("import mistralai\n" + "#" * 2000)
    report = scan_path(repo, max_file_bytes=1000)
    assert "big.py" not in {f["path"] for f in report["findings"]}
    assert report["files_skipped"] >= 1


def test_env_var_name_only():
    findings = scan_text(
        'key = os.environ["OPENAI_API_KEY"]  # sk-not-real\n', "a.py", "python"
    )
    env = [f for f in findings if f.kind == "env_var"]
    assert [(f.value, f.line) for f in env] == [("OPENAI_API_KEY", 1)]
    assert "sk-not-real" not in json.dumps([f.__dict__ for f in findings])


def test_unparseable_python_falls_back_to_regex():
    findings = scan_text(
        'import cohere\ndef oops(:\n    return "command-r-plus"\n', "b.py", "python"
    )
    assert {(f.kind, f.value) for f in findings} >= {
        ("import", "cohere"),
        ("model", "command-r-plus"),
    }


def test_prose_is_not_a_model():
    assert scan_text('x = "the o1 thing"\ny = "rerank-model"\n', "c.py", "python") == []


def test_events_match_collector_ingest_shape(repo):
    events = scan_path(repo)["events"]
    assert events
    assert all(e["source"] == "code_scan" for e in events)
    assert all(
        set(e) == {"vendor", "model", "source", "calling_service"} for e in events
    )
    pairs = {(e["vendor"], e["model"]) for e in events}
    assert ("anthropic", "claude-sonnet-4-5") in pairs
    # Frameworks are findings but not provider sightings.
    assert not any(
        e["vendor"] in ("langchain", "litellm", "llama_index") for e in events
    )


def test_sarif(repo):
    sarif = to_sarif(scan_path(repo))
    assert sarif["version"] == "2.1.0"
    results = sarif["runs"][0]["results"]
    assert results and all(r["ruleId"].startswith("ps-scan/") for r in results)


def test_cli_formats(repo, capsys, tmp_path):
    assert main([str(repo)]) == 0
    out = capsys.readouterr().out
    assert "ps-scan:" in out and "anthropic" in out

    assert main([str(repo), "--format", "events"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["events"]

    dest = tmp_path / "report.json"
    assert main([str(repo), "--format", "json", "-o", str(dest)]) == 0
    report = json.loads(dest.read_text())
    assert report["schema_version"] == 1 and report["findings"]


def test_cli_missing_path(tmp_path):
    with pytest.raises(SystemExit):
        main([str(tmp_path / "nope")])
