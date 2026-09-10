<div align="center">
  <img src="assets/icon.png" alt="AI Story Studio" width="110" />

  # AI Story Studio

  **A local-first writing studio for building novels with AI.**

  Write your story. Refine the draft. Build an outline. Develop characters and world. Write chapters. Export the book.

  <p>
    <img src="https://img.shields.io/badge/Status-In%20Development-F59E0B?style=flat-square" alt="In Development" />
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+" />
    <img src="https://img.shields.io/badge/UI-PySide6-41CD52?style=flat-square&logo=qt&logoColor=white" alt="PySide6" />
    <img src="https://img.shields.io/badge/LLM-GGUF-111827?style=flat-square" alt="GGUF" />
    <img src="https://img.shields.io/badge/License-MIT-0F7660" alt="MIT License" />
  </p>

  <p>
    <a href="#demo">Demo</a> ·
    <a href="#screenshots">Screenshots</a> ·
    <a href="#how-it-works">How it works</a> ·
    <a href="#features">Features</a> ·
    <a href="#using-the-ui">Using the UI</a> ·
    <a href="#workflow-architecture">Architecture</a> ·
    <a href="#todo">TODO</a> ·
    <a href="#quick-start">Quick Start</a>
  </p>
</div>

> **Status: In Development** — AI Story Studio is usable, but it is not a final or stable release yet. The UI, workflows, model support, and behavior may continue to change during development.

---

## What is AI Story Studio?

AI Story Studio is a desktop application for writers who want to build novels with **local AI models** rather than a hosted writing service.

The application combines story drafting, outline planning, character and world management, chapter generation and revision, author preferences, contextual chat, memory, local image generation, statistics, search, and export in one workspace.

The writing pipeline is deliberately separated into stages so that each model call has a clear job and receives only the context it needs.

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

<p align="center"><sub>Story workspace — build the story from Synopsis / Draft and Outline through chapters.</sub></p>

<div align="center">
  <img src="docs/screenshots/chat.jpg" alt="AI Story Studio — Chat" width="900" />
</div>

<p align="center"><sub>Chat with project context and chapter-aware assistance.</sub></p>

<div align="center">
  <img src="docs/screenshots/book.jpg" alt="AI Story Studio — Book reader" width="900" />
</div>

<p align="center"><sub>Book-style reader for reviewing generated chapters as a finished manuscript.</sub></p>

---

## How it works

The current workflow is:

```text
Create Project
      ↓
Synopsis / Draft
      ↓
Consistency + Draft Development
      ↓
Characters + World
      ↓
Outline
      ↓
Chapters
      ↓
Review / Change / Continue
      ↓
Export
```

`Synopsis / Draft` is the author's story source and normalization stage. `Outline` is the structured chapter blueprint used to drive chapter generation.

---

## Synopsis / Draft

The Story tab presents the story source as **Synopsis / Draft**.

The user can write or paste raw story material directly into the editor and press **Generate Draft**. The current editor text is used directly, so it does not need to be saved first.

The pipeline is:

```text
User story text
      ↓
Consistency check
      ↓
If problems exist → repair contradictions
      ↓
Fresh consistency check
      ↓
Draft development + Author Profile
      ↓
Final Synopsis / Draft
      ↓
Characters + World
```

The consistency stage checks problems such as contradictory ordering, causal conflicts, character-state conflicts, location conflicts, identity conflicts, and incompatible events. Repairs are intended to change only what is necessary to make the story coherent.

The final result is a **developed draft**, not finished chapter prose. It can be substantially richer than the user's raw material, but it is not intended to turn into a full scene-by-scene chapter with extensive dialogue.

Characters and World are extracted or updated **only after the final draft exists**.

The internal project field remains `project.synopsis` for compatibility while the UI presents it as **Synopsis / Draft**.

---

## Outline

### Generate Outline

`Generate Outline` takes the completed Synopsis / Draft and the requested chapter count, then partitions the story into exactly that number of chronological source blocks.

```text
Synopsis / Draft
      ↓
Semantic chronological split
      ↓
Exactly N chapter source blocks
      ↓
For each chapter:
      ├── relevant Characters
      ├── relevant World
      ├── full Author Profile
      └── rich chapter treatment
```

Each source block preserves meaningful events, causal order, motivations, relationships, descriptive details, turning points, and consequences needed by the chapter-treatment writer.

The chapter treatment is a **rich planning artifact**. It can contain character actions, emotional movement, interactions, dialogue content, sensory details, turning points, and consequences. It is still not the finished novel chapter.

`Generate Outline` does **not** create or update Characters or World as a side effect.

### Extend Outline

`Extend Outline` works from an existing outline plus new material entered by the user. It generates the requested new chapters without rewriting earlier ones.

For each new chapter it uses:

```text
New chapter source
      +
Relevant Characters
      +
Relevant World
      +
Author Profile
      ↓
Rich chapter treatment
```

After all requested chapters have been successfully generated, **Characters and World are updated from the user's original extension material**. This allows a user extension to introduce new characters, relationships, locations, or world information that did not exist in the original draft.

---

## Characters and World

Characters and World are reusable project canon.

### Characters

Characters can contain:

- name and role
- description
- backstory
- traits
- relationships
- generated portraits

Automatic character extraction attempts to merge candidates with existing records when they represent the same character.

### World

World stores reusable setting information. Chapter workflows can select only the relevant portion of the world for the current chapter instead of sending the entire document every time.

---

## Author Profile

The Author area stores long-term creative preferences used by planning and writing workflows where appropriate.

It can include creative intent and writing-style preferences such as:

- emotional journey and lasting reader impression
- themes and unique elements
- inspirations and things to avoid
- point of view
- pacing
- prose density
- dialogue frequency
- violence and romance level
- genre and other chapter-writing preferences

---

## Chapter generation

### Write Chapter

`Write Chapter` uses the selected outline entry, a planning checklist, relevant Characters and World, the Author Profile, and immediate previous-chapter continuity where applicable.

Its generation pattern is:

```text
Planner
   ↓
Writer
   ↓
Evaluator
   ↓
Complete? ── Yes → Save
   │
   No
   ↓
Fresh continuation pass
```

Continuation passes use bounded checkpoints and rebuild their prompts from Python state instead of depending on prior UI Chat turns.

### Change Chapter

`Change Chapter` performs a targeted rewrite of an existing chapter. Its request can go through a consistency precheck before the rewrite planner continues.

The rewrite uses scoped chapter context and does not automatically import UI Chat history as hidden context.

### Generate Full Book

`Generate Full Book` is an orchestration layer over `Write Chapter`.

It:

- writes remaining chapters sequentially using the normal `Write Chapter` workflow;
- keeps Chat cleanup between chapters;
- reloads the project between chapters;
- stops if a chapter is incomplete;
- does **not** automatically update Story Memory.

---

## Context and workflow boundaries

Dedicated chapter workflows deliberately use scoped context.

```text
Full Character DB ───┐
                     ├──> Relevant chapter context ──> Chapter workflow
Full World ──────────┘

Outline Chapter N ────────────────────────────────────> Chapter workflow
Author Profile ───────────────────────────────────────> Chapter workflow
```

Write Chapter and Change Chapter do not automatically use:

```text
UI Chat history        ❌
Chat Summary           ❌
Story Memory           ❌
Unrelated project data ❌
```

This keeps model prompts focused on the current task.

---

## Memory

Story Memory is a separate project feature. It can be inspected and edited manually.

`Generate Full Book` does not automatically update Story Memory between chapters. Memory therefore remains separate from the chapter-writing input context unless a dedicated workflow explicitly uses it.

---

## Chat

The top-level Chat tab is a general-purpose writing assistant.

With project context enabled, Chat can work with broader project information and its own conversation history. A chapter can also be attached for focused discussion.

Chat history is not automatically carried into Write Chapter or Change Chapter.

---

## Features

| Area | What it does |
|---|---|
| 📚 **Projects** | Create, open, search, rename, and delete projects. |
| ✍️ **Synopsis / Draft** | Write or paste story material, validate consistency, repair contradictions, develop the draft with the Author Profile, then build Characters and World from the finalized draft. |
| 🧭 **Outline** | Split the story into chronological chapter source blocks, generate rich chapter treatments, edit them, and extend an existing outline. |
| 👤 **Characters** | Manage character details, traits, backstories, relationships, and portraits. |
| 🌍 **World** | Manage reusable setting and world information. |
| 📖 **Chapters** | Generate, continue, review, change, edit, save, and delete chapters. Generate the remaining book sequentially. |
| 🧠 **Memory** | Maintain separate reusable story-state information. |
| 🎨 **Author** | Define creative intent and writing-style preferences. |
| 💬 **Chat** | General writing assistance with optional project context and chapter attachment. |
| 🖼️ **Images** | Generate book covers, scenes, locations, objects/items, and character portraits locally. |
| 📊 **Stats** | Inspect word counts, chapter counts, reading-time information, review state, and progress. |
| 🔎 **Search** | Search project content with normal, case-sensitive, or regex-based matching. |
| ⚙️ **Models & Settings** | Configure GGUF models, context size, hardware options, generation parameters, and response language. |
| 🖥️ **Console** | Inspect runtime logs and generation diagnostics. |
| ⇩ **Export Book** | Export the finished book to Word, PDF, Markdown, or plain text. |

---

## Using the UI

### 1. Create or open a project

Use **Projects** to create a project or continue an existing one.

### 2. Configure models

Open **Settings** to configure the models directory and local generation settings. Use **Models** to assign GGUF models to the required tasks.

### 3. Write the story

Open **Story → Synopsis / Draft**, write or paste your story, then press **Generate Draft**.

### 4. Generate the outline

Open **Story → Outline**, choose the chapter count, and press **Generate Outline**.

### 5. Extend the outline

Use **Extend Outline** when you want to add new story material and additional chapters to an existing outline.

### 6. Review the canon

Inspect **Characters** and **World** after the draft or an outline extension has updated them. Manual editing remains available.

### 7. Write chapters

Open **Story → Chapters** and use:

```text
Generate Chapter
Generate Next Chapter
Generate Full Book
Change Chapter
Continue
Review
Save
Delete
```

### 8. Export

Use **Export Book** to create the final manuscript in a supported format.

---

## Workflow Architecture

```text
                    AUTHOR STORY INPUT
                           │
                           ▼
                  ┌──────────────────┐
                  │ SYNOPSIS / DRAFT │
                  └────────┬─────────┘
                           │
                 consistency validation
                           │
                    repair if needed
                           │
                    draft development
                    + Author Profile
                           │
                           ▼
                 Final Synopsis / Draft
                           │
                  ┌────────┴────────┐
                  ▼                 ▼
             Characters          World
                  │                 │
                  └────────┬────────┘
                           ▼
                       OUTLINE
                           │
                  semantic split
                           │
                relevant canon per chunk
                           │
                    Author Profile
                           │
                           ▼
                 detailed chapter plans
                           │
                           ▼
                       CHAPTERS
                           │
                 write / change / review
                           │
                           ▼
                         BOOK
```

### Generate Outline

```text
Final Synopsis / Draft
        ↓
Split into exactly N chronological blocks
        ↓
Chunk N
   ├── relevant Characters
   ├── relevant World
   └── Author Profile
        ↓
Rich chapter treatment
```

### Chapter writing

```text
Outline Chapter N
      ↓
Planning checklist
      ↓
Relevant Characters + World
      ↓
Author Profile
      ↓
Write Chapter
      ↓
Evaluator
      ├── complete → save
      └── incomplete → fresh continuation
```

---

## Local-first design

The application is designed around local inference with GGUF models through `llama-cpp-python`.

The model layer supports GPU layer configuration, CPU thread settings, context-size configuration, and generation controls such as temperature, Top P, and Top K.

---

## Images

The Images area provides separate local image-generation workflows for:

- character portraits
- book covers
- scene illustrations
- locations
- object/item images

Image generation is intentionally separate from the text-generation pipeline.

---

## TODO

### Character Generation / Extraction

- Control the maximum number of traits per character.
- Avoid redundant or overly generic traits.
- Improve trait selection so only story-relevant traits are retained.
- Validate relationships before adding them.
- Prevent the model from inventing unsupported relationships.
- Prevent duplicate or contradictory relationships.
- Ensure both characters involved in a relationship actually exist.
- Properly separate `role`, `traits`, `description`, and `backstory`.
- Improve merging with existing characters without overwriting valid information.
- Make Character Generation rely only on information explicitly stated or strongly supported by the Synopsis/Draft, Outline, and Chapters.

---

## Quick Start

### Requirements

- Python 3.10+
- PySide6
- `llama-cpp-python`
- Local GGUF language models
- Optional CUDA-capable GPU configuration

### Run on Windows

The repository includes `run.bat` for launching the application from the local virtual environment after setup.

```text
run.bat
```

### First launch

1. Create or open a project.
2. Configure the models directory and assign GGUF models.
3. Open **Synopsis / Draft** and write your story.
4. Generate the normalized draft.
5. Review Characters and World.
6. Generate the Outline with the desired chapter count.
7. Generate and revise chapters.
8. Export the finished book.

---

## Development status

AI Story Studio is an active development project. Local-model behavior can vary by model family, quantization, context size, and hardware.

The repository contains automated tests covering important workflow, context, continuation, and outline behaviors.

---

## License

MIT
