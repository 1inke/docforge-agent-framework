"""DocForge Agent Framework (DAF) — Local single-user guardrail framework for document engineering."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

from daf.core.engine import DAFEngine


def _resolve_version() -> str:
    """Prefer checkout pyproject.toml when running from source, else installed package metadata."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        with pyproject.open("rb") as fh:
            proj = tomllib.load(fh).get("project", {})
        if proj.get("name") == "docforge-agent-framework" and proj.get("version"):
            return str(proj["version"])
    try:
        return version("docforge-agent-framework")
    except PackageNotFoundError:
        return "0.2.0"


__version__ = _resolve_version()

__all__ = ["DAFEngine", "__version__"]
