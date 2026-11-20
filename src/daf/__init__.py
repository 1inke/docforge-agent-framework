"""DocForge Agent Framework (DAF) — Local single-user guardrail framework for document engineering."""

from importlib.metadata import PackageNotFoundError, version

from daf.core.engine import DAFEngine

try:
    __version__ = version("docforge-agent-framework")
except PackageNotFoundError:
    __version__ = "0.2.0"

__all__ = ["DAFEngine", "__version__"]
