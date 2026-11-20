"""Pandoc JSON AST parser, citation/crossref classifier, and deterministic L2a sentence checker."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Literal

from daf.core.errors import PandocUnavailableError

MIN_RECOMMENDED_PANDOC_VERSION: tuple[int, int, int] = (3, 2, 0)

CROSSREF_PREFIXES: tuple[str, ...] = ("fig:", "tbl:", "eq:", "sec:")

# R3: Physical & measurement units (trigger major when paired with a number).
# Counting/planning units (年, 个, 项, 次, 章, 节, 人, 篇,...) are explicitly excluded.
PHYSICAL_UNITS: tuple[str, ...] = (
    "%",
    "％",
    "ms",
    "us",
    "μs",
    "ns",
    "Hz",
    "kHz",
    "MHz",
    "GHz",
    "KB",
    "MB",
    "GB",
    "TB",
    "PB",
    "MB/s",
    "GB/s",
    "ops/s",
    "QPS",
    "TPS",
    "FPS",
    "FLOPS",
    "GFLOPS",
    "TFLOPS",
    "eV",
    "meV",
    "keV",
    "MeV",
    "GeV",
    "K",
    "℃",
    "°C",
    "mA",
    "A",
    "mV",
    "V",
    "kV",
    "W",
    "kW",
    "MW",
    "Pa",
    "kPa",
    "MPa",
    "GPa",
    "mol",
    "mmol",
    "μmol",
    "mL",
    "L",
    "kg",
    "g",
    "mg",
    "μg",
    "ug",
    "km",
    "cm",
    "mm",
    "nm",
    "μm",
    "um",
    "m/s",
    "dB",
    "mAh",
    "Wh",
    "kWh",
    "rpm",
    "ppm",
    "ppb",
    "h",
    "hr",
    "hrs",
    "倍",
)

COUNTING_OR_PLANNING_UNITS: tuple[str, ...] = (
    "年",
    "个",
    "项",
    "次",
    "章",
    "节",
    "人",
    "篇",
    "方面",
    "阶段",
    "步",
    "条",
    "类",
    "种",
    "组",
    "套",
    "名",
    "位",
    "天",
    "周",
    "月",
    "季度",
)

# Strong comparative/priority markers -> trigger minor alone (do not block release; semantic review checks)
STRONG_COMPARATIVE_MARKERS: tuple[str, ...] = (
    "优于",
    "超过",
    "首次",
    "领先",
    "显著高于",
    "显著低于",
    "outperform",
    "outperforms",
    "outperformed",
    "first to",
    "state-of-the-art",
)

# Weak comparative markers -> trigger major ONLY when paired with a remaining numeric token
WEAK_COMPARATIVE_MARKERS: tuple[str, ...] = (
    "降低",
    "下降",
    "减少",
    "提升",
    "提高",
    "高于",
    "低于",
    "达到",
    "缩短",
    "加速",
    "更快",
    "更低",
    "更高",
    "reduce",
    "reduces",
    "reduced",
    "improve",
    "improves",
    "improved",
    "faster",
    "slower",
    "higher",
    "lower",
    "achieve",
    "achieves",
    "achieved",
)

# Planning / goal markers exempt a sentence unless a completed-state marker is also present
PLANNING_EXEMPT_MARKERS: tuple[str, ...] = (
    "拟",
    "计划",
    "预期",
    "目标是",
    "总体目标",
    "次要目标",
    "旨在",
    "打算",
    "将",
    "希望",
    "aim to",
    "aims to",
    "plan to",
    "plans to",
    "intend to",
    "will ",
    "target is",
)

COMPLETED_FACT_MARKERS: tuple[str, ...] = (
    "已",
    "已经",
    "实验表明",
    "实验证明",
    "实测",
    "结果显示",
    "测试表明",
    "数据显示",
    "测量表明",
    "observed",
    "measured",
    "results show",
    "experiments show",
    "empirically verified",
)

HYPOTHESIS_FALSE_EXPERIMENT_MARKERS: tuple[str, ...] = (
    "实验证明",
    "实验表明",
    "实测结果证实",
    "实验结果证实",
    "数据显示",
    "测试表明",
    "结果显示",
    "已验证",
    "实测达到",
    "experimentally proved",
    "results show",
    "experiments show",
    "empirically verified",
)

YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}\s*年?")
COUNTING_NUM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:"
    + "|".join(re.escape(u) for u in COUNTING_OR_PLANNING_UNITS)
    + r")(?![A-Za-z0-9_])"
)
NUMBER_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")


def _build_physical_unit_regex(extra_units: list[str] | None = None) -> re.Pattern[str]:
    units = list(PHYSICAL_UNITS)
    if extra_units:
        for u in extra_units:
            cleaned = str(u).strip()
            if cleaned and cleaned not in units:
                units.append(cleaned)
    # Sort longest-first so 'GB/s' matches before 'G' or 'B'
    ordered = sorted(units, key=len, reverse=True)
    escaped = "|".join(re.escape(u) for u in ordered)
    return re.compile(
        rf"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:{escaped})(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


DEFAULT_PHYSICAL_UNIT_PATTERN = _build_physical_unit_regex()


@dataclass
class ASTStats:
    math_inline: int = 0
    math_display: int = 0
    tables: int = 0
    images: int = 0
    headings: int = 0
    citations: list[str] = field(default_factory=list)
    defined_labels: list[str] = field(default_factory=list)
    crossref_targets: list[str] = field(default_factory=list)


@dataclass
class ParsedSentence:
    index: int
    text: str
    citation_ids: list[str]
    crossref_ids: list[str]
    is_hypothesis: bool
    sentence_hash: str


def find_pandoc_binary() -> str | None:
    return shutil.which("pandoc")


def get_pandoc_version() -> tuple[int, ...] | None:
    """Return local Pandoc version tuple e.g. (3, 8, 3), or None if unavailable."""
    pandoc_bin = find_pandoc_binary()
    if not pandoc_bin:
        return None
    try:
        proc = subprocess.run(
            [pandoc_bin, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        first_line = proc.stdout.decode("utf-8", errors="replace").splitlines()[0]
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", first_line)
        if not m:
            return None
        parts = [int(m.group(1)), int(m.group(2))]
        if m.group(3) is not None:
            parts.append(int(m.group(3)))
        return tuple(parts)
    except Exception:
        return None


def parse_markdown_to_ast(markdown: str) -> dict[str, Any]:
    """Run ``pandoc -f markdown -t json`` and return the parsed AST dict."""
    pandoc_bin = find_pandoc_binary()
    if not pandoc_bin:
        raise PandocUnavailableError(
            "Pandoc executable not found in PATH; required for Markdown AST parsing and DOCX rendering."
        )
    proc = subprocess.run(
        [pandoc_bin, "-f", "markdown", "-t", "json"],
        input=markdown.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise PandocUnavailableError(f"Pandoc AST parsing failed: {err}")
    return json.loads(proc.stdout.decode("utf-8"))


def is_crossref_id(cid: str) -> bool:
    return cid.startswith(CROSSREF_PREFIXES)


def _record_label(ident: Any, stats: ASTStats) -> None:
    if isinstance(ident, str) and is_crossref_id(ident) and ident not in stats.defined_labels:
        stats.defined_labels.append(ident)


def _inline_to_text_and_cites(inlines: list[Any], stats: ASTStats) -> tuple[str, list[str]]:
    parts: list[str] = []
    cites: list[str] = []
    for node in inlines:
        if not isinstance(node, dict):
            continue
        t = node.get("t")
        c = node.get("c")
        if t == "Str":
            parts.append(str(c))
        elif t in {"Space", "SoftBreak"}:
            parts.append(" ")
        elif t == "LineBreak":
            parts.append("\n")
        elif t == "Code":
            if isinstance(c, list) and len(c) >= 2:
                parts.append(str(c[1]))
        elif t == "Math":
            if isinstance(c, list) and len(c) >= 2:
                mtype = c[0].get("t") if isinstance(c[0], dict) else ""
                if mtype == "DisplayMath":
                    stats.math_display += 1
                else:
                    stats.math_inline += 1
                parts.append(str(c[1]))
        elif t == "Cite":
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list):
                ids_here: list[str] = []
                for item in c[0]:
                    if isinstance(item, dict) and item.get("citationId"):
                        cid = str(item["citationId"])
                        cites.append(cid)
                        if is_crossref_id(cid):
                            if cid not in stats.crossref_targets:
                                stats.crossref_targets.append(cid)
                        elif cid != "hypothesis":
                            stats.citations.append(cid)
                        ids_here.append(f"@{cid}")
                if ids_here:
                    parts.append("[" + "; ".join(ids_here) + "]")
        elif t == "Image":
            stats.images += 1
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
                sub_text, sub_cites = _inline_to_text_and_cites(c[1], stats)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t in {"Emph", "Strong", "Strikeout", "Superscript", "Subscript", "SmallCaps"}:
            if isinstance(c, list):
                sub_text, sub_cites = _inline_to_text_and_cites(c, stats)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t == "Quoted":
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
                sub_text, sub_cites = _inline_to_text_and_cites(c[1], stats)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t in {"Link", "Span"}:
            if t == "Span" and isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
                sub_text, sub_cites = _inline_to_text_and_cites(c[1], stats)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t == "Note":
            if isinstance(c, list):
                sub_blocks_text = _extract_blocks_plain_segments(c, stats)
                parts.append(" ".join(sub_blocks_text))
    return "".join(parts), cites


def _extract_blocks_plain_segments(blocks: list[Any], stats: ASTStats) -> list[str]:
    segments: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("t")
        c = block.get("c")
        if t in {"Para", "Plain"} and isinstance(c, list):
            txt, _ = _inline_to_text_and_cites(c, stats)
            if txt.strip():
                segments.append(txt.strip())
        elif t == "Header":
            stats.headings += 1
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list) and c[1]:
                _record_label(c[1][0], stats)
        elif t == "Table":
            stats.tables += 1
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list) and len(c[1]) >= 2:
                cap_blocks = c[1][1]
                if isinstance(cap_blocks, list):
                    _extract_blocks_plain_segments(cap_blocks, stats)
        elif t == "Figure":
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 3 and isinstance(c[2], list):
                _extract_blocks_plain_segments(c[2], stats)
        elif t in {"BulletList", "OrderedList"}:
            items = c if t == "BulletList" else (c[1] if isinstance(c, list) and len(c) >= 2 else [])
            if isinstance(items, list):
                for item_blocks in items:
                    if isinstance(item_blocks, list):
                        segments.extend(_extract_blocks_plain_segments(item_blocks, stats))
        elif t == "BlockQuote" and isinstance(c, list):
            segments.extend(_extract_blocks_plain_segments(c, stats))
        elif t == "Div" and isinstance(c, list) and len(c) >= 2:
            if isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c[1], list):
                segments.extend(_extract_blocks_plain_segments(c[1], stats))
    return segments


INLINE_CITE_BRACKET_RE = re.compile(r"\[((?:@[A-Za-z0-9_.\-:]+)(?:\s*;\s*@[A-Za-z0-9_.\-:]+)*)\]")


def analyze_markdown_with_pandoc(markdown: str) -> tuple[list[ParsedSentence], ASTStats, dict[str, Any]]:
    """Parse markdown via Pandoc JSON AST and extract sentences, citations, crossrefs, and labels."""
    ast = parse_markdown_to_ast(markdown)
    stats = ASTStats()
    blocks = ast.get("blocks", []) if isinstance(ast, dict) else []
    segments = _extract_blocks_plain_segments(blocks, stats)

    sentences: list[ParsedSentence] = []
    idx = 0
    for seg in segments:
        raw_parts = re.split(r"(?<=[。！？!?；;])|(?<=\.)(?=\s+[A-Z\u4e00-\u9fff])|\n+", seg)
        for part in raw_parts:
            s = part.strip()
            if not s:
                continue
            found_ids: list[str] = []
            for m in INLINE_CITE_BRACKET_RE.finditer(s):
                for token in m.group(1).split(";"):
                    cid = token.strip().lstrip("@")
                    if cid:
                        found_ids.append(cid)
            is_hyp = "hypothesis" in found_ids
            xref_ids = list(dict.fromkeys(cid for cid in found_ids if is_crossref_id(cid)))
            ev_ids = list(
                dict.fromkeys(cid for cid in found_ids if cid != "hypothesis" and not is_crossref_id(cid))
            )
            clean_text = INLINE_CITE_BRACKET_RE.sub("", s).strip()
            shash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
            sentences.append(
                ParsedSentence(
                    index=idx,
                    text=clean_text,
                    citation_ids=ev_ids,
                    crossref_ids=xref_ids,
                    is_hypothesis=is_hyp,
                    sentence_hash=shash,
                )
            )
            idx += 1

    return sentences, stats, ast


def classify_uncited_sentence(
    sentence_text: str,
    extra_units: list[str] | None = None,
) -> Literal["major", "minor"] | None:
    """R3 narrowed R-A classifier:
    - Planning/goal cues exempt sentences lacking completed-fact cues.
    - 4-digit years and counting/planning units are stripped before numeric checks.
    - Number + physical/percent unit -> 'major'.
    - Weak comparative + remaining number -> 'major'.
    - Strong comparative alone -> 'minor' (does not block release).
    """
    lowered = sentence_text.lower()
    has_planning = any(m in lowered for m in PLANNING_EXEMPT_MARKERS)
    has_completed = any(m in lowered for m in COMPLETED_FACT_MARKERS)
    if has_planning and not has_completed:
        return None

    stripped = YEAR_PATTERN.sub(" ", sentence_text)
    stripped = COUNTING_NUM_PATTERN.sub(" ", stripped)

    unit_re = _build_physical_unit_regex(extra_units) if extra_units else DEFAULT_PHYSICAL_UNIT_PATTERN
    if unit_re.search(stripped):
        return "major"

    stripped_lower = stripped.lower()
    has_number = bool(NUMBER_TOKEN_PATTERN.search(stripped))
    if has_number and any(m in stripped_lower for m in WEAK_COMPARATIVE_MARKERS):
        return "major"

    if any(m in stripped_lower for m in STRONG_COMPARATIVE_MARKERS):
        return "minor"

    return None


def sentence_needs_citation(sentence_text: str, extra_units: list[str] | None = None) -> bool:
    """Boolean convenience wrapper around:func:`classify_uncited_sentence`."""
    return classify_uncited_sentence(sentence_text, extra_units=extra_units) is not None


def extract_numbers_strict(text: str) -> list[str]:
    """Extract numeric tokens with strict digit/dot boundaries."""
    return NUMBER_TOKEN_PATTERN.findall(text)


def number_present_with_boundary(number_str: str, excerpt: str) -> bool:
    """R-B: Boundary-aware numeric match so '5' never matches '15' or '5.2'."""
    escaped = re.escape(number_str)
    pattern = re.compile(rf"(?<![\d.]){escaped}(?![\d.])")
    return bool(pattern.search(excerpt))


def hypothesis_has_false_experiment_marker(sentence_text: str) -> list[str]:
    """R-D: Return list of matched false-experiment markers in a hypothesis sentence."""
    lowered = sentence_text.lower()
    return [m for m in HYPOTHESIS_FALSE_EXPERIMENT_MARKERS if m in lowered]
