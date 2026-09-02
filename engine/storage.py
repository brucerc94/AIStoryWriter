"""
Storage layer for AI Story Studio.
All data is stored as Markdown (.md) and JSON (.json) files — now
transparently encrypted at rest with AES-256-GCM (see
`engine/encryption.py`). No database. No SQLite. No cloud.

Encryption is fully transparent to every caller in this module and to
everything above it (ui/, engine/workflow.py, etc.): callers still read
and write plain Python strings via the helpers below, exactly as
before. `EncryptionService` (injected, not hardcoded — see
`_encryption_service()`) is the only thing that knows AES exists.

Legacy plaintext files (from before encryption was introduced) are
migrated automatically and transparently: `_read_bytes()` detects the
absence of the encryption header, reads the file as plaintext, and
immediately re-saves it encrypted so every subsequent read/write for
that file goes through AES from then on.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from engine.encryption import EncryptionService, get_default_encryption_service
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








def _encryption_service() -> EncryptionService:
    return get_default_encryption_service()



def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def project_dir(project_id: str) -> Path:
    return DATA_DIR / project_id


def characters_dir(project_id: str) -> Path:
    return project_dir(project_id) / "characters"


def chapters_dir(project_id: str) -> Path:
    return project_dir(project_id) / "chapters"










def _read_bytes(path: Path) -> Optional[bytes]:
    """
    Read a file and transparently decrypt it.

    If the file predates encryption (no magic header), it's treated as
    legacy plaintext: read as-is, then immediately re-written encrypted
    so the migration only has to happen once per file. Returns None if
    the file doesn't exist.
    """
    if not path.exists():
        return None
    raw = path.read_bytes()
    service = _encryption_service()
    if service.is_encrypted(raw):
        return service.decrypt(raw)


    logger.info(f"Migrating legacy unencrypted file to AES-256: '{path}'.")
    _write_bytes(path, raw)
    return raw


def _write_bytes(path: Path, data: bytes) -> None:
    """Encrypt `data` and write it to `path`."""
    service = _encryption_service()
    path.write_bytes(service.encrypt(data))


def _read_text(path: Path, default: str = "") -> str:
    raw = _read_bytes(path)
    if raw is None:
        return default
    return raw.decode("utf-8")


def _write_text_encrypted(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))






def load_settings() -> AppSettings:
    ensure_data_dir()
    text = _read_text(SETTINGS_FILE)
    if text:
        try:
            return AppSettings.from_dict(json.loads(text))
        except Exception:
            pass
    return AppSettings()


def save_settings(settings: AppSettings) -> None:
    ensure_data_dir()
    _write_text_encrypted(
        SETTINGS_FILE,
        json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
    )






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






def load_project(project_id: str) -> Optional[Project]:
    pdir = project_dir(project_id)
    project_file = pdir / "project.json"
    if not project_file.exists():
        return None

    try:
        data = json.loads(_read_text(project_file))
        project = Project.from_dict(data)


        project.synopsis = _read_text(pdir / "synopsis.md")
        project.outline = _read_text(pdir / "outline.md")
        project.world = _read_text(pdir / "world.md")
        project.memory = _read_text(pdir / "memory.md")


        chat_file = pdir / "chat.json"
        chat_text = _read_text(chat_file)
        if chat_text:
            chat_data = json.loads(chat_text)
            project.chat_messages = [ChatMessage.from_dict(m) for m in chat_data]


        cdir = characters_dir(project_id)
        project.characters = []
        if cdir.exists():
            for f in sorted(cdir.glob("*.json")):
                try:
                    c = Character.from_dict(json.loads(_read_text(f)))
                    project.characters.append(c)
                except Exception:
                    pass


        chdir = chapters_dir(project_id)
        project.chapters = []
        if chdir.exists():
            chapter_files = sorted(chdir.glob("chapter_*.json"), key=_chapter_sort_key)
            for f in chapter_files:
                try:
                    ch_data = json.loads(_read_text(f))
                    ch = Chapter.from_dict(ch_data)

                    md_path = chdir / f"chapter_{ch.number:03d}.md"
                    if md_path.exists():
                        ch.content = _read_text(md_path)
                    project.chapters.append(ch)
                except Exception:
                    pass

        logger.debug(f"Loaded project '{project.title}' ({project_id}), "
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


    project_file = pdir / "project.json"
    _write_text_encrypted(
        project_file,
        json.dumps(project.to_dict(), indent=2, ensure_ascii=False),
    )


    _write_md(pdir / "synopsis.md", project.synopsis)
    _write_md(pdir / "outline.md", project.outline)
    _write_md(pdir / "world.md", project.world)
    _write_md(pdir / "memory.md", project.memory)






    _write_story_md(project)


    chat_file = pdir / "chat.json"
    chat_data = [m.to_dict() for m in project.chat_messages]
    _write_text_encrypted(chat_file, json.dumps(chat_data, indent=2, ensure_ascii=False))


    cdir = characters_dir(project.id)

    existing_ids = {c.id for c in project.characters}
    for f in cdir.glob("*.json"):
        if f.stem not in existing_ids:
            f.unlink(missing_ok=True)
    for char in project.characters:
        f = cdir / f"{char.id}.json"
        _write_text_encrypted(f, json.dumps(char.to_dict(), indent=2, ensure_ascii=False))


    chdir = chapters_dir(project.id)
    existing_nums = {ch.number for ch in project.chapters}

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

        content = meta.pop("content", "")
        f = chdir / f"chapter_{ch.number:03d}.json"
        _write_text_encrypted(f, json.dumps(meta, indent=2, ensure_ascii=False))
        md_f = chdir / f"chapter_{ch.number:03d}.md"
        _write_text_encrypted(md_f, content)

    logger.debug(f"Saved project '{project.title}' ({project.id}).")


def delete_project(project_id: str) -> None:
    pdir = project_dir(project_id)
    title = project_id
    project_file = pdir / "project.json"
    if project_file.exists():
        try:
            title = json.loads(_read_text(project_file)).get("title", project_id)
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
        data = json.loads(_read_text(project_file))
        old_title = data.get("title", "")
        data["title"] = new_title
        _write_text_encrypted(project_file, json.dumps(data, indent=2, ensure_ascii=False))
        logger.info(f"Renamed project ({project_id}): '{old_title}' -> '{new_title}'.")






def _read_md(path: Path) -> str:
    return _read_text(path)


def _write_md(path: Path, content: str) -> None:
    _write_text_encrypted(path, content)


def _write_story_md(project: Project) -> None:
    """Write a human-readable full story file (encrypted at rest, like
    every other project file — see the note in save_project())."""
    lines = [f"# {project.title}", ""]
    if project.synopsis:
        lines += ["## Synopsis", "", project.synopsis, ""]
    for ch in sorted(project.chapters, key=lambda c: c.number):
        lines += [f"## Chapter {ch.number}: {ch.title}", ""]
        if ch.content:
            lines.append(ch.content)
        lines.append("")
    pdir = project_dir(project.id)
    _write_text_encrypted(pdir / "story.md", "\n".join(lines))


def _chapter_sort_key(path: Path) -> int:
    try:
        return int(path.stem.split("_")[1])
    except Exception:
        return 0


def save_binary_resource(
    project_id: str,
    resource_name: str,
    data: bytes,
    *,
    mime_type: str = "application/octet-stream",
    subfolder: str = "resources",
) -> dict[str, Any]:
    """Persist binary data through the existing encrypted storage layer.

    The project already uses encrypted files for all persisted content. This
    reuses that same path for character-image resources by writing the bytes
    to a dedicated file under the project's data directory and returning only
    a small metadata reference that gets stored in the Character object.

    ``subfolder`` controls which sub-directory under the project folder the
    file lands in.  Pass ``"characters"`` for character portrait images so
    they sit alongside the character JSON files rather than in a generic
    ``resources/`` bucket.
    """
    ensure_data_dir()
    resource_dir = project_dir(project_id) / subfolder
    resource_dir.mkdir(parents=True, exist_ok=True)
    target = resource_dir / resource_name
    _write_bytes(target, data)
    return {
        "path": f"{subfolder}/{resource_name}",
        "mime_type": mime_type,
        "size_bytes": len(data),
        "storage_mode": "encrypted_file",
    }


def load_binary_resource(project_id: str, resource_ref: Optional[dict[str, Any]]) -> Optional[bytes]:
    """Load a binary resource that was previously saved via save_binary_resource()."""
    if not resource_ref:
        return None
    path = project_dir(project_id) / resource_ref.get("path", "")
    if not path.exists():
        return None
    return _read_bytes(path)


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