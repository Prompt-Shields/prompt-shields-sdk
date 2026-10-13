"""ps-scan: statically scan a repository for AI usage.

Finds LLM SDK imports and client constructors, model name strings, provider
base URLs and API-key environment variable *names* in Python (via ``ast``),
TypeScript/JavaScript, Java and Go (via regex). Standard library only, no
network, and source code never leaves the machine: findings carry only the
matched identifier (module, model, URL host/path or env var name), a file
path and a line number.

Safety rules:
    * ``.env`` / ``.env.*`` files are never opened, so secret values cannot
      leak into the output. Env vars are reported by name only.
    * URLs are reduced to scheme, host and path; query strings and userinfo
      are dropped.
    * ``.git``, ``node_modules``, virtualenvs, build output and binary files
      are skipped; files over ``max_file_bytes`` are skipped.

Usage::

    ps-scan [PATH] [--format summary|json|events|sarif] [--output FILE]

``--format events`` prints an ``{"events": [...]}`` body that the collector's
``POST /ingest/events`` accepts, with ``source="code_scan"``. ps-scan itself
never uploads anything.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = 1
SOURCE = "code_scan"
DEFAULT_MAX_FILE_BYTES = 1_000_000
MAX_VALUE_LEN = 200

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "site-packages",
        "dist",
        "build",
        ".next",
        ".turbo",
        "target",
        "vendor",
        ".idea",
        ".vscode",
    }
)

LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    # Config files: only model / URL / env var patterns apply.
    ".json": "config",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
}

# Module prefix -> vendor. Longest matching prefix wins.
MODULE_VENDORS: dict[str, str] = {
    # Python
    "openai": "openai",
    "anthropic": "anthropic",
    "google.genai": "google",
    "google.generativeai": "google",
    "vertexai": "google",
    "mistralai": "mistral",
    "cohere": "cohere",
    "langchain": "langchain",
    "langchain_core": "langchain",
    "langchain_community": "langchain",
    "langchain_openai": "openai",
    "langchain_anthropic": "anthropic",
    "langchain_google_genai": "google",
    "langchain_aws": "bedrock",
    "langchain_mistralai": "mistral",
    "langchain_cohere": "cohere",
    "llama_index": "llama_index",
    "litellm": "litellm",
    "prompt_shields": "prompt_shields",
    # JavaScript / TypeScript
    "@anthropic-ai/sdk": "anthropic",
    "@anthropic-ai/bedrock-sdk": "anthropic",
    "@google/genai": "google",
    "@google/generative-ai": "google",
    "@google-cloud/vertexai": "google",
    "@aws-sdk/client-bedrock-runtime": "bedrock",
    "@mistralai/mistralai": "mistral",
    "cohere-ai": "cohere",
    "@langchain": "langchain",
    "llamaindex": "llama_index",
    "portkey-ai": "portkey",
    # Java / Kotlin
    "com.openai": "openai",
    "com.anthropic": "anthropic",
    "com.google.genai": "google",
    "com.google.cloud.vertexai": "google",
    "software.amazon.awssdk.services.bedrockruntime": "bedrock",
    "dev.langchain4j": "langchain",
    "com.theokanning.openai": "openai",
    # Go
    "github.com/openai/openai-go": "openai",
    "github.com/sashabaranov/go-openai": "openai",
    "github.com/anthropics/anthropic-sdk-go": "anthropic",
    "google.golang.org/genai": "google",
    "github.com/google/generative-ai-go": "google",
    "github.com/aws/aws-sdk-go-v2/service/bedrockruntime": "bedrock",
    "github.com/tmc/langchaingo": "langchain",
    "github.com/cohere-ai/cohere-go": "cohere",
}

# Client constructors / entry points, keyed by the called name's last part.
CLIENT_VENDORS: dict[str, str] = {
    "OpenAI": "openai",
    "AsyncOpenAI": "openai",
    "AzureOpenAI": "openai",
    "AsyncAzureOpenAI": "openai",
    "ChatOpenAI": "openai",
    "OpenAIOkHttpClient": "openai",
    "Anthropic": "anthropic",
    "AsyncAnthropic": "anthropic",
    "AnthropicBedrock": "anthropic",
    "AnthropicVertex": "anthropic",
    "ChatAnthropic": "anthropic",
    "AnthropicOkHttpClient": "anthropic",
    "GenerativeModel": "google",
    "ChatGoogleGenerativeAI": "google",
    "GoogleGenAI": "google",
    "Mistral": "mistral",
    "MistralClient": "mistral",
    "ChatMistralAI": "mistral",
    "CohereClient": "cohere",
    "CohereClientV2": "cohere",
    "ChatCohere": "cohere",
    "ChatBedrock": "bedrock",
    "ChatBedrockConverse": "bedrock",
    "BedrockRuntimeClient": "bedrock",
    "ShieldsClient": "prompt_shields",
    "ShieldsOpenAI": "prompt_shields",
    "ShieldsAnthropic": "prompt_shields",
    "AsyncShieldsClient": "prompt_shields",
    "AsyncShieldsOpenAI": "prompt_shields",
    "AsyncShieldsAnthropic": "prompt_shields",
}
# Qualified calls whose last part is too generic on its own.
QUALIFIED_CLIENTS: dict[str, str] = {
    "genai.Client": "google",
    "cohere.Client": "cohere",
    "cohere.ClientV2": "cohere",
    "litellm.completion": "litellm",
    "litellm.acompletion": "litellm",
    "litellm.embedding": "litellm",
}
BEDROCK_SERVICES = frozenset({"bedrock-runtime", "bedrock-agent-runtime", "bedrock"})

# Model names. Matched against whole string literals, so prose rarely hits.
_MODEL_PATTERNS: list[tuple[str, str]] = [
    (
        "openai",
        r"(?:gpt-[0-9][\w.:-]*|chatgpt-[\w.-]+|o[1-9](?:-(?:mini|pro|preview)[\w.-]*)?|text-embedding-[\w-]+|dall-e-[23]|whisper-1|tts-1(?:-hd)?)",
    ),
    ("anthropic", r"claude-[\w.:@-]+"),
    (
        "bedrock",
        r"(?:[a-z]{2}\.)?(?:anthropic|meta|amazon|mistral|cohere|ai21)\.[\w.:-]+-v\d[\w.:-]*",
    ),
    ("google", r"(?:gemini-[\w.-]+|text-bison[\w@-]*|chat-bison[\w@-]*)"),
    (
        "mistral",
        r"(?:mistral-(?:\d|large|medium|small|tiny|nemo|embed|moderation|saba|ocr)[\w.-]*|mixtral-[\w.-]+|codestral-[\w.-]+|pixtral-[\w.-]+|open-mistral-[\w.-]+)",
    ),
    (
        "cohere",
        r"(?:command-r[\w.-]*|command-a[\w.-]*|embed-(?:english|multilingual)-[\w.-]+|rerank-(?:v\d|english|multilingual|\d)[\w.-]*)",
    ),
    ("meta", r"(?:meta-llama/[\w.-]+|llama-?[234][\w.:-]*)"),
]
_MODEL_RES = [(v, re.compile(p, re.IGNORECASE)) for v, p in _MODEL_PATTERNS]
# Lookahead so matches can overlap: in `"", "gpt-4o"` the `", "` pair must
# not swallow the opening quote of the model string.
_QUOTED_RE = re.compile(r"""(?=(["'`])([^"'`\n\\]{2,120})\1)""")

# Provider base URLs (host match). Path is kept, query/userinfo are not.
_URL_HOSTS: list[tuple[str, str]] = [
    ("openai", r"api\.openai\.com"),
    ("openai", r"[\w-]+\.openai\.azure\.com"),
    ("anthropic", r"api\.anthropic\.com"),
    ("google", r"generativelanguage\.googleapis\.com"),
    ("google", r"(?:[\w-]+-)?aiplatform\.googleapis\.com"),
    (
        "bedrock",
        r"bedrock(?:-runtime|-agent-runtime)?(?:-fips)?\.[\w-]+\.amazonaws\.com",
    ),
    ("mistral", r"api\.mistral\.ai"),
    ("cohere", r"api\.cohere\.(?:ai|com)"),
    ("groq", r"api\.groq\.com"),
    ("together", r"api\.together\.(?:xyz|ai)"),
    ("openrouter", r"openrouter\.ai"),
    ("deepseek", r"api\.deepseek\.com"),
    ("portkey", r"api\.portkey\.ai"),
]
_URL_RE = re.compile(
    r"https?://(?:[^\s/@\"'`]+@)?(?P<host>"
    + "|".join(f"(?:{h})" for _, h in _URL_HOSTS)
    + r")(?P<path>/[\w./-]*)?",
    re.IGNORECASE,
)
_HOST_RES = [(v, re.compile(h + r"\Z", re.IGNORECASE)) for v, h in _URL_HOSTS]

# API-key env var names. Only the name is ever captured.
ENV_VAR_VENDORS: dict[str, str] = {
    "OPENAI_API_KEY": "openai",
    "OPENAI_BASE_URL": "openai",
    "OPENAI_ORG_ID": "openai",
    "AZURE_OPENAI_API_KEY": "openai",
    "AZURE_OPENAI_ENDPOINT": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "ANTHROPIC_BASE_URL": "anthropic",
    "GOOGLE_API_KEY": "google",
    "GEMINI_API_KEY": "google",
    "GOOGLE_GENAI_USE_VERTEXAI": "google",
    "MISTRAL_API_KEY": "mistral",
    "COHERE_API_KEY": "cohere",
    "CO_API_KEY": "cohere",
    "GROQ_API_KEY": "groq",
    "TOGETHER_API_KEY": "together",
    "OPENROUTER_API_KEY": "openrouter",
    "DEEPSEEK_API_KEY": "deepseek",
    "HUGGINGFACE_API_KEY": "huggingface",
    "HF_TOKEN": "huggingface",
    "LANGCHAIN_API_KEY": "langchain",
    "LANGSMITH_API_KEY": "langchain",
    "LLAMA_CLOUD_API_KEY": "llama_index",
    "PORTKEY_API_KEY": "portkey",
    "AWS_BEARER_TOKEN_BEDROCK": "bedrock",
    "PS_API_KEY": "prompt_shields",
}
_ENV_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(sorted(ENV_VAR_VENDORS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])"
)

# Regex import extraction for non-Python languages.
_JS_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']([^"'\n]+)["']"""
)
_PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.MULTILINE)
_GO_IMPORT_RE = re.compile(
    r"""^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"\n]+)"\s*$""", re.MULTILINE
)
_NEW_CLIENT_RE = re.compile(r"\bnew\s+([A-Z]\w*)\s*[(<]")
_JAVA_BUILDER_RE = re.compile(r"\b([A-Z]\w*)\s*\.\s*(?:builder|fromEnv)\s*\(")
_BEDROCK_STR_RE = re.compile(r"""["'](bedrock(?:-agent)?-runtime)["']""")


@dataclass(frozen=True)
class Finding:
    """One piece of evidence of AI usage. Never contains source text."""

    kind: str  # import | client | model | endpoint | env_var
    vendor: str
    value: str
    path: str
    line: int
    language: str


def module_vendor(module: str) -> str | None:
    """Map an import path to a vendor by longest dotted / slashed prefix."""
    best: tuple[int, str] | None = None
    for prefix, vendor in MODULE_VENDORS.items():
        matches = module == prefix or module.startswith((prefix + ".", prefix + "/"))
        if matches and (best is None or len(prefix) > best[0]):
            best = (len(prefix), vendor)
    return best[1] if best else None


def model_vendor(text: str) -> str | None:
    for vendor, rx in _MODEL_RES:
        if rx.fullmatch(text):
            return vendor
    return None


def _host_vendor(host: str) -> str:
    for vendor, rx in _HOST_RES:
        if rx.match(host):
            return vendor
    return "unknown"


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _clip(value: str) -> str:
    return value[:MAX_VALUE_LEN]


# --------------------------------------------------------------------------
# Language scanners
# --------------------------------------------------------------------------


def _scan_text_patterns(
    text: str, rel: str, lang: str, *, models: bool = True
) -> Iterator[Finding]:
    """Models in quoted strings, provider URLs and env var names."""
    if models:
        for m in _QUOTED_RE.finditer(text):
            vendor = model_vendor(m.group(2))
            if vendor:
                yield Finding(
                    "model",
                    vendor,
                    _clip(m.group(2)),
                    rel,
                    _line_of(text, m.start()),
                    lang,
                )
    for m in _URL_RE.finditer(text):
        host = m.group("host").lower()
        path = (m.group("path") or "").rstrip("./")
        yield Finding(
            "endpoint",
            _host_vendor(host),
            _clip(f"https://{host}{path}"),
            rel,
            _line_of(text, m.start()),
            lang,
        )
    for m in _ENV_RE.finditer(text):
        yield Finding(
            "env_var",
            ENV_VAR_VENDORS[m.group(1)],
            m.group(1),
            rel,
            _line_of(text, m.start()),
            lang,
        )


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _scan_python(text: str, rel: str) -> Iterator[Finding]:
    lang = "python"
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        # Unparseable: fall back to the generic regex scanners.
        yield from _scan_regex_imports(text, rel, lang, _PY_IMPORT_RE)
        yield from _scan_text_patterns(text, rel, lang)
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                vendor = module_vendor(alias.name)
                if vendor:
                    yield Finding("import", vendor, alias.name, rel, node.lineno, lang)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            vendor = module_vendor(node.module)
            if vendor:
                yield Finding("import", vendor, node.module, rel, node.lineno, lang)
            elif node.module == "google":
                for alias in node.names:
                    if alias.name in ("genai", "generativeai"):
                        yield Finding(
                            "import",
                            "google",
                            f"google.{alias.name}",
                            rel,
                            node.lineno,
                            lang,
                        )
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if not name:
                continue
            last = name.rsplit(".", 1)[-1]
            tail2 = ".".join(name.rsplit(".", 2)[-2:])
            if tail2 in QUALIFIED_CLIENTS:
                yield Finding(
                    "client", QUALIFIED_CLIENTS[tail2], name, rel, node.lineno, lang
                )
            elif last in CLIENT_VENDORS:
                yield Finding(
                    "client", CLIENT_VENDORS[last], name, rel, node.lineno, lang
                )
            elif last == "client" and _first_str_arg(node) in BEDROCK_SERVICES:
                yield Finding(
                    "client",
                    "bedrock",
                    f"{name}({_first_str_arg(node)!r})",
                    rel,
                    node.lineno,
                    lang,
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if 2 <= len(value) <= 120:
                vendor = model_vendor(value)
                if vendor:
                    yield Finding("model", vendor, value, rel, node.lineno, lang)

    yield from _scan_text_patterns(text, rel, lang, models=False)


def _first_str_arg(call: ast.Call) -> str | None:
    if (
        call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ):
        return call.args[0].value
    for kw in call.keywords:
        if (
            kw.arg == "service_name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _scan_regex_imports(
    text: str, rel: str, lang: str, rx: re.Pattern[str]
) -> Iterator[Finding]:
    for m in rx.finditer(text):
        module = m.group(1)
        vendor = module_vendor(module)
        if vendor:
            yield Finding(
                "import", vendor, _clip(module), rel, _line_of(text, m.start(1)), lang
            )


def _scan_clients_regex(text: str, rel: str, lang: str) -> Iterator[Finding]:
    for rx in (_NEW_CLIENT_RE, _JAVA_BUILDER_RE):
        for m in rx.finditer(text):
            vendor = CLIENT_VENDORS.get(m.group(1))
            if vendor:
                yield Finding(
                    "client", vendor, m.group(1), rel, _line_of(text, m.start(1)), lang
                )
    for m in _BEDROCK_STR_RE.finditer(text):
        yield Finding(
            "client", "bedrock", m.group(1), rel, _line_of(text, m.start()), lang
        )


def _scan_go(text: str, rel: str) -> Iterator[Finding]:
    yield from _scan_regex_imports(text, rel, "go", _GO_IMPORT_RE)
    yield from _scan_text_patterns(text, rel, "go")


def scan_text(text: str, rel: str, lang: str) -> list[Finding]:
    """Scan one file's text. ``rel`` is the path reported in findings."""
    if lang == "python":
        found = _scan_python(text, rel)
    elif lang in ("javascript", "typescript"):
        found = _chain(
            _scan_regex_imports(text, rel, lang, _JS_IMPORT_RE),
            _scan_clients_regex(text, rel, lang),
            _scan_text_patterns(text, rel, lang),
        )
    elif lang in ("java", "kotlin"):
        found = _chain(
            _scan_regex_imports(text, rel, lang, _JAVA_IMPORT_RE),
            _scan_clients_regex(text, rel, lang),
            _scan_text_patterns(text, rel, lang),
        )
    elif lang == "go":
        found = _scan_go(text, rel)
    else:
        found = _scan_text_patterns(text, rel, lang)
    # Deduplicate (same kind/value on the same line) while keeping order.
    return list(dict.fromkeys(found))


def _chain(*its: Iterable[Finding]) -> Iterator[Finding]:
    for it in its:
        yield from it


# --------------------------------------------------------------------------
# Repository walk
# --------------------------------------------------------------------------


def _is_secret_file(name: str) -> bool:
    lower = name.lower()
    return lower == ".env" or lower.startswith(".env.") or lower.endswith(".env")


def iter_files(root: Path) -> Iterator[Path]:
    """Yield candidate source files, pruning skipped dirs and virtualenvs."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not d.endswith(".egg-info")
            and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg"))
        )
        for name in sorted(filenames):
            if _is_secret_file(name):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() in LANG_BY_EXT and not path.is_symlink():
                yield path


def _read_text(path: Path, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_path(
    root: str | os.PathLike[str], *, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
) -> dict:
    """Scan a directory (or single file) and return the JSON report dict."""
    root_path = Path(root).resolve()
    findings: list[Finding] = []
    scanned = skipped = 0
    files = [root_path] if root_path.is_file() else iter_files(root_path)
    base = root_path.parent if root_path.is_file() else root_path
    for path in files:
        if _is_secret_file(path.name):
            skipped += 1
            continue
        text = _read_text(path, max_file_bytes)
        if text is None:
            skipped += 1
            continue
        scanned += 1
        rel = path.relative_to(base).as_posix()
        lang = LANG_BY_EXT.get(path.suffix.lower(), "config")
        findings.extend(scan_text(text, rel, lang))

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "ps-scan",
        "source": SOURCE,
        "root": root_path.name,
        "files_scanned": scanned,
        "files_skipped": skipped,
        "findings": [asdict(f) for f in findings],
        "summary": summarize(findings),
        "events": to_events(findings, calling_service=root_path.name),
    }


def summarize(findings: Iterable[Finding]) -> dict:
    by_vendor: dict[str, dict] = {}
    for f in findings:
        entry = by_vendor.setdefault(
            f.vendor, {"files": set(), "models": set(), "kinds": {}}
        )
        entry["files"].add(f.path)
        entry["kinds"][f.kind] = entry["kinds"].get(f.kind, 0) + 1
        if f.kind == "model":
            entry["models"].add(f.value)
    return {
        vendor: {
            "files": len(e["files"]),
            "models": sorted(e["models"]),
            "counts": dict(sorted(e["kinds"].items())),
        }
        for vendor, e in sorted(by_vendor.items())
    }


# Vendors that are frameworks or proxies rather than model providers; they
# are reported in findings but do not become collector sightings on their own.
_NON_PROVIDER_VENDORS = frozenset(
    {"langchain", "llama_index", "litellm", "prompt_shields", "unknown"}
)


def to_events(
    findings: Iterable[Finding], *, calling_service: str | None = None
) -> list[dict]:
    """Collapse findings into collector ``IngestEvent`` dicts (``source="code_scan"``).

    One event per (vendor, model) seen, plus one model-less event for a
    provider whose usage was found without any model string.
    """
    pairs: dict[tuple[str, str | None], None] = {}
    provider_vendors: dict[str, None] = {}
    for f in findings:
        if f.vendor in _NON_PROVIDER_VENDORS:
            continue
        provider_vendors[f.vendor] = None
        if f.kind == "model":
            pairs[(f.vendor, f.value)] = None
    with_model = {v for v, _ in pairs}
    for vendor in provider_vendors:
        if vendor not in with_model:
            pairs[(vendor, None)] = None
    return [
        {
            "vendor": vendor,
            "model": model,
            "source": SOURCE,
            "calling_service": calling_service,
        }
        for vendor, model in sorted(pairs, key=lambda p: (p[0], p[1] or ""))
    ]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def format_summary(report: dict) -> str:
    lines = [
        (
            f"ps-scan: {report['root']} - {report['files_scanned']} files scanned, "
            f"{report['files_skipped']} skipped, {len(report['findings'])} findings"
        )
    ]
    if not report["summary"]:
        lines.append("No AI usage found.")
    for vendor, s in report["summary"].items():
        counts = ", ".join(f"{k}={v}" for k, v in s["counts"].items())
        lines.append(f"  {vendor:<15} {s['files']:>4} file(s)  {counts}")
        if s["models"]:
            lines.append(f"  {'':<15} models: {', '.join(s['models'])}")
    return "\n".join(lines)


_SARIF_RULES = {
    "import": "AI SDK import",
    "client": "AI client construction",
    "model": "AI model name",
    "endpoint": "AI provider endpoint",
    "env_var": "AI API-key environment variable",
}


def to_sarif(report: dict) -> dict:
    results = [
        {
            "ruleId": f"ps-scan/{f['kind']}",
            "level": "note",
            "message": {
                "text": f"{_SARIF_RULES[f['kind']]} ({f['vendor']}): {f['value']}"
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["path"]},
                        "region": {"startLine": f["line"]},
                    }
                }
            ],
        }
        for f in report["findings"]
    ]
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ps-scan",
                        "rules": [
                            {"id": f"ps-scan/{k}", "shortDescription": {"text": v}}
                            for k, v in _SARIF_RULES.items()
                        ],
                    }
                },
                "results": results,
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ps-scan", description="Statically scan a repository for AI usage."
    )
    parser.add_argument(
        "path", nargs="?", default=".", help="directory or file to scan (default: .)"
    )
    parser.add_argument(
        "--format",
        choices=("summary", "json", "events", "sarif"),
        default="summary",
        help="summary (human), json (full report), events (collector ingest body), sarif",
    )
    parser.add_argument("--output", "-o", help="write to this file instead of stdout")
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help="skip files larger than this",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.path):
        parser.error(f"path not found: {args.path}")

    report = scan_path(args.path, max_file_bytes=args.max_file_bytes)
    if args.format == "summary":
        out = format_summary(report)
    elif args.format == "json":
        out = json.dumps(report, indent=2)
    elif args.format == "events":
        out = json.dumps({"events": report["events"]}, indent=2)
    else:
        out = json.dumps(to_sarif(report), indent=2)

    if args.output:
        Path(args.output).write_text(out + "\n", encoding="utf-8")
        if args.format != "summary":
            print(format_summary(report), file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
