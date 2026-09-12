#!/usr/bin/env python3
"""Conservative repository credential scan with value-safe output.

The scanner never prints a matched value. Findings still require human review;
it is intended as a dependency-free pre-commit/CI guard, not a replacement for
a dedicated entropy-aware scanner.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {
    ".git", ".codex", ".idea", ".terraform", ".next", "node_modules",
    "target", "__pycache__", "running-broker-jar-inspect", ".tmp",
}
TEXT_SUFFIXES = {
    ".env", ".ini", ".java", ".json", ".md", ".properties", ".ps1",
    ".py", ".tf", ".toml", ".ts", ".tsx", ".xml", ".yaml", ".yml",
}
SENSITIVE_KEY = (
    r"password|passwd|api[_-]?key|apikey|secret|token|authorization|"
    r"connection[_-]?string|connectionstring|shared[_-]?access[_-]?key"
)
CONFIG_ASSIGNMENT = re.compile(
    rf"(?i)^\s*[\"']?(?P<key>[a-z0-9_.-]*(?:{SENSITIVE_KEY})[a-z0-9_.-]*)[\"']?"
    rf"\s*[:=]\s*[\"']?(?P<value>[^\s#,\"']*)"
)
CODE_LITERAL = re.compile(
    rf"(?i)\b(?P<key>[a-z0-9_]*(?:{SENSITIVE_KEY})[a-z0-9_]*)"
    rf"\s*=\s*[\"'](?P<value>[^\"']+)[\"']"
)
BEARER_LITERAL = re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})")
CONFIG_SUFFIXES = {".env", ".ini", ".json", ".properties", ".tf", ".toml", ".yaml", ".yml"}
HIGH_CONFIDENCE = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("AZURE_STORAGE_KEY", re.compile(r"(?i)(?:AccountKey|SharedAccessKey)=")),
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("GOOGLE_API_KEY", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("OPENAI_STYLE_KEY", re.compile(r"sk-[A-Za-z0-9]{20,}")),
]
PLACEHOLDER_MARKERS = (
    "${", "{{", "replace", "placeholder", "example", "invalid", "change-me",
    "changeme", "dummy", "redacted", "your-", "dev-", "test-", "<",
)


def classification(path: Path, key: str, value: str) -> str:
    normalized = value.lower()
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if "test" in path.parts or "tests" in path.parts or "fixture" in path.parts:
        return "TEST_FIXTURE"
    if (
        normalized in {"true", "false", "null", "[]", "{}", "{", "|", ">"}
        or normalized_key.endswith(("ttl", "ttlseconds", "tokenfile"))
        or normalized_key in {
            "enabledbacauthorization", "enablerbacauthorization",
            "secretrotationenabled", "imagepullsecrets", "secrets",
            "secretobjects", "automountserviceaccounttoken",
        }
    ):
        return "FALSE_POSITIVE"
    if path.name in {"package.json", "package-lock.json"} and normalized_key.endswith("tokens"):
        return "FALSE_POSITIVE"
    if (
        normalized_key.endswith(("secretname", "secretkey", "secretkeyname"))
        or normalized_key.endswith("secretkeyenvironmentvariable")
        or normalized_key in {"existingsecret", "secretproviderclassname"}
        or re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*(?::[^}]*)?}", value)
    ):
        return "SECRET_REFERENCE"
    if not value or any(marker in normalized for marker in PLACEHOLDER_MARKERS):
        return "PLACEHOLDER"
    if path.suffix.lower() == ".md":
        return "DOCUMENTATION_EXAMPLE"
    return "REVIEW_REQUIRED"


def files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(
            part in SKIP_PARTS or part == "site-packages" or part.startswith(".venv")
            for part in path.parts
        ):
            continue
        if path.name.startswith(".env") or path.suffix.lower() in TEXT_SUFFIXES:
            if path.stat().st_size <= 2_000_000:
                yield path


def main() -> int:
    findings: set[tuple[str, int, str, str]] = set()
    for path in files():
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), 1):
            for name, pattern in HIGH_CONFIDENCE:
                if pattern.search(line):
                    findings.add((relative, line_number, name, "REVIEW_REQUIRED"))
            for match in BEARER_LITERAL.finditer(line):
                findings.add(
                    (
                        relative,
                        line_number,
                        "BEARER_TOKEN",
                        classification(path.relative_to(ROOT), "authorization", match.group("value")),
                    )
                )
            matcher = CONFIG_ASSIGNMENT if (path.name.startswith(".env") or path.suffix.lower() in CONFIG_SUFFIXES) else CODE_LITERAL
            for match in matcher.finditer(line):
                key = match.group("key").upper().replace("-", "_")
                value = match.group("value")
                findings.add((relative, line_number, key, classification(path.relative_to(ROOT), key, value)))

    categories = Counter(finding[3] for finding in findings)
    print(f"secret_scan_findings={len(findings)}")
    for category in (
        "REVIEW_REQUIRED", "PLACEHOLDER", "SECRET_REFERENCE", "TEST_FIXTURE",
        "DOCUMENTATION_EXAMPLE", "FALSE_POSITIVE",
    ):
        print(f"secret_scan_{category.lower()}={categories[category]}")
    for relative, line_number, key, category in sorted(findings):
        print(f"{relative}:{line_number} key={key} classification={category} value=<redacted>")
    return 1 if categories["REVIEW_REQUIRED"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
