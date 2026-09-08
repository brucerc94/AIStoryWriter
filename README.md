<div align="center">
  <img src="assets/icon.png" alt="AI Story Studio" width="110" />

  # AI Story Studio

  **A local-first writing studio for building novels with AI.**

  Write. Plan. Develop characters. Build chapters. Generate images. Export your book.

  <p>
    <img src="https://img.shields.io/badge/Status-In%20Development-F59E0B?style=flat-square" alt="In Development" />
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+" />
    <img src="https://img.shields.io/badge/UI-PySide6-41CD52?style=flat-square&logo=qt&logoColor=white" alt="PySide6" />
    <img src="https://img.shields.io/badge/LLM-GGUF-111827?style=flat-square" alt="GGUF" />
    <img src="https://img.shields.io/badge/License-MIT-0F766E" alt="MIT License" />
  </p>

  <p>
    <a href="#demo">Demo</a> ·
    <a href="#screenshots">Screenshots</a> ·
    <a href="#how-it-works">How it works</a> ·
    <a href="#using-the-ui">Using the UI</a> ·
    <a href="#context-and-workflow-behavior">Context & workflow behavior</a> ·
    <a href="#tips">Tips</a> ·
    <a href="#recommended-models">Recommended models</a> ·
    <a href="#quick-start">Quick Start</a>
  </p>
</div>

> **Status: In Development** — AI Story Studio is already usable, but it is **not a final or stable release yet**. The interface, workflows, model support, and behavior may continue to change while development progresses.

> **Documentation policy:** this README describes the **current graphical interface and user-visible workflow behavior**. Internal classes, backend APIs, prompt files, development scaffolding, and unexposed implementation details are intentionally omitted unless they are important for understanding how the application behaves.

---

## What is AI Story Studio?

AI Story Studio is a desktop application for writers who want to work with **local AI models** instead of relying on a hosted writing service.

It brings the main stages of novel creation into one workspace: story planning, characters, world notes, chapters, contextual chat, local image generation, project organization, statistics, search, and export.

The idea is deliberately simple: **you follow the workflow, and the AI helps at each stage.**

## How it works

The main writing process is:

```text
Create Project
      ↓
Synopsis
      ↓
Outline
      ↓
Characters + World + Author
      ↓
Chapters
      ↓
Review / Edit / Change / Continue
      ↓
Finish the Book
      ↓
Export
```

The important distinction is that the **Outline acts as the chapter blueprint**. Chapter generation then works from the selected chapter's plan and the relevant project canon instead of blindly feeding the entire project back into the model.

You do not need to understand agents, APIs, pipelines, or prompt files to use the application. The UI handles the underlying context and model calls for you.

---

## Recent workflow improvements

The current development branch has tightened the writing workflow around a few rules:

### Outline is a real chapter blueprint

The outline is no longer treated as a simple list of chapter titles. Each chapter is planned with a structured format centered on:

```text
Chapter Plan
Continuity
```

That gives chapter generation a clearer, binding description of what should happen and what should remain consistent.

### Extend Outline is sequential

**Extend Outline** appends new chapters after the existing outline instead of rewriting earlier chapters. New chapter numbers are validated to remain sequential, without collisions, gaps, or accidental repeats.

### Write Chapter uses chapter-scoped canon

When a chapter is generated, the application selects the **relevant established characters and world/setting information for that chapter** rather than sending all character records and the entire world document every time.

This reduces irrelevant context and helps keep the generation focused on the current chapter.

### Change Chapter uses the same principle

**Change Chapter** works from the current chapter, your requested modification, the chapter plan, and only the relevant canon needed for that change.

The goal is to change the requested part of a chapter without pulling unrelated project information into the rewrite.

### UI Chat history is isolated from chapter writing

The top-level Chat workspace has its own conversation state. That chat history is **not automatically used as context when writing or changing a chapter**.

The chapter-writing workflows do not take their instructions from whatever happened earlier in `UI/Chat`.

### Story Memory is not automatically injected into Write/Change

Story Memory remains a separate project feature. It can be inspected and maintained through the Memory area, and longer automated book workflows can update it, but **Write Chapter and Change Chapter do not use Story Memory as an automatic source of chapter-writing context**.

This separation is intentional: the chapter plan and relevant canon should drive chapter writing, while Chat and Memory remain separate tools.

---

## Demo

<div align="center">
  <img src="docs/screenshots/Animation.gif" alt="AI Story Studio demo" width="900" />
</div>

<p align="center"><sub>A quick look at the writing workflow inside AI Story Studio.</sub></p>

---

## Screenshots

<div align="center">
  <img src="docs/screenshots/story.jpg" alt="AI Story Studio — Story workspace" width="900" />
</div>

<p align="center"><sub>Story workspace — build the story from synopsis and outline through chapters.</sub></p>

<div align="center">
  <img src="docs/screenshots/chat.jpg" alt="AI Story Studio — Chat" width="900" />
</div>

<p align="center"><sub>Chat with project context and chapter-aware assistance.</sub></p>

<div align="center">
  <img src="docs/screenshots/book.jpg" alt="AI Story Studio — Book reader" width="900" />
</div>

<p align="center"><sub>Book-style reader for reviewing generated chapters as a finished manuscript.</sub></p>

---

## Features

| Area | What you can do |
|---|---|
| 📚 **Projects** | Create, open, search, rename and delete projects. See status and chapter progress. |
| ✍️ **Synopsis** | Write manually or generate a synopsis with AI. |
| 🧭 **Outline** | Create a structured chapter blueprint, edit it, extend it, and keep chapter numbering consistent. |
| 👤 **Characters** | Manage character details, relationships, and AI-generated portraits. |
| 🌍 **World** | Keep world and setting notes available as project canon. |
| 📖 **Chapters** | Generate, read, edit, change, save, review, continue, and delete chapters. Generate the remaining book from the outline. |
| 🧠 **Memory** | Maintain story memory separately, including automatic updates during longer book-generation workflows. |
| 🎨 **Author** | Define creative intent and writing-style preferences. |
| 💬 **Chat** | Ask questions, brainstorm, request writing help, attach a chapter, and control story context. |
| 🖼️ **Images** | Generate book covers, scenes, locations, objects/items, and character portraits. Image generation is currently separate from the main story-writing pipeline. |
| 📊 **Stats** | Track words, chapters, reading time, review status, progress, and chapter breakdowns. |
| 🔎 **Search** | Search the project with normal text, case-sensitive mode, or regex. |
| ⚙️ **Models & Settings** | Configure local GGUF models and generation/image settings. |
| 🖥️ **Console** | Inspect runtime logs and generation diagnostics. |
| ⇩ **Export** | Export the finished book to Word, PDF, Markdown, or plain text. |

---

## Using the UI

This section is the easiest way to understand what to do when you first open the application.

### 1. Create or open a project

The **Projects** panel is on the left side of the window.

Click **+ New** to create a project, enter a title, and optionally provide an initial synopsis. You can later open an existing project by clicking it in the list.

Once a project is open, the main workspace appears on the right.

### 2. Configure your local model

Before asking the application to generate text, make sure a GGUF model is configured.

Open **Settings** and choose your **Models Directory**. After changing the directory, scroll to the bottom of the Settings page and click **Save App Settings**. This save step is required to persist the directory change.

After saving, open **Models** and assign the appropriate GGUF model to the tasks you plan to use, such as Synopsis, Outline, Chapter, Book, Chat, Review, or Rewrite. Changes made to these per-task model assignments are saved to the current project automatically.

You can also adjust generation and hardware settings in **Settings**, including context size, GPU layers, CPU threads, response language, maximum tokens per pass, and other exposed options. Remember to click **Save App Settings** after changing these values.

### 3. Start in the Story tab

The **Story** tab contains the main writing workflow. Its internal tabs are:

```text
Synopsis → Outline → Characters → World → Chapters → Memory → Author → Stats → Search
```

You do not have to fill every field manually before generating content. The application is designed so you can write what you know and let the AI help with the next stage.

### 4. Write or generate the Synopsis

Open **Story → Synopsis**.

You can write a synopsis manually. **If you already have a draft synopsis, write or paste it into the editor and click `Save` first. You can then use `Generate Synopsis` to have the AI work from the synopsis already stored in the project instead of starting with an empty synopsis.**

You can also click **Generate Synopsis** directly when you want the AI to draft one for you. After generation, read through the result and edit or refine it as needed. Whenever you manually change the synopsis, click **Save** so the project keeps those changes before moving to the next step.

### 5. Build the Outline

Open **Story → Outline**.

Click **Generate Outline** to open the outline setup dialog. There you can choose the number of chapters and optionally define the author's creative intent and writing-style preferences used for the generation.

The outline is now intended to be a **detailed chapter blueprint**, not only a list of titles. Chapter entries follow a structured format centered on:

```text
## Chapter 1: Title

Chapter Plan
...

Continuity
...
```

The chapter plan describes the material the chapter should cover. The continuity section records important information that should remain consistent as the story progresses.

The outline remains editable. When you manually edit the outline, click **Save** to store the changes before using another workflow action that depends on the updated outline.

You can also use **Extend Outline** when you want to append additional chapters. Existing chapters are preserved, and the new chapters continue from the next available chapter number.

### 6. Add Characters

Open **Story → Characters**.

You can add characters manually with **+ Add Character**, providing:

- Name
- Role
- Physical description
- Backstory
- Traits
- Relationships

When you generate an outline, the application also extracts and adds characters found in the generated outline automatically. You can then open **Characters** to review, edit, or delete those characters, and you can always add new characters manually.

Characters can also have AI-generated portraits. Existing character data is kept when you edit a character unless you explicitly change it.

### 7. Add World & Setting information

Open **Story → World**.

Use this space for information the story needs to keep consistent: locations, rules, history, politics, culture, technology, magic systems, or other setting details.

This section is intentionally user-editable in the current UI. **After writing or changing the world information, click `Save` at the bottom of the editor.** The changes are not persisted until you save them.

### 8. Define the Author profile

Open **Story → Author**.

The **Author Profile** is where you can establish long-term creative preferences such as themes, emotional goals, inspirations, point of view, pacing, dialogue style, description density, violence, romance, genre tags, and target chapter length.

These settings are stored in the project and can be used by planning and writing workflows where appropriate.

### 9. Generate Chapters

Open **Story → Chapters**.

After an outline exists, select a chapter from the list on the left. The chapter's state determines which view you see:

- An **empty chapter** opens in **Edit** mode so you can write it or generate it.
- A chapter that already has content opens in **Read** mode.

The chapter workspace provides these main actions:

| Action | What it does |
|---|---|
| **Generate Chapter** | Generates the **currently selected chapter**. It does not automatically create the next numbered chapter. |
| **Generate Next Chapter** | Finds the first chapter in the outline that still has no content and generates that chapter. |
| **Generate Full Book** | Generates all remaining chapters from the outline in order, one after another. |
| **Change Chapter** | Opens a targeted revision workflow for the current chapter; it requires existing chapter content. |
| **Mark as Ready** | Marks the current chapter as reviewed/ready. This does not call the AI. |
| **Save** | Saves manual edits to the chapter content. |
| **Delete** | Deletes the current chapter from the project. |

For example, if your outline has Chapters 1–5 but Chapter 3 is still empty, **Generate Next Chapter** targets Chapter 3. If Chapter 4 is selected and you press **Generate Chapter**, it generates Chapter 4 regardless of which other chapters are empty.

Use **Read** mode to review the chapter as a book page. You can move between chapters with **Previous Chapter / Next Chapter** and through pages with **Previous Page / Next Page**. Use **Edit** mode when you need to change the title or text manually.

The editor also shows a live word count, character count, and estimated reading time.

**Generate Full Book** is useful when you want the application to work through the remaining outline automatically. It confirms how many chapters remain before starting and maintains story continuity across the generated chapters.

### 10. Use Change Chapter for targeted revisions

**Change Chapter** is for an existing chapter that needs a controlled modification.

Describe:

- what must change
- what must remain unchanged
- the desired tone or emotional effect
- which scene or passage should be expanded or shortened
- how characters should behave
- what information should be added or removed
- what the ending should preserve

The workflow is designed to keep the rewrite focused on the requested chapter rather than pulling unrelated project context into the revision.

A strong request looks more like:

```text
Make the confrontation between Elena and Marcus more tense.

Keep the existing plot events and ending, but make Elena more defensive
and Marcus more controlled and threatening. Add more subtext to their
dialogue, slow the scene slightly, and preserve the final outcome.
Do not add a new character.
```

The more concrete the instruction, the easier it is to steer the revision toward the result you actually want.

### 11. Use Story Memory

Open **Story → Memory** to inspect the story memory maintained by the application.

During longer book-generation workflows, memory can be updated between chapters. You can also edit the memory manually when needed.

**Memory is intentionally separate from Write Chapter and Change Chapter input context.** Those workflows do not automatically read Story Memory when generating or rewriting a chapter.

After making manual changes, click **Save** at the bottom of the Memory editor to persist them.

### 12. Use Chat when you want assistance

The top-level **Chat** tab is a general-purpose writing assistant.

You can send questions, brainstorm ideas, ask for writing help, enable or disable project context, and attach a chapter to the conversation.

With context enabled, Chat can use project information such as the synopsis, outline, characters and relationships, world notes, memory, and conversation summary.

Chat has its own conversation history. **That UI chat history is not automatically carried into Write Chapter or Change Chapter.**

### 13. Generate images separately

The top-level **Images** tab provides local image generation for:

- Book Cover
- Scene Illustration
- Location
- Object / Item
- Character portraits through the character workflow

Image generation **works**, but it is currently a **separate tool and is not yet integrated into the main synopsis → outline → characters/world → chapters pipeline**. Generated images are saved inside the active project.

Open **Images**, enter a prompt, optionally set a negative prompt, seed, dimensions, steps, and CFG, then click **Generate**.

### 14. Check progress and find text

Use **Stats** to see project progress, chapter counts, word counts, reading-time information, and review status.

Use **Search** to find text across the project.

### 15. Export the finished book

Use **Export Book** from the top-right of the application window.

The current UI supports:

- Word (`.docx`)
- PDF (`.pdf`)
- Markdown (`.md`)
- Plain text (`.txt`)

---

## Context and workflow behavior

AI Story Studio uses different context boundaries for different jobs. This is important when working with a large project.

### Outline generation

Outline generation can use the project material needed to plan the story, including relevant Characters, World information, and Author preferences.

The goal is to create a detailed chapter-by-chapter blueprint.

### Write Chapter

Write Chapter is driven by the selected chapter's outline entry and the chapter-writing workflow.

For chapter canon, the application selects **relevant established characters and relevant world/setting sections** based on the current chapter context. This is intentionally narrower than sending all project canon.

Write Chapter does **not** automatically use:

```text
UI Chat history       ❌
Chat Summary          ❌
Story Memory          ❌
Full project synopsis ❌
Entire character database ❌
Entire world document ❌
```

The chapter workflow can still use the previous chapter's ending as a continuity anchor when continuing the story.

### Change Chapter

Change Chapter follows the same isolation principle.

The rewrite is based on:

```text
Current Chapter
       +
User's requested change
       +
Chapter checklist / plan
       +
Relevant Characters
       +
Relevant World / Setting
```

The top-level Chat history and Story Memory are not automatically injected into this workflow.

### Chat

Chat is intentionally different.

When Chat context is enabled, it can use broader project information and its own conversation history. That makes Chat suitable for brainstorming and open-ended assistance.

This separation prevents a conversation in the Chat tab from silently becoming writing context for a later chapter generation.

---

## Tips

These are practical settings and workflow habits that have worked well in testing. They are **recommendations, not hard requirements**; your hardware, model, and story can change the best values.

### Keep character relationships detailed

When a character has relationships with other characters, define them explicitly in the **Characters** tab instead of leaving the relationship section vague.

For example:

```text
father of → Daniel
rival of → Marcus
friend of → Elena
```

Try to define important relationships **for each relevant character**, not only for the protagonist. This gives chapter generation more concrete information about how characters should behave toward each other.

### Make the outline specific

The more useful the chapter plan is, the easier it is for chapter generation to stay on track.

A good chapter entry should make clear:

- what the chapter needs to accomplish
- which events should happen
- which characters matter
- what continuity must be preserved
- how the chapter should end or transition into the next one

### Use Change Chapter for precise revisions

Use **Change Chapter** when you already have a chapter and want to make a controlled revision.

Be specific. Instead of:

```text
Make this chapter better.
```

describe exactly what should change and what should remain.

The more concrete the instruction, the easier it is to steer the revision toward the result you actually want.

### Context size

A good starting point is **8192 tokens** for **Context Size**.

You can use a smaller context if your hardware needs it. Context Size is the model's combined window for the prompt and the generated response, so increasing it can help when the project context is large, while decreasing it can reduce memory pressure.

### Max Tokens per Pass

A useful starting point is **4256 tokens** for **Max Tokens per Pass**.

This is separate from Context Size: Context Size is the total model window, while Max Tokens per Pass limits how much the application asks the model to generate in one pass.

The best value depends on the model and the task, so treat 4256 as a practical starting point rather than a universal setting.

### Thinking mode

For the current writing workflow, **Enable Thinking** can be left **off** when using a model that supports that option.

In testing, keeping thinking disabled avoids spending part of the available token budget on hidden reasoning and leaves more room for the actual story response. If you prefer a different balance for a particular model, you can experiment with it.

### Save manual edits

Whenever you manually change content in a section that has a **Save** button, use it before moving on.

This is especially important for **Synopsis**, **Outline**, **World**, **Memory**, and chapter editing.

---

## Recommended models

AI Story Studio uses local **GGUF** language models. Choose the model that matches your hardware.

### 🟢 Around 6 GB VRAM

**Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M**

Recommended as a starting point for GPUs in the **6 GB VRAM class**. The Q4_K_M file is about **5.0 GB**, so actual usable context and GPU offloading depend on the rest of your VRAM/RAM configuration.

[Hugging Face — Gemma-4-E4B-Uncensored-HauhauCS-Aggressive](https://huggingface.co/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive)

### 🔵 More powerful hardware

**Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_XS**

For systems with substantially more memory, this is the higher-capability option. The IQ4_XS file is about **18.7 GB**. Qwen3.6-35B-A3B is a **Mixture-of-Experts (MoE)** model with 35B total parameters and an A3B configuration.

[Hugging Face — Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive](https://huggingface.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive)

> **Hardware note:** an 18.7 GB GGUF is not the same thing as “18.7 GB VRAM required” in every setup. CPU/RAM offloading is possible, but full GPU offload needs enough VRAM for the model plus runtime/context overhead.

### MoE support

AI Story Studio can also be used with **MoE models**. The application exposes MoE-related performance settings in **Settings → MoE Performance**, so users with compatible hardware can tune this class of model without changing the writing workflow.

The model choice is the part that changes. The writing workflow remains the same.

---

## Chat

Chat is the general-purpose AI workspace.

Available controls include:

- **Send**
- **Stop**
- **Insert Chapter**
- **Context: ON/OFF**
- **Clear Chat**

With context enabled, the conversation can include the project's synopsis, outline, characters and relationships, world notes, memory, and conversation summary.

Chat history remains local to the Chat workflow and does not silently become Write Chapter or Change Chapter context.

---

## Image generation

The **Images** area is currently independent from the main writing workflow.

It provides:

| Type | Purpose |
|---|---|
| **Book Cover** | Create a visual cover concept. |
| **Scene Illustration** | Visualize a moment from the story. |
| **Location** | Visualize a place or setting. |
| **Object / Item** | Visualize an important prop, artifact, weapon, object, or item. |

Character portraits can also be generated from the Characters workflow or in batches from the Images tab.

All generated images are saved inside the active project.

---

## Export

Use **Export Book** to save the current book as:

- **Word (.docx)**
- **PDF (.pdf)**
- **Markdown (.md)**
- **Plain text (.txt)**

---

## Quick Start

### Requirements

- Python **3.10+**
- PySide6
- cryptography
- A compatible local **GGUF** model with `llama-cpp-python` for AI text generation
- The image-generation backend and model configuration when using **Images**
- `python-docx` and `reportlab` when Word/PDF export support is needed

### Install

```bash
git clone https://github.com/brucerc94/AIStoryWriter.git
cd AIStoryWriter
python -m venv .venv
```

**Windows**

```bat
.venv\Scripts\activate
```

**Linux / macOS**

```bash
source .venv/bin/activate
```

Then:

```bash
pip install -r requirements.txt
pip install llama-cpp-python
```

Optional Word/PDF export support:

```bash
pip install python-docx reportlab
```

### Run

```bash
python main.py
```

On Windows, `run.bat` is also available as a convenience launcher.

### First setup in the app

1. Open **Settings** and choose the **Models Directory**.
2. Scroll to the bottom and click **Save App Settings**.
3. Open **Models** and assign a GGUF model to the tasks you plan to use.
4. Create a project from **Projects → + New**.
5. Open **Story → Synopsis** and write or generate the synopsis. Click **Save** after manual edits.
6. Open **Story → Outline** and generate or write the outline. Click **Save** after manual edits.
7. Add **Characters**, **World**, and an **Author Profile** as needed. Click **Save** after manual World or Memory edits.
8. Open **Story → Chapters** and generate chapters from the outline.
9. Use **Chat**, **Images**, **Stats**, **Search**, and **Console** as needed.
10. Export the finished manuscript with **Export Book**.

---

## Current scope

AI Story Studio is a local desktop application. Language and image models are **not bundled** with the repository and must be provided by the user.

The main writing workflow is the **Story** workspace. Image generation is functional, but it is currently a standalone companion tool rather than part of the automatic story-generation pipeline.

The current chapter workflow is designed around a **structured Outline + scoped canon + chapter writing** model:

```text
Outline
  ↓
Chapter Plan + Continuity
  ↓
Relevant Characters + World
  ↓
Write / Change Chapter
  ↓
Review / Continue
```

The Chat workspace and Story Memory remain separate sources of assistance and state.

This README follows the **UI currently shipped** and the workflow behavior currently implemented. New user-facing features should be documented here when they are actually visible and usable in the application.

Because the project is still in development, behavior, UI details, and supported models may change before a stable release.

## Screenshot directory

```text
docs/
└── screenshots/
    ├── Animation.gif
    ├── story.jpg
    ├── chat.jpg
    └── book.jpg
```

## License

MIT
