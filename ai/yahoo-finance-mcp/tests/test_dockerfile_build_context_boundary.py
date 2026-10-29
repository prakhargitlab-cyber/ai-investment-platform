"""Regression guard: the Yahoo Finance MCP image must never COPY the whole
ai/research-engine directory into its build context/image again.

research-engine's own pyproject.toml restricts its installable package to
`app/` ([tool.setuptools.packages.find] include=["app*"]). Nothing outside
`app/` -- including root-level operational scripts such as JWT-signing
helpers (create_jwt.py, create_jwt_manual.py, save_jwt.py) or
activate_user.sql -- is required by pip install, by yahoo_mcp_server, or by
anything else in this image. This test fails the build if a future edit
reintroduces a broad `COPY ai/research-engine ...` (or `COPY ai ...`)
instruction, which would copy those files back into the image regardless of
.dockerignore.
"""
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"

FORBIDDEN_FRAGMENTS = (
    "copy ai/research-engine /opt/research-engine",
    "copy ai/research-engine .",
    "copy ai ",
    "copy . .",
    "copy . /opt/research-engine",
)

ALLOWED_RESEARCH_ENGINE_COPIES = (
    "copy ai/research-engine/pyproject.toml",
    "copy ai/research-engine/app",
)


def test_dockerfile_never_copies_whole_research_engine_directory():
    text = DOCKERFILE.read_text(encoding="utf-8")
    lowered_lines = [line.strip().lower() for line in text.splitlines()]

    for line in lowered_lines:
        for forbidden in FORBIDDEN_FRAGMENTS:
            assert forbidden not in line, (
                f"Dockerfile line '{line}' broadly copies ai/research-engine "
                "or the whole build context -- this would reintroduce "
                "JWT-signing helpers / activate_user.sql into the image. "
                "Copy only pyproject.toml and app/ explicitly instead."
            )


def test_dockerfile_only_copies_the_allowed_research_engine_paths():
    text = DOCKERFILE.read_text(encoding="utf-8")
    lowered_lines = [line.strip().lower() for line in text.splitlines()]
    research_engine_copy_lines = [
        line for line in lowered_lines
        if line.startswith("copy") and "research-engine" in line
    ]
    assert research_engine_copy_lines, "Expected at least one COPY line referencing research-engine"
    for line in research_engine_copy_lines:
        assert any(line.startswith(allowed) for allowed in ALLOWED_RESEARCH_ENGINE_COPIES), (
            f"Unexpected research-engine COPY line: '{line}'. Only pyproject.toml "
            "and app/ may be copied from ai/research-engine."
        )
