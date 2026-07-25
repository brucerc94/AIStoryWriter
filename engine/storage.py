"""
Storage layer for AI Story Studio.
All data is stored as Markdown (.md) and JSON (.json) files.
No database. No SQLite. No cloud.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from engine.models import (
    AppSettings,
    Character,
    Chapter,
    ChatMessage,
    Project,
)

logger = logging.getLogger("storage")

DATA_DIR = Path(__file__).parent.parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def project_dir(project_id: str) -> Path:
    return DATA_DIR / project_id


def characters_dir(project_id: str) -> Path:
    return project_dir(project_id) / "characters"


def chapters_dir(project_id: str) -> Path:
    return project_dir(project_id) / "chapters"


# ──────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────

def load_settings() -> AppSettings:
    ensure_data_dir()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return AppSettings.from_dict(data)
        except Exception:
            pass
    return AppSettings()


def save_settings(settings: AppSettings) -> None:
    ensure_data_dir()
    SETTINGS_FILE.write_text(
        json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ──────────────────────────────────────────────
# Projects index
# ──────────────────────────────────────────────

def list_project_ids() -> list[str]:
    ensure_data_dir()
    ids = []
    for entry in DATA_DIR.iterdir():
        if entry.is_dir() and (entry / "project.json").exists():
            ids.append(entry.name)
    return ids


def load_all_projects() -> list[Project]:
    projects = []
    for pid in list_project_ids():
        p = load_project(pid)
        if p:
            projects.append(p)
    projects.sort(key=lambda p: p.updated_at, reverse=True)
    return projects


# ──────────────────────────────────────────────
# Single project
# ──────────────────────────────────────────────

def load_project(project_id: str) -> Optional[Project]:
    pdir = project_dir(project_id)
    project_file = pdir / "project.json"
    if not project_file.exists():
        return None

    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
        project = Project.from_dict(data)

        # Load markdown blobs
        project.synopsis = _read_md(pdir / "synopsis.md")
        project.outline = _read_md(pdir / "outline.md")
        project.world = _read_md(pdir / "world.md")
        project.memory = _read_md(pdir / "memory.md")

        # Load chat
        chat_file = pdir / "chat.json"
        if chat_file.exists():
            chat_data = json.loads(chat_file.read_text(encoding="utf-8"))
            project.chat_messages = [ChatMessage.from_dict(m) for m in chat_data]

        # Load characters
        cdir = characters_dir(project_id)
        project.characters = []
        if cdir.exists():
            for f in sorted(cdir.glob("*.json")):
                try:
                    c = Character.from_dict(json.loads(f.read_text(encoding="utf-8")))
                    project.characters.append(c)
                except Exception:
                    pass

        # Load chapters
        chdir = chapters_dir(project_id)
        project.chapters = []
        if chdir.exists():
            chapter_files = sorted(chdir.glob("chapter_*.json"), key=_chapter_sort_key)
            for f in chapter_files:
                try:
                    ch_data = json.loads(f.read_text(encoding="utf-8"))
                    ch = Chapter.from_dict(ch_data)
                    # Load chapter content from markdown
                    md_path = chdir / f"chapter_{ch.number:03d}.md"
                    if md_path.exists():
                        ch.content = md_path.read_text(encoding="utf-8")
                    project.chapters.append(ch)
                except Exception:
                    pass

        logger.info(f"Loaded project '{project.title}' ({project_id}), "
                    f"{len(project.chapters)} chapter(s), {len(project.chat_messages)} chat message(s).")
        return project
    except Exception as e:
        logger.error(f"Error loading project {project_id}: {e}")
        return None


def save_project(project: Project) -> None:
    from datetime import datetime

    pdir = project_dir(project.id)
    pdir.mkdir(parents=True, exist_ok=True)
    characters_dir(project.id).mkdir(exist_ok=True)
    chapters_dir(project.id).mkdir(exist_ok=True)

    project.updated_at = datetime.now().isoformat()

    # Save main JSON (no heavy content)
    project_file = pdir / "project.json"
    project_file.write_text(
        json.dumps(project.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Save markdown blobs
    _write_md(pdir / "synopsis.md", project.synopsis)
    _write_md(pdir / "outline.md", project.outline)
    _write_md(pdir / "world.md", project.world)
    _write_md(pdir / "memory.md", project.memory)

    # Save story.md — full concatenated story for human reading
    _write_story_md(project)

    # Save chat
    chat_file = pdir / "chat.json"
    chat_data = [m.to_dict() for m in project.chat_messages]
    chat_file.write_text(
        json.dumps(chat_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Save characters
    cdir = characters_dir(project.id)
    # Remove old files not in current list
    existing_ids = {c.id for c in project.characters}
    for f in cdir.glob("*.json"):
        if f.stem not in existing_ids:
            f.unlink(missing_ok=True)
    for char in project.characters:
        f = cdir / f"{char.id}.json"
        f.write_text(json.dumps(char.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    # Save chapters
    chdir = chapters_dir(project.id)
    existing_nums = {ch.number for ch in project.chapters}
    # Remove orphan chapter files
    for f in chdir.glob("chapter_*.json"):
        try:
            num = int(f.stem.split("_")[1])
            if num not in existing_nums:
                f.unlink(missing_ok=True)
                md_f = chdir / f"chapter_{num:03d}.md"
                md_f.unlink(missing_ok=True)
        except Exception:
            pass

    for ch in project.chapters:
        meta = ch.to_dict()
        # Don't store content in JSON, only in MD
        content = meta.pop("content", "")
        f = chdir / f"chapter_{ch.number:03d}.json"
        f.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        md_f = chdir / f"chapter_{ch.number:03d}.md"
        md_f.write_text(content, encoding="utf-8")

    logger.info(f"Saved project '{project.title}' ({project.id}).")


def delete_project(project_id: str) -> None:
    pdir = project_dir(project_id)
    title = project_id
    project_file = pdir / "project.json"
    if project_file.exists():
        try:
            title = json.loads(project_file.read_text(encoding="utf-8")).get("title", project_id)
        except Exception:
            pass
    if pdir.exists():
        shutil.rmtree(pdir)
        logger.info(f"Deleted project '{title}' ({project_id}).")
    else:
        logger.warning(f"Delete requested for project {project_id}, but it doesn't exist on disk.")


def rename_project(project_id: str, new_title: str) -> None:
    pdir = project_dir(project_id)
    project_file = pdir / "project.json"
    if project_file.exists():
        data = json.loads(project_file.read_text(encoding="utf-8"))
        old_title = data.get("title", "")
        data["title"] = new_title
        project_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Renamed project ({project_id}): '{old_title}' -> '{new_title}'.")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _read_md(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _write_md(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _write_story_md(project: Project) -> None:
    """Write a human-readable full story file."""
    lines = [f"# {project.title}", ""]
    if project.synopsis:
        lines += ["## Synopsis", "", project.synopsis, ""]
    for ch in sorted(project.chapters, key=lambda c: c.number):
        lines += [f"## Chapter {ch.number}: {ch.title}", ""]
        if ch.content:
            lines.append(ch.content)
        lines.append("")
    pdir = project_dir(project.id)
    (pdir / "story.md").write_text("\n".join(lines), encoding="utf-8")


def _chapter_sort_key(path: Path) -> int:
    try:
        return int(path.stem.split("_")[1])
    except Exception:
        return 0


def list_gguf_models(directory: str) -> list[str]:
    """Return all .gguf files found recursively under directory."""
    if not directory or not os.path.isdir(directory):
        return []
    result = []
    for root, _, files in os.walk(directory):
        for f in files:
            if f.lower().endswith(".gguf"):
                result.append(os.path.join(root, f))
    return sorted(result)
