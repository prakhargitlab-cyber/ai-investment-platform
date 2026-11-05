"""Offline format tests. Synthetic archives only, no database access."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("backup_format", HERE / "helm_k3d_backup_format.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_postgres_custom_header(tmp_path):
    f = tmp_path / "synthetic.dump"
    f.write_bytes(b"PGDMP" + b"synthetic" * 20)
    assert mod.classify(f) == "POSTGRES_CUSTOM_ARCHIVE"


def test_gzip_header(tmp_path):
    import gzip
    f = tmp_path / "synthetic.dump.gz"
    with gzip.open(f, "wb") as stream:
        stream.write(b"PGDMP" + b"synthetic" * 20)
    assert mod.classify(f) == "GZIP"


def test_sql_header(tmp_path):
    f = tmp_path / "synthetic.sql"
    f.write_bytes(b"-- PostgreSQL database dump\nSELECT 1;\n")
    assert mod.classify(f) == "PLAINTEXT_SQL_POSSIBLE"


def test_unknown_format_is_not_assumed_restorable(tmp_path):
    f = tmp_path / "opaque.blob"
    f.write_bytes(b"no known backup signature")
    assert mod.classify(f) == "UNKNOWN"
