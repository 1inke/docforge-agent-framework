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
    "s",
    "min",
    "Hz",
    "kHz",
    "MHz",
    "GHz",
    "KB",
    "MB",
    "GB",
    "TB",
    "PB",
    "M",
    "G",
    "T",
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
    "m",
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
    "px",
    "pixel",
    "pixels",
    "像素",
    "毫秒",
    "微秒",
    "纳秒",
    "秒",
    "米",
    "厘米",
    "毫米",
    "千米",
    "公里",
    "倍",
)

COUNTING_OR_PLANNING_UNITS: tuple[str, ...] = (
    "年",
    "个月",
    "小节",
    "方面",
    "阶段",
    "季度",
    "个",
    "项",
    "次",
    "章",
    "节",
    "人",
    "篇",
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
    "降到",
    "降至",
    "下降",
    "减少",
    "减至",
    "提升",
    "提升到",
    "提升至",
    "提高",
    "提高到",
    "提高至",
    "增加到",
    "增至",
    "压缩到",
    "压缩至",
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

# Planning / goal markers exempt a sentence unless a completed-state marker is also present.
# Bare "将" is intentionally omitted because Chinese disposal constructions ("该方法将 ...") describe results.
PLANNING_EXEMPT_MARKERS: tuple[str, ...] = (
    "拟将",
    "我们将",
    "本项目将",
    "本研究将",
    "本文将在",
    "将在",
    "将会",
    "将于",
    "将用于",
    "拟",
    "计划",
    "预期",
    "目标是",
    "总体目标",
    "次要目标",
    "旨在",
    "打算",
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
    "降低了",
    "提升了",
    "提高了",
    "减少了",
    "达到了",
    "压缩了",
    "缩短了",
    "实现了",
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
    + "|".join(re.escape(u) for u in sorted(COUNTING_OR_PLANNING_UNITS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])"
)
NUMBER_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")

# Measurement units for R-B unit consistency (superset of PHYSICAL_UNITS)
MEASUREMENT_UNIT_TOKENS: tuple[str, ...] = tuple(
    dict.fromkeys(
        list(PHYSICAL_UNITS)
        + ["mW", "B", "B/s", "KB/s", "TB/s", "sec", "secs", "second", "seconds", "小时"]
    )
)

_CANONICAL_UNIT_ALIASES: dict[str, str] = {
    "％": "%",
    "℃": "°C",
    "μs": "us",
    "微秒": "us",
    "毫秒": "ms",
    "纳秒": "ns",
    "秒": "s",
    "sec": "s",
    "secs": "s",
    "second": "s",
    "seconds": "s",
    "hr": "h",
    "hrs": "h",
    "小时": "h",
    "米": "m",
    "厘米": "cm",
    "毫米": "mm",
    "千米": "km",
    "公里": "km",
    "μm": "um",
    "μg": "ug",
    "μmol": "umol",
    "像素": "px",
    "pixel": "px",
    "pixels": "px",
    "kb/s": "KB/s",
    "mb/s": "MB/s",
    "gb/s": "GB/s",
    "tb/s": "TB/s",
    "b/s": "B/s",
    "ops/s": "ops/s",
    "hz": "Hz",
    "khz": "kHz",
    "mhz": "MHz",
    "ghz": "GHz",
    "db": "dB",
    "dbm": "dBm",
    "qps": "QPS",
    "tps": "TPS",
    "fps": "FPS",
    "flops": "FLOPS",
    "gflops": "GFLOPS",
    "tflops": "TFLOPS",
}


def canonicalize_unit(unit: str) -> str:
    """Normalize equivalent measurement unit notations while preserving prefix scales (s != ms != us, m != M)."""
    cleaned = unit.strip()
    if not cleaned:
        return ""
    if cleaned in _CANONICAL_UNIT_ALIASES:
        return _CANONICAL_UNIT_ALIASES[cleaned]
    lowered = cleaned.lower()
    if lowered in _CANONICAL_UNIT_ALIASES:
        return _CANONICAL_UNIT_ALIASES[lowered]
    return cleaned


def _build_unit_prefix_regex(extra_units: list[str] | None = None) -> re.Pattern[str]:
    units = list(MEASUREMENT_UNIT_TOKENS)
    if extra_units:
        for u in extra_units:
            cleaned = str(u).strip()
            if cleaned and cleaned not in units:
                units.append(cleaned)
    ordered = sorted(units, key=len, reverse=True)
    escaped = "|".join(re.escape(u) for u in ordered)
    return re.compile(rf"^\s*({escaped})(?![A-Za-z0-9_])", re.IGNORECASE)


DEFAULT_UNIT_PREFIX_PATTERN = _build_unit_prefix_regex()


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
class CitationClause:
    text: str
    evidence_ids: list[str]


@dataclass
class ParsedSentence:
    index: int
    text: str
    citation_ids: list[str]
    crossref_ids: list[str]
    is_hypothesis: bool
    sentence_hash: str
    citation_clauses: list[CitationClause] = field(default_factory=list)


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


def _clean_sentence_text(raw_text: str) -> str:
    cleaned = CITE_TOKEN_RE.sub("", raw_text)
    cleaned = re.sub(r"\s+([。！？!?；;，,])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


CITE_TOKEN_RE = re.compile(r"\uE000CITE:(\d+)\uE001")
TABLE_CAPTION_ATTR_RE = re.compile(r"(?m)^\s*:\s+.*?\{#(tbl:[A-Za-z0-9_.\-:]+)\}")
_PREFIX_CUE_RE = re.compile(
    r"(?:根据|据|如|见|参照|按照|依据|参考文献|according\s+to|per|in|from)\s*$",
    re.IGNORECASE,
)


def _inline_to_text_and_cites(
    inlines: list[Any],
    stats: ASTStats,
    cite_tokens: list[list[str]],
) -> tuple[str, list[str]]:
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
                        ids_here.append(cid)
                if ids_here:
                    tok_idx = len(cite_tokens)
                    cite_tokens.append(ids_here)
                    # Sentinel without punctuation guarantees sentence splitters never split inside [@a; @b]
                    parts.append(f"\uE000CITE:{tok_idx}\uE001")
        elif t == "Image":
            stats.images += 1
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
                sub_text, sub_cites = _inline_to_text_and_cites(c[1], stats, cite_tokens)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t in {"Emph", "Strong", "Strikeout", "Superscript", "Subscript", "SmallCaps"}:
            if isinstance(c, list):
                sub_text, sub_cites = _inline_to_text_and_cites(c, stats, cite_tokens)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t == "Quoted":
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
                sub_text, sub_cites = _inline_to_text_and_cites(c[1], stats, cite_tokens)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t in {"Link", "Span"}:
            if t == "Span" and isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
                sub_text, sub_cites = _inline_to_text_and_cites(c[1], stats, cite_tokens)
                parts.append(sub_text)
                cites.extend(sub_cites)
        elif t == "Note":
            if isinstance(c, list):
                sub_blocks_text = _extract_blocks_plain_segments(c, stats, cite_tokens)
                parts.append(" ".join(sub_blocks_text))
    return "".join(parts), cites


def _extract_blocks_plain_segments(
    blocks: list[Any],
    stats: ASTStats,
    cite_tokens: list[list[str]],
) -> list[str]:
    segments: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("t")
        c = block.get("c")
        if t in {"Para", "Plain"} and isinstance(c, list):
            txt, _ = _inline_to_text_and_cites(c, stats, cite_tokens)
            if txt.strip():
                segments.append(txt.strip())
        elif t == "Header":
            stats.headings += 1
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list) and c[1]:
                _record_label(c[1][0], stats)
        elif t == "Table":
            stats.tables += 1
            tbl_id = c[0][0] if (isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]) else ""
            _record_label(tbl_id, stats)
            if isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list) and len(c[1]) >= 2:
                cap_blocks = c[1][1]
                if isinstance(cap_blocks, list):
                    cap_segs = _extract_blocks_plain_segments(cap_blocks, stats, cite_tokens)
                    if not tbl_id:
                        m = re.search(r"\{#(tbl:[A-Za-z0-9_.\-:]+)\}", " ".join(cap_segs))
                        if m:
                            _record_label(m.group(1), stats)
        elif t == "Figure":
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c, list) and len(c) >= 3 and isinstance(c[2], list):
                _extract_blocks_plain_segments(c[2], stats, cite_tokens)
        elif t in {"BulletList", "OrderedList"}:
            items = c if t == "BulletList" else (c[1] if isinstance(c, list) and len(c) >= 2 else [])
            if isinstance(items, list):
                for item_blocks in items:
                    if isinstance(item_blocks, list):
                        segments.extend(_extract_blocks_plain_segments(item_blocks, stats, cite_tokens))
        elif t == "BlockQuote" and isinstance(c, list):
            segments.extend(_extract_blocks_plain_segments(c, stats, cite_tokens))
        elif t == "Div" and isinstance(c, list) and len(c) >= 2:
            if isinstance(c[0], list) and c[0]:
                _record_label(c[0][0], stats)
            if isinstance(c[1], list):
                segments.extend(_extract_blocks_plain_segments(c[1], stats, cite_tokens))
    return segments


def _extract_citation_clauses(
    raw_sentence: str,
    cite_tokens: list[list[str]],
    clean_sentence: str,
) -> list[CitationClause]:
    """Scope each distinct citation group inside a sentence to its own clause text."""
    raw_tokens: list[tuple[int, int, list[str]]] = []
    for m in CITE_TOKEN_RE.finditer(raw_sentence):
        tok_idx = int(m.group(1))
        raw_ids = cite_tokens[tok_idx] if 0 <= tok_idx < len(cite_tokens) else []
        ev_here = list(
            dict.fromkeys(cid for cid in raw_ids if cid != "hypothesis" and not is_crossref_id(cid))
        )
        raw_tokens.append((m.start(), m.end(), ev_here))

    if not any(ev_here for _, _, ev_here in raw_tokens):
        return []

    ev_spans = [(s, e, ids) for (s, e, ids) in raw_tokens if ids]
    raw_segs: list[str] = []
    last = 0
    for s, e, _ in ev_spans:
        seg = CITE_TOKEN_RE.sub("", raw_sentence[last:s])
        raw_segs.append(seg)
        last = e
    raw_segs.append(CITE_TOKEN_RE.sub("", raw_sentence[last:]))

    merged_segs: list[str] = [raw_segs[0]]
    merged_groups: list[list[str]] = []
    for idx, (_s, _e, ids) in enumerate(ev_spans):
        seg_before = raw_segs[idx]
        if idx > 0 and re.sub(r"[\s,，、;；:：]+", "", seg_before) == "":
            merged_groups[-1] = list(dict.fromkeys([*merged_groups[-1], *ids]))
        else:
            if merged_groups:
                merged_segs.append(seg_before)
            merged_groups.append(list(ids))
    merged_segs.append(raw_segs[-1])

    if len(merged_groups) == 1:
        return [CitationClause(text=clean_sentence, evidence_ids=merged_groups[0])]

    def _strip_clause(text: str) -> str:
        t = re.sub(r"\s+([。！？!?；;，,])", r"\1", text)
        t = re.sub(r"[ \t]{2,}", " ", t).strip()
        return t.strip(" \t\r\n,，、;；:：")

    cleaned_segs = [_strip_clause(s) for s in merged_segs]
    seg0 = cleaned_segs[0]
    trailing = cleaned_segs[-1]
    trailing_nonpunct = trailing.strip("。！？!?. ")
    use_prefix = (
        (not seg0 or (not NUMBER_TOKEN_PATTERN.search(seg0) and bool(_PREFIX_CUE_RE.search(seg0))))
        and bool(trailing_nonpunct)
    )

    clauses: list[CitationClause] = []
    if use_prefix:
        for i, ids in enumerate(merged_groups):
            txt = cleaned_segs[i + 1]
            if i == 0 and seg0:
                txt = f"{seg0} {txt}".strip() if txt else seg0
            clauses.append(CitationClause(text=txt or clean_sentence, evidence_ids=ids))
    else:
        for i, ids in enumerate(merged_groups):
            txt = cleaned_segs[i]
            if i == len(merged_groups) - 1 and trailing_nonpunct:
                txt = f"{txt} {trailing}".strip() if txt else trailing
            clauses.append(CitationClause(text=txt or clean_sentence, evidence_ids=ids))
    return clauses


def analyze_markdown_with_pandoc(markdown: str) -> tuple[list[ParsedSentence], ASTStats, dict[str, Any]]:
    """Parse markdown via Pandoc JSON AST and extract sentences, citations, clauses, crossrefs, and labels."""
    ast = parse_markdown_to_ast(markdown)
    stats = ASTStats()
    cite_tokens: list[list[str]] = []
    blocks = ast.get("blocks", []) if isinstance(ast, dict) else []
    segments = _extract_blocks_plain_segments(blocks, stats, cite_tokens)

    sentences: list[ParsedSentence] = []
    idx = 0
    for seg in segments:
        raw_parts = re.split(r"(?<=[。！？!?；;])|(?<=\.)(?=\s+[A-Z\u4e00-\u9fff])|\n+", seg)
        for part in raw_parts:
            s = part.strip()
            if not s:
                continue
            found_ids: list[str] = []
            for m in CITE_TOKEN_RE.finditer(s):
                tok_idx = int(m.group(1))
                if 0 <= tok_idx < len(cite_tokens):
                    found_ids.extend(cite_tokens[tok_idx])
            is_hyp = "hypothesis" in found_ids
            xref_ids = list(dict.fromkeys(cid for cid in found_ids if is_crossref_id(cid)))
            ev_ids = list(
                dict.fromkeys(cid for cid in found_ids if cid != "hypothesis" and not is_crossref_id(cid))
            )
            clean_text = _clean_sentence_text(s)
            clauses = _extract_citation_clauses(s, cite_tokens, clean_text)
            shash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
            sentences.append(
                ParsedSentence(
                    index=idx,
                    text=clean_text,
                    citation_ids=ev_ids,
                    crossref_ids=xref_ids,
                    is_hypothesis=is_hyp,
                    sentence_hash=shash,
                    citation_clauses=clauses,
                )
            )
            idx += 1

    return sentences, stats, ast


def detect_table_caption_attribute_syntax(markdown: str) -> list[str]:
    """Detect `: Caption {#tbl:id}` table attribute syntax (fragile on Pandoc < 3.8); returns sorted unique labels."""
    no_code = re.sub(r"```[\s\S]*?```", "", markdown)
    labels: list[str] = []
    for m in TABLE_CAPTION_ATTR_RE.finditer(no_code):
        tid = m.group(1)
        if tid not in labels:
            labels.append(tid)
    return labels


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


def extract_number_unit_pairs(
    text: str,
    extra_units: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Extract (number, canonical_unit) pairs with strict numeric boundaries and immediate following unit."""
    unit_prefix_re = _build_unit_prefix_regex(extra_units) if extra_units else DEFAULT_UNIT_PREFIX_PATTERN
    pairs: list[tuple[str, str]] = []
    for m in NUMBER_TOKEN_PATTERN.finditer(text):
        num = m.group(0)
        um = unit_prefix_re.match(text[m.end() :])
        canon_unit = canonicalize_unit(um.group(1)) if um else ""
        pairs.append((num, canon_unit))
    return pairs


def number_present_with_boundary(number_str: str, excerpt: str) -> bool:
    """R-B: Boundary-aware numeric match so '5' never matches '15' or '5.2'."""
    escaped = re.escape(number_str)
    pattern = re.compile(rf"(?<![\d.]){escaped}(?![\d.])")
    return bool(pattern.search(excerpt))


def hypothesis_has_false_experiment_marker(sentence_text: str) -> list[str]:
    """R-D: Return list of matched false-experiment markers in a hypothesis sentence."""
    lowered = sentence_text.lower()
    return [m for m in HYPOTHESIS_FALSE_EXPERIMENT_MARKERS if m in lowered]
