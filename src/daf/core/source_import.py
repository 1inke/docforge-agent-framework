"""Local import root guardrails for source files and templates.

Default-deny policy: local imports require explicitly configured directories via
``DAF_ALLOWED_SOURCE_ROOTS`` (JSON array) or trusted ``allowed_source_roots``
argument. Paths are resolved and checked to reside within configured roots.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from daf.core.errors import SecurityBoundaryError

SOURCE_ROOTS_ENV = "DAF_ALLOWED_SOURCE_ROOTS"


def resolve_source_roots(roots: list[str | Path] | None = None) -> list[Path] | None:
    """Resolve allowed import root directories.

    - ``None`` (env unset): local imports are denied, while read/discovery still works.
    - ``[]``: explicitly denies all local imports.
    - Non-empty list: each entry must be an existing directory.
    """
    if roots is None:
        raw = os.environ.get(SOURCE_ROOTS_ENV)
        if raw is None:
            return None
        try:
            roots = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SecurityBoundaryError(
                "DAF_ALLOWED_SOURCE_ROOTS must be a JSON array of directory paths.",
                code="SOURCE_IMPORT_CONFIG_INVALID",
            ) from exc
    if not isinstance(roots, list) or any(
        not isinstance(p, (str, Path)) or not str(p).strip() for p in roots
    ):
        raise SecurityBoundaryError(
            "allowed_source_roots must be a list of non-empty directory paths.",
            code="SOURCE_IMPORT_CONFIG_INVALID",
        )
    result: list[Path] = []
    for item in roots:
        try:
            path = Path(item).resolve(strict=True)
            if not path.is_dir():
                raise ValueError("not a directory")
        except (OSError, ValueError, RuntimeError) as exc:
            raise SecurityBoundaryError(
                "Configured allowed source root does not exist or is not a directory.",
                code="SOURCE_IMPORT_CONFIG_INVALID",
            ) from exc
        if path not in result:
            result.append(path)
    return result


def _assert_inside(path: Path, roots: list[Path] | None) -> None:
    if roots is None:
        raise SecurityBoundaryError(
            "Local source import is not configured; set DAF_ALLOWED_SOURCE_ROOTS.",
            code="SOURCE_IMPORT_NOT_CONFIGURED",
        )
    if not any(path.is_relative_to(root) for root in roots):
        raise SecurityBoundaryError("File is outside configured allowed source roots.")


def validate_import_path(
    file_path: str | Path, allowed_roots: list[Path] | None = None
) -> Path:
    roots = resolve_source_roots(allowed_roots)
    if roots is None or not roots:
        _assert_inside(Path.cwd(), roots)
    try:
        raw = str(file_path)
        if not raw.strip() or "\x00" in raw:
            raise ValueError("invalid path")
        resolved = Path(raw).resolve(strict=True)
        _assert_inside(resolved, roots)
        if not resolved.is_file():
            raise ValueError("not a regular file")
    except SecurityBoundaryError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise SecurityBoundaryError(
            "Source or template file path is invalid or does not exist.",
            code="SOURCE_IMPORT_PATH_INVALID",
        ) from exc
    return resolved


def read_import_snapshot(
    file_path: str | Path,
    allowed_roots: list[Path] | None = None,
    allowed_extensions: frozenset[str] = frozenset({".md", ".txt", ".docx", ".pdf"}),
) -> tuple[Path, bytes]:
    """Validate path against allowed roots and return ``(resolved_path, bytes)``."""
    roots = resolve_source_roots(allowed_roots)
    path = validate_import_path(file_path, roots)
    if path.suffix.lower() not in allowed_extensions:
        raise SecurityBoundaryError(
            f"Unsupported source or template extension '{path.suffix}'.",
            code="SOURCE_TYPE_UNSUPPORTED",
        )
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise SecurityBoundaryError(
            "Failed to read source or template file.",
            code="SOURCE_IMPORT_PATH_INVALID",
        ) from exc
