"""
PatchEngine — lightweight SEARCH/REPLACE-style patch engine for the
project's Markdown documents (World & Setting, Chapters).

Inspired by the LLM -> edit-instructions -> apply-diff pattern used by
tools like Aider/Continue, but implemented from scratch, with no
external dependency, and adapted to plain Markdown text instead of
source code / file-based diffs.

Supported patch blocks (a model response may contain several):

    <<<<<<< REPLACE
    exact text that already exists in the document
    =======
    replacement text
    >>>>>>> REPLACE

    <<<<<<< DELETE
    exact text that already exists in the document
    >>>>>>> DELETE

    <<<<<<< ADD
    SECTION: Name of an existing or new Markdown section
    =======
    content to append under that section
    >>>>>>> ADD

Design rules:
- SEARCH text must match the current document EXACTLY. If it matches
  zero or more-than-one places, that is an error — the engine never
  guesses which occurrence was meant and never silently edits the
  wrong text.
- Validation happens before anything is applied. Apply is atomic: if
  `strict` (default), either every patch in the batch is valid and all
  are applied, or none are and the original document is returned
  unchanged.
- This module never touches disk. Callers decide when/whether to
  persist the returned document (e.g. via engine.storage.save_project),
  so a failed patch can never corrupt what's already saved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("patch_engine")


class PatchOp(str, Enum):
    REPLACE = "REPLACE"
    DELETE = "DELETE"
    ADD = "ADD"


@dataclass
class Patch:
    op: PatchOp
    search: str = ""
    replace: str = ""
    section: str = ""
    raw_block: str = ""


@dataclass
class PatchError:
    index: int
    reason: str
    raw_block: str = ""


@dataclass
class PatchResult:
    """Structured result of applying a batch of patches."""
    success: bool
    document: str = ""
    applied: int = 0
    errors: list[PatchError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_BLOCK_RE = re.compile(
    r"<{7}\s*(REPLACE|DELETE|ADD)\s*\n(.*?)\n>{7}\s*\1\s*",
    re.DOTALL,
)
_SEP_RE = re.compile(r"\n={7}\n")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$", re.MULTILINE)




WORLD_ALLOWED_SECTIONS = ("Geography", "Culture & Customs", "Relevant History")
_WORLD_SECTION_ALIASES = {
    "geography": "Geography",
    "geography & key locations": "Geography",
    "locations": "Geography",
    "key locations": "Geography",
    "climate": "Geography",
    "terrain": "Geography",
    "culture": "Culture & Customs",
    "culture & customs": "Culture & Customs",
    "political & social structure": "Culture & Customs",
    "factions": "Culture & Customs",
    "economies": "Culture & Customs",
    "languages": "Culture & Customs",
    "social structure": "Culture & Customs",
    "history": "Relevant History",
    "history relevant to the plot": "Relevant History",
    "relevant history": "Relevant History",
    "time period & technology": "Relevant History",
}
_WORLD_DISALLOWED_SECTIONS = {
    "magic",
    "magic / power systems",
    "power systems",
    "technology",
    "technology & time period",
}


def _is_world_document(document: str) -> bool:
    return bool(re.search(r"^#\s+World\s*$", document or "", re.MULTILINE | re.IGNORECASE))


def _canonical_world_section(title: str) -> Optional[str]:
    normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
    if normalized in _WORLD_DISALLOWED_SECTIONS:
        return None
    if normalized in _WORLD_SECTION_ALIASES:
        return _WORLD_SECTION_ALIASES[normalized]
    if normalized in {s.lower() for s in WORLD_ALLOWED_SECTIONS}:
        return next(s for s in WORLD_ALLOWED_SECTIONS if s.lower() == normalized)
    return None


def _merge_world_sections(document: str) -> str:
    """Normalize a World document to the three canonical sections.

    Existing legacy sections that map cleanly are merged into the canonical
    bucket. Explicit magic/technology sections are intentionally dropped from
    the World document because that information is outside the requested
    three-part world reference. Unknown headings are also dropped rather than
    creating a fourth category.
    """
    if not _is_world_document(document):
        return document

    headings = _headings(document)
    if not headings:
        return document.strip() + "\n"

    buckets: dict[str, list[str]] = {name: [] for name in WORLD_ALLOWED_SECTIONS}

    for i, (start, level, title) in enumerate(headings):
        if level != 2:
            continue
        end = len(document)
        for start2, level2, _ in headings[i + 1:]:
            if level2 <= level:
                end = start2
                break
        body = document[start:end]
        body = re.sub(r"^##\s*.+?\s*\n", "", body, count=1).strip()
        if not body:
            continue
        canonical = _canonical_world_section(title)
        if canonical:
            buckets[canonical].append(body)

    lines = ["# World", ""]
    for name in WORLD_ALLOWED_SECTIONS:
        lines.append(f"## {name}")
        if buckets[name]:
            merged = "\n".join(x.strip() for x in buckets[name] if x.strip())
            lines.append(merged)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"




def parse_patches(text: str) -> tuple[list[Patch], list[str]]:
    """
    Parse all patch blocks out of raw model output.

    Text outside the blocks (stray commentary) is ignored rather than
    treated as an error — only malformed blocks generate a warning.
    Returns (patches, warnings).
    """
    if not text:
        return [], ["Empty model response."]

    patches: list[Patch] = []
    warnings: list[str] = []

    for m in _BLOCK_RE.finditer(text):
        op_name, body = m.group(1), m.group(2)
        raw_block = m.group(0)
        op = PatchOp(op_name)

        if op == PatchOp.DELETE:
            search = body.strip("\n")
            if not search.strip():
                warnings.append("Empty DELETE block skipped.")
                continue
            patches.append(Patch(op=op, search=search, raw_block=raw_block))
            continue

        parts = _SEP_RE.split(body, maxsplit=1)
        if len(parts) != 2:
            warnings.append(f"Malformed {op_name} block (missing '=======' separator) skipped.")
            continue
        left, right = parts[0].strip("\n"), parts[1].strip("\n")

        if op == PatchOp.REPLACE:
            if not left.strip():
                warnings.append("Empty SEARCH in REPLACE block skipped.")
                continue
            patches.append(Patch(op=op, search=left, replace=right, raw_block=raw_block))
        elif op == PatchOp.ADD:
            section = ""
            for line in left.splitlines():
                if line.strip().upper().startswith("SECTION:"):
                    section = line.split(":", 1)[1].strip()
                    break
            patches.append(Patch(op=op, section=section, replace=right, raw_block=raw_block))

    if not patches:
        warnings.append("No valid patch blocks found in model output.")

    return patches, warnings




def _headings(document: str) -> list[tuple[int, int, str]]:
    """[(start_offset, heading_level, title), ...] in document order."""
    return [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _HEADING_RE.finditer(document)
    ]


def _find_section_bounds(document: str, section_name: str) -> Optional[tuple[int, int, int]]:
    """
    Locate a Markdown section by heading title (case-insensitive, any
    level). Returns (heading_start, section_end, level), where
    section_end is just before the next heading of equal-or-higher
    level (or end of document). None if no heading matches.
    """
    headings = _headings(document)
    target = section_name.strip().lower()
    for i, (start, level, title) in enumerate(headings):
        if title.lower() == target:
            end = len(document)
            for start2, level2, _ in headings[i + 1:]:
                if level2 <= level:
                    end = start2
                    break
            return start, end, level
    return None


def find_relevant_section(document: str, query: str, min_overlap: int = 1) -> Optional[str]:
    """Find the Markdown section most relevant to `query`.

    For the World document, return the FULL canonical three-section world
    reference. This is deliberate: duplicate prevention needs visibility of
    all existing world facts, not just the keyword-matched section.
    """
    if _is_world_document(document):
        canonical = _merge_world_sections(document)
        parts = []
        for name in WORLD_ALLOWED_SECTIONS:
            bounds = _find_section_bounds(canonical, name)
            if bounds:
                start, end, _ = bounds
                parts.append(canonical[start:end].strip())
        return "\n\n".join(parts).strip() or None

    if not document.strip():
        return None
    headings = _headings(document)
    if not headings:
        return None

    query_words = set(re.findall(r"[a-zA-ZÀ-ÿ]{4,}", query.lower()))
    if not query_words:
        return None

    best_body: Optional[str] = None
    best_score = 0
    for i, (start, level, _title) in enumerate(headings):
        end = len(document)
        for start2, level2, _ in headings[i + 1:]:
            if level2 <= level:
                end = start2
                break
        body = document[start:end]
        body_words = set(re.findall(r"[a-zA-ZÀ-ÿ]{4,}", body.lower()))
        score = len(query_words & body_words)
        if score > best_score:
            best_score = score
            best_body = body

    if best_body is not None and best_score >= min_overlap:
        return best_body.strip()
    return None


def ensure_sections(document: str, section_names: list[str]) -> str:
    """Ensure the document has its required Markdown sections.

    World documents always use exactly the three canonical sections, regardless
    of legacy callers passing an older five-section list.
    """
    doc = document if document and document.strip() else ""
    if not re.search(r"^#\s+\S", doc, re.MULTILINE):
        doc = ("# World\n\n" + doc.strip() + "\n") if doc.strip() else "# World\n"

    if _is_world_document(doc):
        return _merge_world_sections(doc)

    existing_titles = {
        m.group(1).strip().lower()
        for m in re.finditer(r"^##\s*(.+?)\s*$", doc, re.MULTILINE)
    }
    additions = [
        f"\n## {name}\n" for name in section_names
        if name.strip().lower() not in existing_titles
    ]
    if additions:
        doc = doc.rstrip() + "\n" + "\n".join(additions) + "\n"
    return doc


def split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """
    Split a Markdown document into (heading_title, section_text) tuples.
    """
    if not text or not text.strip():
        return []
    headings = _headings(text)
    if not headings:
        return [("", text.strip())]
    sections: list[tuple[str, str]] = []
    if headings[0][0] > 0:
        pre = text[: headings[0][0]].strip()
        if pre:
            sections.append(("", pre))
    for i, (start, _level, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        sections.append((title, text[start:end].strip()))
    return sections


def merge_markdown_document(existing: str, new_text: str, default_sections: list[str]) -> str:
    """Merge generated Markdown into the World document without adding rogue sections.

    For the World document, only the three canonical sections are accepted.
    Unsupported headings are ignored and content is merged by canonical bucket.
    """
    doc = ensure_sections(existing, default_sections)



    if _is_world_document(new_text):
        normalized_new = _merge_world_sections(new_text)
    else:
        normalized_new = new_text

    if _is_world_document(doc):

        for title, body in split_markdown_sections(normalized_new):
            canonical = _canonical_world_section(title)
            if not canonical:
                continue
            body_content = re.sub(r"^#{1,6}\s*.+?\s*\n", "", body, count=1).strip()
            if not body_content:
                continue
            bounds = _find_section_bounds(doc, canonical)
            if not bounds:
                continue
            start, end, _level = bounds
            existing_body = doc[start:end]


            additions = [line.strip() for line in body_content.splitlines() if line.strip()]
            new_lines = []
            existing_norm = {re.sub(r"\s+", " ", line).strip().lower() for line in existing_body.splitlines() if line.strip()}
            for line in additions:
                norm = re.sub(r"\s+", " ", line).strip().lower()
                if norm and norm not in existing_norm:
                    new_lines.append(line)
                    existing_norm.add(norm)
            if not new_lines:
                continue
            merged_section = existing_body.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
            doc = doc[:start] + merged_section + doc[end:]
        return _merge_world_sections(doc)

    for title, body in split_markdown_sections(normalized_new):
        if not title:
            doc = doc.rstrip() + "\n\n" + body.strip() + "\n"
            continue
        body_content = re.sub(r"^#{1,6}\s*.+?\s*\n", "", body, count=1).strip()
        if not body_content:
            continue
        bounds = _find_section_bounds(doc, title)
        if bounds:
            start, end, _level = bounds
            merged_section = doc[start:end].rstrip("\n") + "\n" + body_content + "\n"
            doc = doc[:start] + merged_section + doc[end:]
        else:
            doc = doc.rstrip() + f"\n\n## {title}\n{body_content}\n"
    return doc.strip() + "\n"




def _count_occurrences(document: str, search: str) -> int:
    if not search:
        return 0
    return document.count(search)


def validate_patches(document: str, patches: list[Patch]) -> list[PatchError]:
    """Check every patch against `document` WITHOUT applying anything."""
    errors: list[PatchError] = []
    for idx, p in enumerate(patches):
        if p.op in (PatchOp.REPLACE, PatchOp.DELETE):
            count = _count_occurrences(document, p.search)
            if count == 0:
                errors.append(PatchError(
                    index=idx,
                    reason=(
                        "SEARCH text not found in the document. It must match "
                        "existing text EXACTLY, including whitespace and "
                        "punctuation — re-read the current content and try again."
                    ),
                    raw_block=p.raw_block,
                ))
            elif count > 1:
                errors.append(PatchError(
                    index=idx,
                    reason=(
                        f"SEARCH text matches {count} places in the document — "
                        "ambiguous. Include more surrounding context in SEARCH "
                        "so it matches exactly one location."
                    ),
                    raw_block=p.raw_block,
                ))
        elif p.op == PatchOp.ADD and _is_world_document(document):
            canonical = _canonical_world_section(p.section)
            if canonical is None:
                errors.append(PatchError(
                    index=idx,
                    reason=(
                        "World & Setting only allows these sections: "
                        "Geography, Culture & Customs, Relevant History. "
                        "Do not create Magic, Kingdoms, Factions, Technology, "
                        "or any other section."
                    ),
                    raw_block=p.raw_block,
                ))
    return errors


def apply_patches(document: str, patches: list[Patch], strict: bool = True) -> PatchResult:
    """Validate then apply `patches` to `document` atomically."""
    if not patches:
        return PatchResult(success=False, document=document, errors=[
            PatchError(index=-1, reason="No patches to apply.")
        ])

    errors = validate_patches(document, patches)
    if errors and strict:
        return PatchResult(success=False, document=document, errors=errors)

    failing_indices = {e.index for e in errors}
    working = document
    applied = 0

    for idx, p in enumerate(patches):
        if idx in failing_indices:
            continue
        try:
            if p.op == PatchOp.REPLACE:
                working = working.replace(p.search, p.replace, 1)
                applied += 1
            elif p.op == PatchOp.DELETE:
                working = working.replace(p.search, "", 1)
                applied += 1
            elif p.op == PatchOp.ADD:
                if p.section:
                    section = p.section
                    if _is_world_document(working):
                        section = _canonical_world_section(section) or section
                        if section not in WORLD_ALLOWED_SECTIONS:
                            continue
                    bounds = _find_section_bounds(working, section)
                    if bounds:
                        start, end, _level = bounds
                        section_text = working[start:end]
                        candidate_lines = [
                            line.strip()
                            for line in p.replace.strip("\n").splitlines()
                            if line.strip()
                        ]
                        existing_norm = {
                            re.sub(r"\s+", " ", line).strip().lower()
                            for line in section_text.splitlines()
                            if line.strip()
                        }
                        unique_lines = []
                        for line in candidate_lines:
                            norm = re.sub(r"\s+", " ", line).strip().lower()
                            if norm and norm not in existing_norm:
                                unique_lines.append(line)
                                existing_norm.add(norm)
                        if not unique_lines:
                            continue
                        insertion = section_text.rstrip("\n") + "\n" + "\n".join(unique_lines) + "\n"
                        working = working[:start] + insertion + working[end:]
                    else:

                        if _is_world_document(working):
                            continue
                        working = working.rstrip() + f"\n\n## {section}\n{p.replace.strip()}\n"
                else:
                    working = working.rstrip() + "\n\n" + p.replace.strip() + "\n"
                applied += 1
        except Exception as e:
            errors.append(PatchError(index=idx, reason=f"Failed to apply: {e}", raw_block=p.raw_block))

    if errors:
        return PatchResult(success=False, document=document, applied=0, errors=errors)

    if _is_world_document(working):
        working = _merge_world_sections(working)

    return PatchResult(success=True, document=working, applied=applied)


def format_errors_for_retry(errors: list[PatchError]) -> str:
    """Turn PatchErrors into a compact message to feed back to the model for a retry."""
    lines = ["Your previous patch could not be applied:"]
    for e in errors:
        label = f"Patch #{e.index + 1}" if e.index >= 0 else "Batch"
        lines.append(f"- {label}: {e.reason}")
    lines.append(
        "\nRe-emit ONLY the corrected patch block(s) in the same SEARCH/REPLACE "
        "/ DELETE / ADD format. Do not restate unaffected content, and do not "
        "output the full document."
    )
    return "\n".join(lines)