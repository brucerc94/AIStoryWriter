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
    <a href="#architecture-and-workflow-diagrams">Architecture</a> ·
    <a href="#tips">Tips</a> ·
    <a href="#recommended-models">Recommended models</a> ·
    <a href="#quick-start">Quick Start</a>
  </p>
</div>

> **Status: In Development** — AI Story Studio is usable, but it is **not a final or stable release yet**. The interface, workflows, model support, and behavior may continue to change while development progresses.

> **Documentation policy:** this README describes the current graphical interface and user-visible workflow behavior. Internal implementation details are included only when they help explain the writing architecture and context boundaries.

---

## What is AI Story Studio?

AI Story Studio is a desktop application for writers who want to work with **local AI models** instead of relying on a hosted writing service.

It brings story planning, character development, world building, chapters, contextual chat, local image generation, project organization, statistics, search, and export into one workspace.

The central idea is simple: **the Outline builds the story blueprint and chapter canon; chapter workflows then execute that plan using only the context that is relevant to the selected chapter.**

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

The **Outline acts as the chapter blueprint**. Chapter generation uses the selected chapter's plan plus scoped project canon instead of blindly sending the entire project back to the model.

---

## Recent workflow architecture

The current writing architecture is built around a strict separation of responsibilities.

### Outline is the source of the story plan

The outline is not intended to be only a list of chapter titles. Each chapter should contain a detailed plan and continuity information describing what the chapter must accomplish and what must remain consistent.

Typical chapter structure:

```text
## Chapter N: Title

Chapter Plan
- events
- characters
- goals
- scene progression
- important details

Continuity
- facts that must remain consistent
- relationships
- unresolved threads
- transitions
```

A detailed outline gives `Write Chapter` and `Change Chapter` a concrete execution target.

### Outline owns canon construction

`Generate Outline` and `Extend Outline` are the workflows responsible for building and extending the story blueprint and its reusable canon.

```text
GENERATE OUTLINE
  ├── Outline    ✅ build/update
  ├── Characters ✅ create/update
  ├── World      ✅ build/update
  └── Memory     ✅ planning state

EXTEND OUTLINE
  ├── Outline    ✅ extend
  ├── Characters ✅ create/update
  ├── World      ✅ update
  └── Memory     ✅ planning state
```

### Write/Change Chapter execute canon

`Write Chapter` and `Change Chapter` are deliberately more isolated. They consume the chapter plan and relevant canon instead of becoming new sources of project canon.

```text
WRITE CHAPTER
  ├── read Characters (scoped) ✅
  ├── read World (scoped)      ✅
  ├── update Characters        ❌
  ├── update World             ❌
  ├── update Memory            ❌
  └── use UI Chat history      ❌

CHANGE CHAPTER
  ├── read Characters (scoped) ✅
  ├── read World (scoped)      ✅
  ├── update Characters        ❌
  ├── update World             ❌
  ├── update Memory            ❌
  ├── use previous chapter     ❌
  └── use UI Chat history      ❌
```

This keeps the Outline as the planning/canon layer and the chapter workflows as execution layers.

### Chapter-scoped canon

A chapter does not need the entire character database or entire world document. The workflow first derives a chapter-specific source from the Outline, checklist, relevant prose, and request, then selects the relevant established Characters and World sections.

```text
Full Character DB
       │
       ▼
Relevant character records
       │
       ▼
Write / Change

Full World
       │
       ▼
Relevant world / setting sections
       │
       ▼
Write / Change
```

This reduces irrelevant prompt context and helps keep generation focused.

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
| 🧠 **Memory** | Maintain story memory separately, including automatic updates during longer sequential book-generation workflows. |
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

### 1. Create or open a project

The **Projects** panel is on the left side of the window.

Click **+ New** to create a project, enter a title, and optionally provide an initial synopsis. You can later open an existing project by clicking it in the list.

### 2. Configure your local model

Open **Settings** and choose your **Models Directory**. After changing the directory, click **Save App Settings**.

After saving, open **Models** and assign the appropriate GGUF model to the tasks you plan to use, such as Synopsis, Outline, Chapter, Book, Chat, Review, or Rewrite.

You can also adjust generation and hardware settings in **Settings**, including context size, GPU layers, CPU threads, response language, maximum tokens per pass, and other exposed options.

### 3. Start in the Story tab

The **Story** tab contains the main writing workflow:

```text
Synopsis → Outline → Characters → World → Chapters → Memory → Author → Stats → Search
```

### 4. Write or generate the Synopsis

Open **Story → Synopsis**.

You can write a synopsis manually. **If you already have a draft synopsis, write or paste it into the editor and click `Save` first.** You can then use `Generate Synopsis` to work from the stored synopsis.

### 5. Build the Outline

Open **Story → Outline**.

Click **Generate Outline** to choose the number of chapters and optional author preferences.

The outline is intended to be a **detailed chapter blueprint**. `Extend Outline` appends new chapters after the existing outline.

### 6. Add Characters

Open **Story → Characters**.

You can add characters manually with **+ Add Character**, including name, role, physical description, backstory, traits, and relationships.

Characters found during outline generation can also be extracted into the Characters area for review and editing. Character portraits can be generated from the character workflow.

### 7. Add World & Setting information

Open **Story → World**.

Use this area for locations, rules, history, politics, culture, technology, magic systems, and other setting details that should remain consistent.

After manual changes, click **Save**.

### 8. Define the Author profile

Open **Story → Author**.

Use the **Author Profile** for long-term creative preferences such as themes, emotional goals, point of view, pacing, dialogue style, description density, violence, romance, genre tags, and target chapter length.

### 9. Generate Chapters

Open **Story → Chapters**.

After an outline exists, select a chapter from the list on the left.

| Action | What it does |
|---|---|
| **Generate Chapter** | Generates the **currently selected chapter**. |
| **Generate Next Chapter** | Finds the first chapter in the outline that still has no content and generates it. |
| **Generate Full Book** | Generates remaining chapters from the outline in order. |
| **Change Chapter** | Rewrites the current chapter according to a requested modification. |
| **Mark as Ready** | Marks the current chapter as reviewed/ready without calling the AI. |
| **Save** | Saves manual chapter edits. |
| **Delete** | Deletes the current chapter. |

`Write Chapter` uses the chapter plan, checklist, scoped Characters/World, author preferences, and for normal next-chapter continuity the ending of the previous chapter.

`Change Chapter` starts from the current chapter and the requested change. It does not use the previous chapter as automatic context.

### 10. Use Change Chapter for targeted revisions

Describe what must change and what must remain unchanged. Useful instructions identify the scene, characters, tone, pacing, additions/removals, and desired ending behavior.

Example:

```text
Make the confrontation between Elena and Marcus more tense.

Keep the existing plot events and ending, but make Elena more defensive
and Marcus more controlled and threatening. Add more subtext to their
dialogue, slow the scene slightly, and preserve the final outcome.
Do not add a new character.
```

### 11. Use Story Memory

Open **Story → Memory** to inspect or edit story memory.

Memory is a separate planning/state feature. It is **not automatically used as chapter-writing input by Write Chapter or Change Chapter**.

Longer sequential book-generation workflows may update Memory between chapters.

### 12. Use Chat when you want assistance

The top-level **Chat** tab is a general-purpose writing assistant.

When Chat context is enabled, it can use broader project information and its own conversation history. That history is **not automatically carried into Write Chapter or Change Chapter**.

### 13. Generate images separately

The **Images** tab provides local image generation for book covers, scenes, locations, objects/items, and character portraits.

Image generation is currently a separate companion workflow rather than part of the automatic synopsis → outline → chapter pipeline.

### 14. Check progress and search

Use **Stats** for word counts, chapter counts, reading-time information, review status, and progress.

Use **Search** to find text across the project.

### 15. Export the finished book

Use **Export Book** from the top-right of the application window.

Current formats:

- Word (`.docx`)
- PDF (`.pdf`)
- Markdown (`.md`)
- Plain text (`.txt`)

---

## Architecture and workflow diagrams

This section documents the current chapter architecture, context boundaries, and continuation strategy.

### 1. Overall architecture

```text
                         ┌───────────────┐
                         │    SYNOPSIS   │
                         └───────┬───────┘
                                 │
                         ┌───────▼───────┐
                         │    OUTLINE    │
                         │               │
                         │ Story Blueprint│
                         └───────┬───────┘
                                 │
                 ┌───────────────┼───────────────┐
                 │               │               │
                 ▼               ▼               ▼
            Characters         World          Memory
                 │               │               │
                 └───────────────┴───────────────┘
                                 │
                                 ▼
                       ┌──────────────────┐
                       │ WRITE / CHANGE   │
                       └────────┬─────────┘
                                │
                                ▼
                       Chapter Preparation
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              Relevant Canon          Checklist
                    │                       │
                    └───────────┬───────────┘
                                ▼
                           GENERATION
                                │
                                ▼
                              EVAL
                                │
                    ┌───────────┴───────────┐
                    │                       │
                COMPLETE                  INCOMPLETE
                    │                       │
                    ▼                       ▼
                   FIN                 CONTINUATION
                                           │
                                           └──────────↻
```

### 2. Canon-building workflows

`Generate Outline` and `Extend Outline` are the workflows that construct the reusable story plan and canon.

```text
┌──────────────────────────────┐
│       GENERATE OUTLINE       │
└──────────────┬───────────────┘
               │
               ▼
        Synopsis + Memory
               │
               ▼
        Characters / World
               │
               ▼
              LLM
               │
               ▼
      Detailed Story Outline
               │
      ┌────────┼─────────┐
      ▼        ▼         ▼
 Chapters   Characters   World
      │        │         │
      └────────┼─────────┘
               │
               ▼
             Memory
```

```text
Existing Outline
      +
Memory
      +
Characters
      +
World
      │
      ▼
EXTEND OUTLINE
      │
      ▼
New Chapter Plans
      │
      ▼
Characters / World / Memory
```

### 3. Write Chapter workflow

```text
┌─────────────────────────────┐
│          WRITE CHAPTER      │
└──────────────┬──────────────┘
               │
               ▼
        Outline Chapter N
               │
               ▼
          PLAN CHAPTER
               │
               ▼
      Checklist Original 1..N
               │
               ▼
         Select relevant canon
               │
          ┌────┴─────┐
          ▼          ▼
     Characters     World
      relevant     relevant
          │          │
          └────┬─────┘
               ▼
        Chapter Context
               │
               ▼
        GENERATION PASS
               │
               ▼
             Text
               │
               ▼
        ¿Token limit hit?
          ┌────┴────┐
          │         │
         NO        YES
          │         │
          ▼         ▼
         EVAL    Continuation
                    │
                    ▼
                   EVAL
```

`Write Chapter` can use the end of the previous chapter as a normal continuity anchor when generating the next chapter.

### 4. Change Chapter workflow

```text
┌─────────────────────────────────────┐
│             CHANGE CHAPTER          │
└──────────────────┬──────────────────┘
                   │
                   ▼
          Current Chapter
                   +
          User Change Request
                   │
                   ▼
              PLAN CHANGE
                   │
                   ▼
              Checklist 1..N
                   │
                   ▼
          Select relevant canon
              ┌────┴─────┐
              ▼          ▼
         Characters     World
          relevant     relevant
              │          │
              └────┬─────┘
                   ▼
             Full Rewrite
                   │
                   ▼
                 EVAL
                   │
            ┌──────┴──────┐
            │             │
         COMPLETE      INCOMPLETE
            │             │
            ▼             ▼
           FIN        CONTINUATION
                           │
                           ▼
                          EVAL
```

`Change Chapter` starts from the current chapter, the requested modification, its plan/checklist, and scoped canon. It does not automatically use the previous chapter.

### 5. Checklist lifecycle

The checklist is created once for the chapter operation and remains the planning contract for that run. Progress is maintained in Python state between passes.

```text
                   PLANNER
                      │
                      ▼
              CHECKLIST ORIGINAL
                    1..60
                      │
                      ▼
                 GENERATION
                      │
                token cutoff
                      │
                      ▼
                  EVALUATOR
                      │
              done = 1..42
                      │
                      ▼
             accumulated_done
                  1..42
                      │
                      ▼
              active remaining
                  43..60
                      │
                      ▼
                CONTINUATION
                      │
                 more prose
                      │
                      ▼
                  EVALUATOR
                      │
              done = 43..51
                      │
                      ▼
             accumulated_done
                  1..51
                      │
                      ▼
              active remaining
                  52..60
                      │
                      ▼
                CONTINUATION
                      │
                      ▼
                  EVALUATOR
                      │
              done = 52..60
                      │
                      ▼
                  COMPLETE
```

### 6. Active-window evaluation

The evaluator is not repeatedly given the whole checklist after every continuation.

```text
CHECKLIST ORIGINAL
        │
        ▼
Python state
        │
        ▼
Active checklist items
        │
        ▼
┌─────────────────────────┐
│       EVALUATOR         │
│                         │
│ active requirements     │
│ + current full chapter  │
└───────────┬─────────────┘
            │
            ▼
       evaluation
            │
            ▼
     new completed IDs
            │
            ▼
      Python state
            │
            └──────────↻
```

Example:

```text
Pass 1
  active: 1..60
  current text: roughly through 42
  result:       1..42 done

Pass 2
  active: 43..60
  current text: roughly through 51
  result:       43..51 done

Pass 3
  active: 52..60
  current text: complete
  result:       52..60 done
```

The evaluator must judge the **current chapter text** for the active window. It must not be told to trust previous IDs as automatically complete.

### 7. Continuation state

When the chapter hits the generation limit, the continuation uses the state required to continue without restarting the scene.

```text
CURRENT CHAPTER END
        +
CHAPTER OUTLINE
        +
RELEVANT CHARACTERS
        +
RELEVANT WORLD
        +
ACTIVE REMAINING CHECKLIST
        +
NEXT REQUIRED ID
        │
        ▼
   CONTINUATION
        │
        ▼
 continue exactly from the last prose
```

For example:

```text
Completed: 1..42
Remaining: 43..60
Next:      43
```

Then:

```text
Completed: 1..51
Remaining: 52..60
Next:      52
```

The continuation should stay in the same local narrative state: same active characters, scene, location, point of view, tense, and immediate objective unless the existing chapter explicitly supports a transition.

### 8. What Write Chapter sees

```text
┌──────────────────────────────┐
│ CHAPTER OUTLINE              │
│ CHECKLIST / ACTIVE STATE     │
│ RELEVANT CHARACTERS          │
│ RELEVANT WORLD               │
│ PREVIOUS CHAPTER ENDING      │
│ STYLE                        │
│ AUTHOR INTENT                │
└──────────────────────────────┘
```

Write Chapter does not automatically receive the entire project canon, Story Memory, or the UI Chat conversation history.

### 9. What Change Chapter sees

```text
┌──────────────────────────────┐
│ CURRENT CHAPTER              │
│ USER CHANGE REQUEST          │
│ CHAPTER OUTLINE              │
│ CHECKLIST / ACTIVE STATE     │
│ RELEVANT CHARACTERS          │
│ RELEVANT WORLD               │
│ STYLE                        │
│ AUTHOR INTENT                │
└──────────────────────────────┘
```

Change Chapter does not automatically receive the previous chapter, Story Memory, or UI Chat history.

### 10. Context isolation rules

```text
                     WRITE / CHANGE
                            │
       ┌────────────────────┼────────────────────┐
       │                    │                    │
       ▼                    ▼                    ▼
   UI Chat History      Story Memory      Full Project Canon
        ❌                  ❌                    ❌

       Allowed scoped context:

       Chapter Outline      ✅
       Relevant Characters  ✅
       Relevant World       ✅
       Current request      ✅
       Previous chapter end ✅ Write only
       Current chapter      ✅ Change only
```

Chat is intentionally different: the Chat workspace can use broader project information and its own conversation history when its context controls are enabled.

### 11. Canon responsibilities at a glance

```text
                  CANON / PLANNING
                         │
            ┌────────────┴────────────┐
            ▼                         ▼
      Generate Outline          Extend Outline
            │                         │
      Characters / World      Characters / World
      Memory / Outline        Memory / Outline
            │                         │
            └────────────┬────────────┘
                         ▼
                   CHAPTERS
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
    Write Chapter                 Change Chapter
          │                             │
     consume canon                 consume canon
     scoped context                scoped context
          │                             │
     change canon? ❌              change canon? ❌
```

### 12. Token-aware context strategy

The chapter workflows use the model context window for both prompt and output. With a configuration such as `Context Size = 8192` and `Max Tokens per Pass = 4256`, the goal is to reserve enough space for the requested output while keeping input context focused.

```text
                      MODEL CONTEXT
                           8192
                            │
              ┌─────────────┴─────────────┐
              │                           │
           PROMPT                       OUTPUT
              │                           │
              ▼                           ▼
     relevant context only             4256
              │
       ┌──────┼───────┐
       ▼      ▼       ▼
    Outline  Canon   Continuity
       │
       ▼
 Active checklist
```

For continuations, the checklist becomes smaller as the active window advances instead of repeating the full original checklist indefinitely.

### 13. Complete chapter workflow at a glance

```text
                         ┌───────────────┐
                         │    SYNOPSIS   │
                         └───────┬───────┘
                                 │
                         ┌───────▼───────┐
                         │    OUTLINE    │
                         │ Plan +        │
                         │ Continuity    │
                         └───────┬───────┘
                                 │
                       Canon / planning
                                 │
                    ┌────────────┴────────────┐
                    │                         │
                    ▼                         ▼
              WRITE CHAPTER            CHANGE CHAPTER
                    │                         │
                    ▼                         ▼
             Plan + Checklist         Plan + Checklist
                    │                         │
                    ▼                         ▼
             Scoped Characters        Scoped Characters
             Scoped World             Scoped World
                    │                         │
                    ▼                         ▼
               Generation               Rewrite
                    │                         │
                    └────────────┬────────────┘
                                 │
                                 ▼
                              EVAL
                                 │
                    ┌────────────┴────────────┐
                    │                         │
               COMPLETE                 INCOMPLETE
                    │                         │
                    ▼                         ▼
                  SAVE                  ACTIVE ITEMS
                                              │
                                              ▼
                                         CONTINUATION
                                              │
                                              └────↻
```

---

## Context and workflow behavior

### Write Chapter

Write Chapter is driven by the selected chapter outline and the writing workflow. It selects relevant established Characters and relevant World/Setting sections instead of sending the entire project context.

It does not automatically use:

```text
UI Chat history              ❌
Chat Summary                 ❌
Story Memory                 ❌
Entire character database    ❌
Entire world document        ❌
Full project synopsis        ❌
```

For normal next-chapter writing, the ending of the previous chapter may be used as a continuity anchor.

### Change Chapter

Change Chapter is driven by:

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

It does not use the previous chapter as automatic context and does not use UI Chat history or Story Memory as automatic input.

### Checklist continuation behavior

A chapter operation has one planner-generated checklist. When a pass ends because of the output limit, Python keeps the IDs already confirmed and computes the active remainder. The next evaluator and continuation prompt focus on that active remainder.

The evaluator is not instructed to blindly trust prior confirmations. Its judgment is based on the current accumulated chapter text for the active window.

### Chat isolation

Chat has its own conversation state and may use broader project context when enabled. That conversation does not silently become chapter-generation context.

---

## Chat

Chat is the general-purpose AI workspace.

Available controls include:

- **Send**
- **Stop**
- **Insert Chapter**
- **Context: ON/OFF**
- **Clear Chat**

With context enabled, Chat can use the project's synopsis, outline, characters and relationships, world notes, memory, and conversation summary.

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

## Tips

These are practical recommendations rather than hard requirements.

### Keep character relationships detailed

When a character has important relationships, define them explicitly in the **Characters** tab.

Example:

```text
father of → Daniel
rival of → Marcus
friend of → Elena
```

### Make the outline specific

A strong chapter plan should make clear:

- what the chapter needs to accomplish
- which events should happen
- which characters matter
- what continuity must be preserved
- how the chapter should end or transition

### Use Change Chapter for precise revisions

Describe exactly what should change and what must remain unchanged.

### Context size

A practical starting point is **8192 tokens** for **Context Size**. Context Size is the model's combined window for prompt and response.

### Max Tokens per Pass

A practical starting point is **4256 tokens** for **Max Tokens per Pass**. This controls how much the application asks the model to generate in one pass.

### Thinking mode

When supported, **Enable Thinking** can be left off for writing-focused generation when you want more of the available output budget to go to the actual story response.

### Save manual edits

Whenever a section has a **Save** button, use it after manual changes. This is especially important for Synopsis, Outline, World, Memory, and chapter editing.

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

AI Story Studio can also be used with MoE models. The application exposes MoE-related performance settings in **Settings → MoE Performance** for compatible setups.

The model choice changes; the writing workflow remains the same.

---

## Quick Start

### Requirements

- Python **3.10+**
- PySide6
- cryptography
- A compatible local **GGUF** model with `llama-cpp-python` for AI text generation
- Image-generation backend and model configuration when using **Images**
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
2. Click **Save App Settings**.
3. Open **Models** and assign a GGUF model to the tasks you plan to use.
4. Create a project from **Projects → + New**.
5. Write or generate the **Synopsis** and save manual changes.
6. Build the **Outline** and save manual changes.
7. Review **Characters**, **World**, and the **Author Profile** as needed.
8. Open **Story → Chapters** and generate chapters from the outline.
9. Use **Chat**, **Images**, **Stats**, **Search**, and **Console** as needed.
10. Export the finished manuscript with **Export Book**.

---

## Current scope

AI Story Studio is a local desktop application. Language and image models are **not bundled** with the repository and must be provided by the user.

The main writing workflow is the **Story** workspace. Image generation is functional, but it remains a standalone companion tool rather than part of the automatic story-generation pipeline.

The current chapter workflow is centered on:

```text
Outline
  ↓
Chapter Plan + Continuity
  ↓
Scoped Characters + Scoped World
  ↓
Write / Change Chapter
  ↓
Checklist Evaluation
  ↓
Continue until complete
```

The Chat workspace and Story Memory remain separate sources of assistance and state.

This README describes the current UI and workflow behavior of the development branch. Behavior, UI details, and supported models may change before a stable release.

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
