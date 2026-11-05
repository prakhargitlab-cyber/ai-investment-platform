#!/usr/bin/env python3
"""Inspect newest DEV DB backup without opening a database or creating resources.

This checks file shape/integrity only; it cannot prove a successful restore.
Never prints backup content or file paths.
"""
import gzip
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / ".tmp" / "main-db-backups"


class Blocked(Exception):
    pass


def classify(path):
    with path.open("rb") as stream:
        head = stream.read(512)
    if head.startswith(b"PGDMP"):
        return "POSTGRES_CUSTOM_ARCHIVE"
    if head.startswith(b"\x1f\x8b"):
        return "GZIP"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "TAR_ARCHIVE"
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith((b"-- PostgreSQL database dump", b"SET ", b"CREATE ",
                            b"\\connect", b"BEGIN;")):
        return "PLAINTEXT_SQL_POSSIBLE"
    return "UNKNOWN"


def inspect():
    if not BACKUP_DIR.is_dir():
        raise Blocked("BACKUP_DIR_MISSING")
    paths = [p for p in BACKUP_DIR.iterdir() if p.is_file() and p.stat().st_size > 0]
    if not paths:
        raise Blocked("NO_NONEMPTY_BACKUP")
    newest = max(paths, key=lambda p: p.stat().st_mtime)
    shape = classify(newest)
    print("BACKUP_FILES_NONEMPTY:", len(paths))
    print("NEWEST_BACKUP_AGE_HOURS:", round((time.time()-newest.stat().st_mtime)/3600, 1))
    print("NEWEST_BACKUP_FORMAT:", shape)
    if shape == "GZIP":
        with gzip.open(newest, "rb") as stream:
            uncompressed_head = stream.read(512)
            # Stream entire payload to validate all gzip blocks and CRC.
            while stream.read(1024 * 1024):
                pass
        print("GZIP_STREAM_INTEGRITY: passed")
        print("GZIP_PAYLOAD_FORMAT:",
              "POSTGRES_CUSTOM_ARCHIVE" if uncompressed_head.startswith(b"PGDMP")
              else "OTHER_OR_PLAINTEXT")
    elif shape == "POSTGRES_CUSTOM_ARCHIVE":
        print("CUSTOM_ARCHIVE_HEADER: present")
        print("ARCHIVE_TOC_AND_PAYLOAD_INTEGRITY: unverified")
    elif shape == "TAR_ARCHIVE":
        print("TAR_HEADER: present")
        print("TAR_MEMBER_INTEGRITY: unverified")
    else:
        print("BACKUP_FORMAT_INTEGRITY: unverified")
    if shape == "UNKNOWN":
        raise Blocked("UNRECOGNIZED_BACKUP_FORMAT")
    print("BACKUP_FORMAT_INSPECTION_COMPLETE")
    print("RESTORE_TEST_STILL_REQUIRED")
    print("LIVE_DATABASE_UNTOUCHED")


if __name__ == "__main__":
    try:
        inspect()
    except (Blocked, OSError, EOFError, ValueError):
        print("BACKUP_FORMAT_INSPECTION_BLOCKED")
        sys.exit(2)
