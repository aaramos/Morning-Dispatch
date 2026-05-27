from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from backend.agents.librarian.text_utils import keyword_set
from backend.app.db import database

SUPPORTED_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
}
TEXT_FILE_TYPE = "text"
CHUNK_CHAR_LIMIT = 1800
MIN_CHUNK_CHARS = 80


@dataclass(frozen=True)
class CollectionMatch:
    collection_name: str
    file_path: str
    relative_path: str
    chunk_index: int
    text: str
    score: float
    matched_terms: tuple[str, ...]


def setup_collections_root(root: Path) -> dict[str, Any]:
    root.expanduser().mkdir(parents=True, exist_ok=True)
    return collections_status(root)


def collections_status(root: Path) -> dict[str, Any]:
    clean_root = root.expanduser()
    root_exists = clean_root.exists() and clean_root.is_dir()
    collection_names = _collection_names(clean_root) if root_exists else []
    summary = database.collection_index_summary(root_path=str(clean_root))
    return {
        **summary,
        "root_path": str(clean_root),
        "root_exists": root_exists,
        "collection_count": len(collection_names),
        "collections": collection_names,
    }


def sync_collections(root: Path, *, max_file_bytes: int) -> dict[str, Any]:
    clean_root = root.expanduser()
    if not clean_root.exists():
        return {
            **collections_status(clean_root),
            "created": False,
            "synced_file_count": 0,
            "ignored_root_file_count": 0,
        }
    if not clean_root.is_dir():
        return {
            **collections_status(clean_root),
            "synced_file_count": 0,
            "ignored_root_file_count": 0,
            "error": "Collections root exists but is not a folder.",
        }

    seen_paths: set[str] = set()
    synced = 0
    ignored_root_files = 0
    for child in sorted(clean_root.iterdir(), key=lambda item: item.name.lower()):
        if child.is_file():
            ignored_root_files += 1
            continue
        if not child.is_dir() or child.name.startswith("."):
            continue
        collection_name = child.name
        for path in sorted((item for item in child.rglob("*") if item.is_file()), key=lambda item: str(item).lower()):
            seen_paths.add(str(path))
            _sync_file(
                root=clean_root,
                collection_name=collection_name,
                path=path,
                max_file_bytes=max_file_bytes,
            )
            synced += 1

    deleted = database.delete_collection_files_not_seen(root_path=str(clean_root), seen_paths=seen_paths)
    return {
        **collections_status(clean_root),
        "synced_file_count": synced,
        "ignored_root_file_count": ignored_root_files,
        "deleted_file_count": deleted,
    }


def search_collections(
    query: str,
    *,
    collection_names: list[str] | None = None,
    limit: int = 12,
) -> list[CollectionMatch]:
    terms = keyword_set(query)
    if not terms:
        return []
    rows = database.list_collection_chunks(collection_names=collection_names, limit=2000)
    matches: list[CollectionMatch] = []
    for row in rows:
        text = str(row.get("text") or "")
        haystack = " ".join(
            str(value or "")
            for value in (
                row.get("collection_name"),
                row.get("relative_path"),
                text,
            )
        ).lower()
        matched_terms = tuple(sorted(term for term in terms if term in haystack))
        if not matched_terms:
            continue
        density = len(matched_terms) / max(len(terms), 1)
        text_bonus = min(len(text) / 3000, 0.18)
        score = round(min(0.98, 0.52 + density * 0.38 + text_bonus), 3)
        matches.append(
            CollectionMatch(
                collection_name=str(row.get("collection_name") or ""),
                file_path=str(row.get("file_path") or ""),
                relative_path=str(row.get("relative_path") or ""),
                chunk_index=int(row.get("chunk_index") or 0),
                text=text,
                score=score,
                matched_terms=matched_terms,
            )
        )
    return sorted(matches, key=lambda item: item.score, reverse=True)[: max(1, int(limit or 12))]


def _sync_file(*, root: Path, collection_name: str, path: Path, max_file_bytes: int) -> None:
    relative_path = str(path.relative_to(root))
    extension = path.suffix.lower()
    last_modified = path.stat().st_mtime
    if extension not in SUPPORTED_TEXT_EXTENSIONS:
        record = database.upsert_collection_file(
            collection_name=collection_name,
            file_path=str(path),
            relative_path=relative_path,
            file_type="unsupported",
            last_modified=last_modified,
            status="unsupported",
            error_message=f"Unsupported file type: {extension or 'none'}",
            chunk_count=0,
        )
        file_id = str(record.get("id") or "")
        if file_id:
            database.replace_collection_chunks(
                file_id=file_id,
                collection_name=collection_name,
                file_path=str(path),
                relative_path=relative_path,
                chunks=[],
            )
        return

    try:
        if path.stat().st_size > max_file_bytes:
            raise ValueError(f"File is larger than the {max_file_bytes:,} byte first-slice limit.")
        text = _read_text(path)
        chunks = _chunk_text(text)
        if not chunks:
            raise ValueError("No readable text found.")
    except Exception as exc:
        record = database.upsert_collection_file(
            collection_name=collection_name,
            file_path=str(path),
            relative_path=relative_path,
            file_type=TEXT_FILE_TYPE,
            last_modified=last_modified,
            status="failed",
            error_message=str(exc)[:240],
            chunk_count=0,
        )
        file_id = str(record.get("id") or "")
        if file_id:
            database.replace_collection_chunks(
                file_id=file_id,
                collection_name=collection_name,
                file_path=str(path),
                relative_path=relative_path,
                chunks=[],
            )
        return

    record = database.upsert_collection_file(
        collection_name=collection_name,
        file_path=str(path),
        relative_path=relative_path,
        file_type=TEXT_FILE_TYPE,
        last_modified=last_modified,
        status="indexed",
        error_message=None,
        chunk_count=len(chunks),
    )
    database.replace_collection_chunks(
        file_id=str(record.get("id") or ""),
        collection_name=collection_name,
        file_path=str(path),
        relative_path=relative_path,
        chunks=chunks,
    )


def _read_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() in {".html", ".htm"}:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ")
    return _clean_text(raw)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _chunk_text(text: str) -> list[str]:
    clean = _clean_text(text)
    if len(clean) < MIN_CHUNK_CHARS:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + CHUNK_CHAR_LIMIT)
        chunk = clean[start:end].strip()
        if len(chunk) >= MIN_CHUNK_CHARS:
            chunks.append(chunk)
        if end >= len(clean):
            break
        start = max(0, end - 180)
    return chunks


def _collection_names(root: Path) -> list[str]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )
