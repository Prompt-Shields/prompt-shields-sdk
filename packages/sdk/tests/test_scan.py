import json
import os
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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs and symlinks")
def test_skips_symlinks_and_fifos(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("import mistralai\n")
    (repo / "link.py").symlink_to(outside / "secret.py")
    (repo / "linkdir").symlink_to(outside, target_is_directory=True)
    # A FIFO with a source extension must not block the scan.
    os.mkfifo(repo / "pipe.py")
    report = scan_path(repo)
    paths = {f["path"] for f in report["findings"]}
    assert not any(p.startswith(("link.py", "linkdir/", "pipe.py")) for p in paths)
    assert ("mistral", "mistralai") not in _kinds(report, "import")


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


# --------------------------------------------------------------------------
# --exclude / --fail-on
# --------------------------------------------------------------------------


def _paths(report):
    return {f["path"] for f in report["findings"]}


def test_exclude_directory_by_name_and_path(repo):
    assert any(p.startswith("go/") for p in _paths(scan_path(repo)))
    by_name = _paths(scan_path(repo, exclude=["go"]))
    assert by_name and not any(p.startswith("go/") for p in by_name)
    by_path = _paths(scan_path(repo, exclude=["web/src/", "./java"]))
    assert not any(p.startswith(("web/", "java/")) for p in by_path)
    assert any(p.startswith("py/") for p in by_path)


def test_exclude_file_glob(repo):
    paths = _paths(scan_path(repo, exclude=["*.yaml", "py/app.py"]))
    assert "config.yaml" not in paths and "py/app.py" not in paths
    assert "py/frameworks.py" in paths


def test_exclude_does_not_match_partial_names(repo):
    # "py" excludes the py/ directory but not files that merely contain "py".
    paths = _paths(scan_path(repo, exclude=["py"]))
    assert not any(p.startswith("py/") for p in paths)
    assert "config.yaml" in paths


def test_cli_exclude_repeatable_and_csv(repo, capsys):
    argv = [str(repo), "--format", "json", "--exclude", "go,java", "--exclude", "web"]
    assert main(argv) == 0
    paths = _paths(json.loads(capsys.readouterr().out))
    assert paths and all(p.startswith("py/") or p == "config.yaml" for p in paths)


def test_cli_fail_on_any(repo, capsys):
    assert main([str(repo), "--fail-on", "any"]) == 1
    assert "matched --fail-on" in capsys.readouterr().err


def test_cli_fail_on_vendor_and_kind(repo, capsys, tmp_path):
    assert main([str(repo), "--fail-on", "anthropic"]) == 1
    assert main([str(repo), "--fail-on", "portkey"]) == 0  # known, no match
    assert main([str(repo), "--fail-on", "env_var,nonexistent"]) == 1
    # The report is still written before the non-zero exit.
    dest = tmp_path / "out.sarif"
    argv = [str(repo), "--format", "sarif", "-o", str(dest), "--fail-on", "model"]
    assert main(argv) == 1
    assert json.loads(dest.read_text())["runs"][0]["results"]
    capsys.readouterr()


def test_cli_fail_on_warns_on_unknown_tokens(repo, capsys):
    assert main([str(repo), "--fail-on", "Anthropc,ANTHROPIC,env-var"]) == 1
    err = capsys.readouterr().err
    assert "warning: ignoring unknown --fail-on token(s): anthropc, env-var" in err
    assert "env_var" in err and "matched --fail-on" in err


def test_cli_fail_on_all_unknown_is_usage_error(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        main([str(repo), "--fail-on", "antropic", "--fail-on", "openia"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "--fail-on: no known tokens in antropic, openia" in captured.err
    assert captured.out == ""  # nothing scanned or printed


def test_cli_fail_on_known_tokens_no_warning(repo, capsys):
    from prompt_shields.scan import FINDING_KINDS, KNOWN_VENDORS

    tokens = ",".join(["any", *FINDING_KINDS, *sorted(KNOWN_VENDORS)])
    main([str(repo), "--fail-on", tokens])
    assert "warning" not in capsys.readouterr().err


def test_cli_fail_on_no_findings(tmp_path):
    (tmp_path / "plain.py").write_text("print('hello')\n")
    assert main([str(tmp_path), "--fail-on", "any"]) == 0


# --------------------------------------------------------------------------
# Self-scan: this repository (#95 "Done when")
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def self_report():
    if not (REPO_ROOT / "packages" / "sdk" / "prompt_shields").is_dir():
        pytest.fail(f"repository root not found at {REPO_ROOT}")
    return scan_path(REPO_ROOT, exclude=["packages/sdk/tests/fixtures"])


def _under(report, prefix):
    return [f for f in report["findings"] if f["path"].startswith(prefix)]


def test_self_scan_finds_sdk_usage(self_report):
    sdk = _under(self_report, "packages/sdk/prompt_shields/")
    import_vendors = {f["vendor"] for f in sdk if f["kind"] == "import"}
    # The SDK wraps the provider SDKs it instruments.
    assert {"openai", "anthropic"} <= import_vendors
    assert any(f["kind"] == "client" and f["vendor"] == "openai" for f in sdk)


def test_self_scan_finds_gateway_usage(self_report):
    gw = _under(self_report, "gateway/src/")
    endpoints = {(f["vendor"], f["value"]) for f in gw if f["kind"] == "endpoint"}
    assert any(v == "openai" and "api.openai.com" in u for v, u in endpoints)
    assert any(v == "anthropic" for v, _ in endpoints)
    assert any(f["kind"] == "model" for f in gw)


def test_self_scan_finds_demo_usage(self_report):
    demo = _under(self_report, "demo/")
    assert any(f["kind"] == "import" and f["vendor"] == "prompt_shields" for f in demo)
    assert any(f["kind"] == "model" for f in demo)


def test_self_scan_events_and_hygiene(self_report):
    assert self_report["source"] == "code_scan"
    vendors = {e["vendor"] for e in self_report["events"]}
    assert {"openai", "anthropic", "google"} <= vendors
    paths = {f["path"] for f in self_report["findings"]}
    assert not any("node_modules/" in p or p.startswith(".git/") for p in paths)
    assert not any(p.startswith("packages/sdk/tests/fixtures/") for p in paths)


# --------------------------------------------------------------------------
# GitHub Action (action.yml at the repository root)
# --------------------------------------------------------------------------


def test_github_action_definition():
    yaml = pytest.importorskip("yaml")
    action = yaml.safe_load((REPO_ROOT / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert {"path", "exclude", "fail-on", "sarif-file", "upload-sarif"} <= set(
        action["inputs"]
    )
    assert action["inputs"]["upload-sarif"]["default"] == "false"
    steps = action["runs"]["steps"]
    upload = [s for s in steps if "upload-sarif" in s.get("uses", "")]
    assert len(upload) == 1 and upload[0]["if"] == "inputs.upload-sarif == 'true'"
    scan = next(s for s in steps if s.get("id") == "scan")
    # Inputs reach the shell through env, never interpolated into the script.
    assert "${{" not in scan["run"]
    assert "ps-scan" in scan["run"] and "--format sarif" in scan["run"]
    install = next(s for s in steps if "pip install" in s.get("run", ""))
    assert install["env"]["SDK_DIR"].endswith("/packages/sdk")


def test_discovery_source_includes_code_scan_and_otel():
    from typing import get_args

    from prompt_shields import DiscoverySource
    from prompt_shields.scan import SOURCE

    assert SOURCE in get_args(DiscoverySource)
    assert "otel" in get_args(DiscoverySource)
