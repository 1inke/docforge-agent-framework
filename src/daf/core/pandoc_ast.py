"""Pandoc JSON AST parser, inline citation extractor, and deterministic L2a sentence checker.

Shared by P0-1 (evidence-anchored submission checks) and P1-3 (single-pass DOCX rendering).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from daf.core.errors import PandocUnavailableError

SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？!?；;])|\n+")

# R-A: numeric + unit/percent OR comparative/quantitative claim cues
NUMERIC_UNIT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?\s*"
    r"(?:%|％|ms|s|us|μs|ns|h|hr|hrs|min|kg|g|mg|μg|ug|km|m|cm|mm|nm|μm|"
    r"Hz|kHz|MHz|GHz|KB|MB|GB|TB|MB/s|GB/s|ops/s|QPS|TPS|FPS|eV|meV|K|℃|°C|"
    r"mA|A|V|mV|W|kW|MW|Pa|kPa|MPa|GPa|mol|mmol|mL|L|倍|项|次|个|天|小时|分钟|秒|年)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

COMPARATIVE_MARKERS: tuple[str, ...] = (
    "降低",
    "下降",
    "减少",
    "提升",
    "提高",
    "优于",
    "高于",
    "低于",
    "超过",
    "首次",
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
    "outperform",
    "outperforms",
    "outperformed",
    "faster",
    "slower",
    "higher",
    "lower",
    "achieve",
    "achieves",
    "achieved",
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

NUMBER_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")


@dataclass
class ASTStats:
    math_inline: int = 0
    math_display: int = 0
    tables: int = 0
    images: int = 0
    headings: int = 0
    citations: list[str] = field(default_factory=list)


@dataclass
class ParsedSentence:
    index: int
    text: str
    citation_ids: list[str]
    is_hypothesis: bool
    sentence_hash: str


def find_pandoc_binary() -> str | None:
    return shutil.which("pandoc")


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
            # c = [citations_list, rendered_inlines]
            if isinstance(c, list) and len(c) >= 1 and isinstance(c[0], list):
                ids_here: list[str] = []
                for item in c[0]:
                    if isinstance(item, dict) and item.get("citationId"):
                        cid = str(item["citationId"])
                        cites.append(cid)
                        stats.citations.append(cid)
                        ids_here.append(f"@{cid}")
                if ids_here:
                    parts.append("[" + "; ".join(ids_here) + "]")
        elif t == "Image":
            stats.images += 1
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
        elif t == "Table":
            stats.tables += 1
        elif t in {"BulletList", "OrderedList"}:
            items = c if t == "BulletList" else (c[1] if isinstance(c, list) and len(c) >= 2 else [])
            if isinstance(items, list):
                for item_blocks in items:
                    if isinstance(item_blocks, list):
                        segments.extend(_extract_blocks_plain_segments(item_blocks, stats))
        elif t == "BlockQuote" and isinstance(c, list):
            segments.extend(_extract_blocks_plain_segments(c, stats))
        elif t == "Div" and isinstance(c, list) and len(c) >= 2 and isinstance(c[1], list):
            segments.extend(_extract_blocks_plain_segments(c[1], stats))
    return segments


INLINE_CITE_BRACKET_RE = re.compile(r"\[((?:@[A-Za-z0-9_.\-:]+)(?:\s*;\s*@[A-Za-z0-9_.\-:]+)*)\]")


def analyze_markdown_with_pandoc(markdown: str) -> tuple[list[ParsedSentence], ASTStats, dict[str, Any]]:
    """Parse markdown via Pandoc JSON AST and extract sentences, inline citations, and structural stats."""
    ast = parse_markdown_to_ast(markdown)
    stats = ASTStats()
    blocks = ast.get("blocks", []) if isinstance(ast, dict) else []
    segments = _extract_blocks_plain_segments(blocks, stats)

    sentences: list[ParsedSentence] = []
    idx = 0
    for seg in segments:
        # Split on Chinese/English sentence boundaries without breaking decimal numbers like 9.2
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
            ev_ids = [cid for cid in found_ids if cid != "hypothesis"]
            # Preserve unique order
            dedup_ev_ids = list(dict.fromkeys(ev_ids))
            clean_text = INLINE_CITE_BRACKET_RE.sub("", s).strip()
            shash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
            sentences.append(
                ParsedSentence(
                    index=idx,
                    text=clean_text,
                    citation_ids=dedup_ev_ids,
                    is_hypothesis=is_hyp,
                    sentence_hash=shash,
                )
            )
            idx += 1

    return sentences, stats, ast


def sentence_needs_citation(sentence_text: str) -> bool:
    """R-A: Return True if sentence contains a numeric+unit measurement or comparative factual claim."""
    if NUMERIC_UNIT_PATTERN.search(sentence_text):
        return True
    lowered = sentence_text.lower()
    return any(marker in lowered for marker in COMPARATIVE_MARKERS)


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
