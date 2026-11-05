"""Static regressions for DEV JWT upgrade preservation.

These assertions prevent accidentally reintroducing stringData or inlining
a Helm value. The server-connected Helm acceptance remains authoritative.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "infrastructure" / "helm" / "ai-investment-platform" / "templates"


def test_jwt_secret_uses_credential_preserving_lookup():
    source = (TEMPLATES / "dev-dependencies.yaml").read_text(encoding="utf-8")
    start = source.index("{{- if .Values.auth.createDevSecret }}")
    finish = source.index("{{- end }}", start)
    jwt = source[start:finish]
    assert "kind: Secret" in jwt
    assert "type: Opaque" in jwt
    assert "data:" in jwt
    assert "stringData:" not in jwt
    assert 'include "aip.devCredential"' in jwt
    assert '.Values.auth.devJwtSecret | quote' not in jwt
    assert '"value" .Values.auth.devJwtSecret' in jwt
    assert '"name" .Values.auth.jwtSecretName' in jwt
    assert '"key" .Values.auth.jwtSecretKey' in jwt


def test_live_lookup_fails_closed_on_upgrade_and_validates_encoding():
    source = (TEMPLATES / "_helpers.tpl").read_text(encoding="utf-8")
    helper = source.split('{{- define "aip.devCredential" -}}', 1)[1]
    helper = helper.split('{{- define "aip.labels" -}}', 1)[0]
    assert 'lookup "v1" "Secret"' in helper
    assert "DEV_CREDENTIAL_MISSING_ON_UPGRADE" in helper
    assert "b64dec" in helper
    assert "b64enc" in helper
