"""Deterministic character identity matching.

The character extractor is allowed to propose character records, but this
module is the final authority on whether a proposed record is a new
character or another mention of an existing one.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from engine.models import Character


_NAME_STOPWORDS = {
    "the", "a", "an", "mr", "mrs", "ms", "miss", "dr", "sir", "lady",
    "lord", "captain", "capt", "commander", "general", "professor", "prof",
}


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize(value).split()
        if token and token not in _NAME_STOPWORDS
    }


def _text_tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize(value).split()
        if len(token) >= 3
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _similarity(left: str, right: str) -> float:
    a = _normalize(left)
    b = _normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def character_match_score(candidate: dict, existing: Character) -> float:
    """Return a conservative identity score in [0, 1]."""
    candidate_name = str(candidate.get("name", "")).strip()
    if not candidate_name or not existing.name.strip():
        return 0.0

    candidate_name_norm = _normalize(candidate_name)
    existing_name_norm = _normalize(existing.name)
    if candidate_name_norm == existing_name_norm:
        return 1.0

    candidate_tokens = _name_tokens(candidate_name)
    existing_tokens = _name_tokens(existing.name)
    name_similarity = _similarity(candidate_name, existing.name)
    shared_name = _jaccard(candidate_tokens, existing_tokens)

    candidate_role = _normalize(str(candidate.get("role", "")))
    existing_role = _normalize(existing.role)
    same_role = bool(candidate_role and existing_role and candidate_role == existing_role)

    candidate_description = str(candidate.get("description", ""))
    candidate_backstory = str(candidate.get("backstory", ""))
    candidate_traits = " ".join(str(t) for t in candidate.get("traits", []) or [])
    candidate_text = " ".join((candidate_description, candidate_backstory, candidate_traits))
    existing_text = " ".join((existing.description, existing.backstory, " ".join(existing.traits)))
    text_similarity = _jaccard(_text_tokens(candidate_text), _text_tokens(existing_text))




    shorter, longer = sorted((candidate_tokens, existing_tokens), key=len)
    contained_name = bool(shorter) and shorter.issubset(longer)
    if contained_name and same_role and (name_similarity >= 0.45 or text_similarity >= 0.12):
        return 0.88



    if name_similarity >= 0.88 and (same_role or text_similarity >= 0.15):
        return 0.90




    if same_role and text_similarity >= 0.55 and (shared_name >= 0.25 or name_similarity >= 0.35):
        return 0.82

    return 0.0


def find_existing_character(candidate: dict, characters: list[Character]) -> tuple[Character | None, float]:
    """Find the strongest existing identity match for a candidate record."""
    best: Character | None = None
    best_score = 0.0
    for existing in characters:
        score = character_match_score(candidate, existing)
        if score > best_score:
            best = existing
            best_score = score
    return (best, best_score) if best_score >= 0.82 else (None, 0.0)


def merge_nonempty_fields(existing: Character, candidate: dict) -> None:
    """Enrich an existing record without replacing established information."""
    description = str(candidate.get("description", "") or "").strip()
    backstory = str(candidate.get("backstory", "") or "").strip()
    if description and not existing.description.strip():
        existing.description = description
    if backstory and not existing.backstory.strip():
        existing.backstory = backstory

    traits_raw = candidate.get("traits", [])
    if isinstance(traits_raw, str):
        traits = [t.strip() for t in traits_raw.split(",") if t.strip()]
    elif isinstance(traits_raw, list):
        traits = [str(t).strip() for t in traits_raw if str(t).strip()]
    else:
        traits = []
    if traits:
        existing_lower = {trait.lower() for trait in existing.traits}
        for trait in traits:
            if trait.lower() not in existing_lower:
                existing.traits.append(trait)
                existing_lower.add(trait.lower())
