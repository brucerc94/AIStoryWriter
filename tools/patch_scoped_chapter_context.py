from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def replace_function(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:]


def patch_context() -> None:
    path = ROOT / "engine/context.py"
    text = path.read_text(encoding="utf-8")

    imports = text.split("from engine", 1)[0]
    if "import re\n" not in imports:
        text = text.replace("import logging\n", "import logging\nimport re\n", 1)
    if "import unicodedata\n" not in imports:
        text = text.replace("import logging\n", "import logging\nimport unicodedata\n", 1)

    if "def build_relevant_chapter_context(" not in text:
        helper = r'''

_CHAPTER_MATCH_STOPWORDS = {
    "about", "after", "again", "also", "before", "being", "between", "could", "from", "have",
    "into", "just", "more", "other", "over", "same", "some", "than", "that", "their", "there",
    "these", "they", "this", "those", "through", "under", "very", "what", "when", "where", "which",
    "while", "with", "would", "chapter", "scene", "story", "plan", "must", "should", "then", "only",
    "para", "como", "desde", "esta", "este", "estas", "estos", "entre", "sobre", "hacia", "donde",
    "cuando", "quien", "quienes", "tambien", "todo", "todos", "toda", "todas", "debe", "deben", "con",
    "por", "del", "las", "los", "una", "uno", "unas", "unos", "que", "sus", "ser", "sea", "son",
}


def _normalize_match_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()


def _meaningful_words(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]{4,}", _normalize_match_text(text))
        if word not in _CHAPTER_MATCH_STOPWORDS
    }


def select_relevant_characters(
    characters: list,
    source_text: str,
    max_characters: int = 12,
    max_chars: int = 5000,
) -> str:
    """Return only established character records referenced by chapter context."""
    source_norm = " " + _normalize_match_text(source_text) + " "
    selected = []
    for character in characters:
        name = (getattr(character, "name", "") or "").strip()
        if not name:
            continue
        name_norm = _normalize_match_text(name)
        if not name_norm:
            continue
        tokens = name_norm.split()
        matched = f" {name_norm} " in source_norm
        if not matched and len(tokens) == 1:
            matched = f" {tokens[0]} " in source_norm
        if not matched and len(tokens) > 1:
            matched = all(f" {token} " in source_norm for token in tokens)
        if matched:
            selected.append(character)
        if len(selected) >= max_characters:
            break
    if not selected:
        return ""
    return _truncate_to_budget(format_characters_block(selected), max(1, max_chars // 4))


def _markdown_world_sections(world: str) -> list[tuple[int, str, str]]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", world or ""))
    sections = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(world)
        sections.append((index, match.group(1).strip(), world[start:end].strip()))
    return sections


def select_relevant_world(
    world: str,
    source_text: str,
    max_sections: int = 4,
    max_chars: int = 6000,
) -> str:
    """Return the highest-scoring world sections referenced by this chapter context."""
    sections = _markdown_world_sections(world)
    if not sections:
        return ""
    source_words = _meaningful_words(source_text)
    scored = []
    for index, title, body in sections:
        score = (
            len(source_words & _meaningful_words(title)) * 5
            + len(source_words & _meaningful_words(body))
        )
        if score > 0:
            scored.append((score, index, body))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = sorted(scored[:max_sections], key=lambda item: item[1])
    blocks = []
    remaining = max_chars
    for _score, _index, body in chosen:
        if remaining <= 0:
            break
        block = body if len(body) <= remaining else body[:remaining].rstrip() + "\n\n[... world section truncated ...]"
        blocks.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(blocks).strip()


def build_relevant_chapter_context(
    project,
    source_text: str,
    max_character_chars: int = 5000,
    max_world_chars: int = 6000,
) -> tuple[str, str]:
    """Resolve scoped chapter canon without an LLM call."""
    return (
        select_relevant_characters(project.characters, source_text, max_chars=max_character_chars),
        select_relevant_world(project.world, source_text, max_chars=max_world_chars),
    )
'''
        anchor = "def _estimate_tokens(text: str) -> int:\n"
        text = text.replace(anchor, helper + "\n\n" + anchor, 1)

    context_fn = '''def _build_story_context_text(\n    project: Project,\n    task: TaskType,\n    system_prompt: str,\n    user_message: str,\n    max_context_tokens: int,\n    include_story_context: bool,\n) -> str:\n    core_system, custom_instructions = _split_custom_instructions(system_prompt)\n    parts = [core_system]\n    if include_story_context:\n        sections = _compact_sections(\n            _section_payloads(project, task, system_prompt, user_message),\n            max_context_tokens=max_context_tokens,\n            task=task,\n            project=project,\n        )\n        if task == TaskType.GENERATE_OUTLINE:\n            if sections["Characters"]:\n                parts.append(f"\\n\\n## Established Characters\\n{sections['Characters']}")\n            if sections["World"]:\n                parts.append(f"\\n\\n## World & Setting\\n{sections['World']}")\n            if sections["Creative Direction"]:\n                parts.append(f"\\n\\n## Author's Creative Direction\\n{sections['Creative Direction']}")\n        elif task in (TaskType.WRITE_CHAPTER, TaskType.CHANGE_CHAPTER):\n            # These workflows inject their own chapter-scoped canon in their\n            # explicit task prompts. Never inject project-wide synopsis, canon,\n            # Memory, UI Chat Summary, or chat history here.\n            pass\n        else:\n            if sections["Synopsis"]:\n                parts.append(f"\\n\\n{sections['Synopsis']}")\n            if sections["Characters"]:\n                parts.append(f"\\n\\n## Established Characters\\n{sections['Characters']}")\n            if sections["Outline"]:\n                parts.append(f"\\n\\n## Outline\\n{sections['Outline']}")\n            if sections["World"]:\n                parts.append(f"\\n\\n## World & Setting\\n{sections['World']}")\n            if sections["Memory"]:\n                parts.append(f"\\n\\n## Story Memory\\n{sections['Memory']}")\n            if sections["Chat Summary"]:\n                parts.append(f"\\n\\n## Conversation Summary (older messages)\\n{sections['Chat Summary']}")\n            if sections["Creative Direction"]:\n                parts.append(f"\\n\\n## Author's Creative Direction\\n{sections['Creative Direction']}")\n\n    if custom_instructions:\n        parts.append(f"\\n\\n{CUSTOM_INSTRUCTIONS_MARKER}\\n{custom_instructions}")\n    return "\\n".join(p for p in parts if p).strip()\n'''
    text = replace_function(text, "def _build_story_context_text(", "def _repair_alternation(", context_fn)
    path.write_text(text, encoding="utf-8")


def patch_workflow() -> None:
    path = ROOT / "engine/workflow.py"
    text = path.read_text(encoding="utf-8")
    if "    build_relevant_chapter_context,\n" not in text:
        text = text.replace("    budget_allocate,\n", "    budget_allocate,\n    build_relevant_chapter_context,\n", 1)

    generation_fn = '''    def _build_chapter_generation_prompt(self, chapter_num: int, outline_entry: str, checklist: str) -> str:\n        sections = []\n\n        prev_tail = ""\n        if chapter_num > 1:\n            prev = next((c for c in self.project.chapters if c.number == chapter_num - 1), None)\n            if prev and prev.content:\n                prev_tail = prev.content[-800:].strip()\n\n        selection_source = "\\n\\n".join(\n            part for part in (outline_entry, checklist, prev_tail, self.extra_input.strip()) if part\n        )\n        relevant_characters, relevant_world = build_relevant_chapter_context(\n            self.project, selection_source, max_character_chars=5000, max_world_chars=6000\n        )\n\n        if outline_entry:\n            sections.append(prompts.render(\n                "change_chapter/section", heading="CHAPTER OUTLINE (binding)", body=outline_entry,\n            ))\n        if checklist:\n            sections.append(prompts.render(\n                "change_chapter/section",\n                heading="INTERNAL REQUIREMENTS CHECKLIST (write ordinary prose — never expose this list to the reader)",\n                body=checklist,\n            ))\n        if relevant_characters:\n            sections.append(prompts.render(\n                "change_chapter/section",\n                heading="RELEVANT CHARACTERS (project canon — use only these established records)",\n                body=relevant_characters,\n            ))\n        if relevant_world:\n            sections.append(prompts.render(\n                "change_chapter/section",\n                heading="RELEVANT WORLD & SETTING (project canon — scoped to this chapter)",\n                body=relevant_world,\n            ))\n        if prev_tail:\n            sections.append(prompts.render(\n                "change_chapter/section", heading="END OF PREVIOUS CHAPTER (continuity only)", body=prev_tail,\n            ))\n\n        style_frag = self.project.writing_style.to_prompt_fragment()\n        if style_frag:\n            sections.append(prompts.render("change_chapter/section", heading="STYLE TO APPLY", body=style_frag))\n\n        intent = self.project.author_intent\n        intent_lines = []\n        if intent.emotional_journey:\n            intent_lines.append(f"Emotional tone to sustain in this chapter: {intent.emotional_journey}")\n        if intent.avoid:\n            intent_lines.append(f"Avoid entirely: {intent.avoid}")\n        if intent_lines:\n            sections.append(prompts.render("change_chapter/section", heading="AUTHOR INTENT", body="\\n".join(intent_lines)))\n\n        if self.extra_input and self.extra_input.strip():\n            sections.append(prompts.render("change_chapter/section", heading="AUTHOR'S REQUEST", body=self.extra_input.strip()))\n\n        return prompts.render(\n            "write_chapter/generation_user",\n            chapter_number=chapter_num,\n            title=self.project.title,\n            sections="\\n\\n".join(sections),\n        )\n'''
    text = replace_function(text, "    def _build_chapter_generation_prompt(", "    def _run_write_chapter", generation_fn)

    start = text.index("    def _build_chapter_continuation_prompt(")
    end = text.index("\n    def _parse_completion_evaluation", start)
    cont = text[start:end]
    cont = re.sub(
        r'^\s*chars_raw\s*=.*\n\s*world_raw\s*=.*\n',
        '',
        cont,
        count=1,
        flags=re.MULTILINE,
    )
    marker = '        prose_tail_raw = chapter_text.strip()\n'
    if marker not in cont:
        raise SystemExit("Write continuation prose marker not found")
    insert = '''        prose_tail_raw = chapter_text.strip()\n        selection_source = "\\n\\n".join(\n            part for part in (outline_raw, chapter_goal, checklist, prev_tail_raw, prose_tail_raw) if part\n        )\n        chars_raw, world_raw = build_relevant_chapter_context(\n            self.project, selection_source, max_character_chars=5000, max_world_chars=6000\n        )\n'''
    cont = cont.replace(marker, insert, 1)
    text = text[:start] + cont + text[end:]
    path.write_text(text, encoding="utf-8")


def patch_change_chapter() -> None:
    path = ROOT / "engine/change_chapter.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from engine.context import budget_allocate, extract_outline_section, format_characters_block\n",
        "from engine.context import budget_allocate, build_relevant_chapter_context, extract_outline_section\n",
        1,
    )

    clean_fn = '''def build_clean_context_sections(worker, chapter_num: int, selection_source: str = "") -> str:\n    """Assemble scoped canon context for the target chapter only."""\n    project = worker.project\n    chapter_outline = extract_outline_section(project.outline, chapter_num) or "(no outline entry for this chapter)"\n    source = "\\n\\n".join(part for part in (chapter_outline, selection_source) if part)\n    characters, world = build_relevant_chapter_context(\n        project, source, max_character_chars=5000, max_world_chars=6000\n    )\n\n    sections = []\n    if characters:\n        sections.append(prompts.render(\n            "change_chapter/section",\n            heading="RELEVANT CHARACTERS (project canon — use only these established records)",\n            body=characters,\n        ))\n    if world:\n        sections.append(prompts.render(\n            "change_chapter/section",\n            heading="RELEVANT WORLD & SETTING (project canon — scoped to this chapter)",\n            body=world,\n        ))\n    intent_frag = project.author_intent.to_prompt_fragment()\n    style_frag = project.writing_style.to_prompt_fragment()\n    if intent_frag:\n        sections.append(prompts.render("change_chapter/section", heading="AUTHOR INTENT", body=intent_frag))\n    if style_frag:\n        sections.append(prompts.render("change_chapter/section", heading="WRITING STYLE", body=style_frag))\n    sections.append(prompts.render("change_chapter/section", heading="CURRENT CHAPTER PLAN", body=chapter_outline))\n    return "\\n\\n".join(sections)\n'''
    text = replace_function(text, "def build_clean_context_sections(", "def build_full_rewrite_prompt", clean_fn)

    old = '        context_sections=build_clean_context_sections(worker, chapter_num),\n'
    new = '        context_sections=build_clean_context_sections(worker, chapter_num, "\\n\\n".join((instruction, checklist, chapter_content))),\n'
    if old not in text:
        raise SystemExit("Change full rewrite context call not found")
    text = text.replace(old, new, 1)

    start = text.index("def _build_continuation_prompt(")
    end = text.index("\ndef trim_leading_overlap", start)
    ccont = text[start:end]
    ccont = re.sub(
        r'^\s*characters_raw\s*=.*\n\s*world_raw\s*=.*\n',
        '',
        ccont,
        count=1,
        flags=re.MULTILINE,
    )
    marker = '    prose_tail_raw = chapter_content.strip()\n'
    if marker not in ccont:
        raise SystemExit("Change continuation prose marker not found")
    insert = '''    prose_tail_raw = chapter_content.strip()\n    selection_source = "\\n\\n".join(\n        part for part in (outline_raw, instruction, checklist, missing_text, prose_tail_raw) if part\n    )\n    characters_raw, world_raw = build_relevant_chapter_context(\n        project, selection_source, max_character_chars=5000, max_world_chars=6000\n    )\n'''
    ccont = ccont.replace(marker, insert, 1)
    text = text[:start] + ccont + text[end:]
    path.write_text(text, encoding="utf-8")


def patch_prompt() -> None:
    path = ROOT / "engine/prompts/write_chapter/continuation_user.txt"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "established characters and relationships, world/setting, story memory, or the text already written.",
        "established characters and relationships, scoped world/setting, or the text already written.",
    )
    path.write_text(text, encoding="utf-8")


def write_tests() -> None:
    path = ROOT / "tests/test_scoped_chapter_context.py"
    path.write_text(r'''import unittest
from types import SimpleNamespace

from engine.context import build_context_for_model, select_relevant_characters, select_relevant_world
from engine.models import Project, TaskType


class ScopedChapterContextTests(unittest.TestCase):
    def character(self, name):
        return SimpleNamespace(
            name=name, role="supporting", description=f"Description for {name}",
            backstory="", traits=[], relationships=[]
        )

    def test_character_selection(self):
        chars = [self.character("Raul"), self.character("Isabel"), self.character("Manolo")]
        result = select_relevant_characters(chars, "Raul talks to Isabel in the daycare.")
        self.assertIn("Raul", result)
        self.assertIn("Isabel", result)
        self.assertNotIn("Manolo", result)

    def test_world_selection(self):
        world = """# World

## Geography
The baptismal font stands beside the maize fields and the daycare.

## Culture & Customs
Santa Clara holds the Harvest Moon Festival.

## Relevant History
The old church was founded by the first settlers.
"""
        result = select_relevant_world(world, "Raul searches the baptismal font beside the maize fields.", max_sections=2)
        self.assertIn("Geography", result)
        self.assertNotIn("Culture & Customs", result)

    def test_writer_context_has_no_ui_chat_or_memory(self):
        project = Project(title="Test")
        project.characters = [self.character("Raul")]
        project.world = "## Geography\nThe daycare stands beside maize fields."
        project.outline = "## Chapter 1: Test\nRaul enters the daycare."
        project.memory = "LATER CHAPTER EVENT MUST NOT LEAK"
        project.chat_summary = "UI CHAT HISTORY MUST NOT LEAK"
        project.chat_messages = []
        messages = build_context_for_model(
            project,
            "Write Chapter 1. CHAPTER OUTLINE (binding): Raul enters the daycare.",
            "SYSTEM",
            8192,
            task=TaskType.WRITE_CHAPTER,
            reply_reserved=4256,
        )
        self.assertNotIn("LATER CHAPTER EVENT MUST NOT LEAK", messages[0]["content"])
        self.assertNotIn("UI CHAT HISTORY MUST NOT LEAK", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")


def main() -> None:
    patch_context()
    patch_workflow()
    patch_change_chapter()
    patch_prompt()
    write_tests()

    for relative in ("engine/context.py", "engine/workflow.py", "engine/change_chapter.py"):
        path = ROOT / relative
        compile(path.read_text(encoding="utf-8"), relative, "exec")

    subprocess.run(["python", "tests/test_scoped_chapter_context.py"], cwd=ROOT, check=True)

    # Remove the temporary workflow and this patch script before creating the final commit.
    helper = ROOT / ".github/workflows/_scope_chapter_context.yml"
    script = ROOT / "tools/patch_scoped_chapter_context.py"
    if helper.exists():
        helper.unlink()
    if script.exists():
        script.unlink()

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run([
        "git", "add",
        "engine/context.py",
        "engine/workflow.py",
        "engine/change_chapter.py",
        "engine/prompts/write_chapter/continuation_user.txt",
        "tests/test_scoped_chapter_context.py",
        ".github/workflows/_scope_chapter_context.yml",
        "tools/patch_scoped_chapter_context.py",
    ], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", "refactor: scope chapter context to relevant canon"], cwd=ROOT, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:bugfixes"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
