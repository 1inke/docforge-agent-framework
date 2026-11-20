"""Deployment-owned import roots and descriptor-based source snapshots."""

from __future__ import annotations

import ctypes
import json
import os
import stat
from pathlib import Path

from daf.core.errors import SecurityBoundaryError

SOURCE_ROOTS_ENV = "DAF_ALLOWED_SOURCE_ROOTS"


def resolve_source_roots(roots: list[str | Path] | None = None) -> list[Path] | None:
    """Explicit [] denies all; absent config denies import while discovery still works.

    Environment syntax is a JSON array, avoiding Windows drive-letter separators.
    Only trusted deployment code may supply this argument; never an Agent tool.
    """
    if roots is None:
        raw = os.environ.get(SOURCE_ROOTS_ENV)
        if raw is None:
            return None
        try:
            roots = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SecurityBoundaryError(
                "DAF_ALLOWED_SOURCE_ROOTS 必须为目录的 JSON 数组。",
                code="SOURCE_IMPORT_CONFIG_INVALID",
            ) from exc
    if not isinstance(roots, list) or any(
        not isinstance(p, (str, Path)) or not str(p).strip() for p in roots
    ):
        raise SecurityBoundaryError(
            "导入目录配置必须为非空目录路径组成的列表。",
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
                "允许导入的目录不存在或配置无效。", code="SOURCE_IMPORT_CONFIG_INVALID"
            ) from exc
        if path not in result:
            result.append(path)
    return result


def _assert_inside(path: Path, roots: list[Path] | None) -> None:
    if roots is None:
        raise SecurityBoundaryError(
            "未配置本地导入目录；请由部署方设置 DAF_ALLOWED_SOURCE_ROOTS。",
            code="SOURCE_IMPORT_NOT_CONFIGURED",
        )
    if not any(path.is_relative_to(root) for root in roots):
        raise SecurityBoundaryError("文件不在允许导入目录中。")


def _reject_links(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(
            info, "st_file_attributes", 0
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise SecurityBoundaryError("来源文件及父目录不能包含符号链接或重解析点。")


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
        lexical = Path(os.path.abspath(raw))
        _assert_inside(lexical, roots)
        _reject_links(lexical)
        resolved = lexical.resolve(strict=True)
        _assert_inside(resolved, roots)
        if not resolved.is_file():
            raise ValueError("not a regular file")
    except SecurityBoundaryError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise SecurityBoundaryError(
            "来源或模板文件路径无效或不存在。", code="SOURCE_IMPORT_PATH_INVALID"
        ) from exc
    return resolved


def _descriptor_path(descriptor: int, fallback: Path) -> Path:
    if os.name == "nt":
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel.GetFinalPathNameByHandleW
        fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        size = 32768
        buffer = ctypes.create_unicode_buffer(size)
        length = fn(msvcrt.get_osfhandle(descriptor), buffer, size, 0)
        if not length or length >= size:
            raise SecurityBoundaryError("无法验证已打开文件的最终路径。")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value).resolve(strict=True)
    fd_path = Path(f"/proc/self/fd/{descriptor}")
    return (
        fd_path.resolve(strict=True)
        if fd_path.exists()
        else fallback.resolve(strict=True)
    )


def read_import_snapshot(
    file_path: str | Path,
    allowed_roots: list[Path] | None = None,
    allowed_extensions: frozenset[str] = frozenset({".md", ".txt", ".docx", ".pdf"}),
) -> tuple[Path, bytes]:
    """Validate the open descriptor before reading, then parse only these bytes.

    This closes path reopen races for PDF/DOCX parsing. Deployment roots still
    need filesystem ACLs; this is not an OS sandbox against a same-user attacker.
    """
    roots = resolve_source_roots(allowed_roots)
    path = validate_import_path(file_path, roots)
    if path.suffix.lower() not in allowed_extensions:
        raise SecurityBoundaryError(
            "不支持此来源或模板文件类型。", code="SOURCE_TYPE_UNSUPPORTED"
        )
    before = path.stat()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise SecurityBoundaryError("文件在校验和打开之间发生变化。")
            final_path = _descriptor_path(stream.fileno(), path)
            _assert_inside(final_path, roots)
            if final_path != path:
                raise SecurityBoundaryError("已打开文件的最终路径与授权路径不一致。")
            _reject_links(path)
            data = stream.read()
            after = os.fstat(stream.fileno())
            if (opened.st_size, opened.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise SecurityBoundaryError("读取期间文件被修改，请重新导入。")
            return path, data
    except OSError as exc:
        raise SecurityBoundaryError(
            "无法安全读取来源或模板文件。", code="SOURCE_IMPORT_PATH_INVALID"
        ) from exc
