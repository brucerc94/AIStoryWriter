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
    <a href="#how-it-works">How it works</a> ·
    <a href="#features">Features</a> ·
    <a href="#using-the-ui">Using the UI</a> ·
    <a href="#workflow-architecture">Architecture</a> ·
    <a href="#quick-start">Quick Start</a>
  </p>
</div>

> **Status: In Development** — AI Story Studio is usable, but it is not a final or stable release yet. The UI, workflows, model support, and behavior may continue to change during development.

---

## What is AI Story Studio?

AI Story Studio is a desktop application for writers who want to build novels with **local AI models** rather than a hosted writing service.

The application combines story drafting, outline planning, character and world management, chapter generation and revision, author preferences, contextual chat, memory, local image generation, statistics, search, and export in one workspace.

The writing pipeline is deliberately separated into stages so that each model call has a clear job and receives only the context it needs.

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
Memory / Finalization
      ↓
Export
```

The important distinction is that **Synopsis / Draft is the author's story source and normalization stage**, while **Outline is the structured chapter blueprint** used to drive chapter generation.

---

## Synopsis / Draft

The Story tab now treats the Synopsis area as **Synopsis / Draft**.

The user can write or paste raw story material directly into the editor and press **Generate Draft**. The current editor text is submitted directly, so the user does not need to save it first for the generation request to use it.

The generation pipeline is:

```text
User story text
      ↓
Consistency check
      ↓
 ┌───────────────┐
 │ Problems found│
 └───────┬───────┘
         │ yes
         ▼
   Repair contradictions
         │
         ▼
   Fresh consistency check
         │
         ▼
      Checked story
         │
         └──────────────┐
                        │
                        ▼
                 Draft development
                        │
                 Author Profile
                        │
                        ▼
                Final Synopsis / Draft
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
        Characters              World
```

The consistency phase checks the user's story for internal problems such as ordering, causal contradictions, character state conflicts, location conflicts, identity conflicts, and incompatible events. When a problem is found, the repair step changes only what is necessary to make the story coherent while preserving the intended events.

The final draft is then developed using the project's **Author Profile**, including creative intent and writing-style preferences. This stage is intentionally a **draft/planning form of the story**, not finished novel prose: it can be richer and more developed than the raw input, but it is not supposed to behave like a finished chapter full of scene-level dialogue.

Only after that final draft is successfully created are Characters and World extracted or updated from it.

### Important behavior

- The user's raw story is the source for this stage.
- Each processing stage uses a fresh inference context.
- A failed repair does not silently replace the user's story with an unsafe result.
- Characters and World are updated only after the final normalized draft exists.
- The internal project field remains `project.synopsis` for compatibility, while the UI presents it as **Synopsis / Draft**.

---

## Outline

The Outline is the application's **chapter blueprint**.

### Generate Outline

`Generate Outline` takes the completed Synopsis / Draft and a requested chapter count, then partitions the story into exactly that many chronological source blocks.

The process is:

```text
Synopsis / Draft
      ↓
Semantic chronological split
      ↓
Exactly N chapter source blocks
      ↓
For each chapter:
      ├── select relevant Characters
      ├── select relevant World
      ├── load full Author Profile
      └── generate detailed chapter treatment
```

The chapter source blocks are kept focused and information-dense. They preserve the story events and causal order so the second-stage chapter writer does not need the full synopsis again.

Before each chapter treatment is generated, the workflow selects only the **relevant Character records and World information for that chapter's source block**. This avoids sending the entire project canon to the model for every chapter.

The chapter treatment itself is intentionally rich: it expands the source block into a detailed narrative plan with character actions, motivations, interactions, important dialogue content, emotional movement, setting-dependent details, turning points, and consequences. It is still a planning artifact, not the finished chapter prose.

### Outline continuation

Long chapter treatments can continue in additional passes. The continuation receives a bounded checkpoint from the previously generated treatment and continues the same chapter without asking the model to regenerate earlier material.

### Generate Outline does not create Characters or World

The current `Generate Outline` flow focuses on the **outline itself**. It does not create or update the Characters and World databases as a side effect.

That separation matters because Characters and World are prepared from the finalized Synopsis / Draft before outline generation, while later outline extensions may update them explicitly from new user-provided story material.

---

## Extend Outline

`Extend Outline` is different from `Generate Outline`.

Instead of re-partitioning the original synopsis, the user supplies new material in the extension dialog. The workflow uses the existing outline and the new user material to build additional chapter plans, while selecting relevant Characters and World for each new chapter from its own source block.

After **all requested extension chapters are successfully generated**, the workflow updates Characters and World using the **user's original extension material**. This lets the extension introduce new characters, relationships, locations, rules, or other world information that was not present in the original Synopsis / Draft.

```text
Existing Outline
      +
User extension material
      ↓
Semantic split into new chapter source blocks
      ↓
For each new chapter:
      ├── relevant Characters
      ├── relevant World
      ├── Author Profile
      └── detailed chapter treatment
      ↓
Complete extension
      ↓
Update Characters + World
      ↓
Save
```

This means the two outline actions serve different purposes:

| Action | Purpose |
|---|---|
| **Generate Outline** | Turn the current Synopsis / Draft into the requested number of chapter source blocks and detailed chapter treatments. |
| **Extend Outline** | Add new chapters from new user-provided material to an existing outline, then update Characters and World from the original extension material. |

---

## Characters and World

Characters and World are reusable project canon.

### Characters

The Characters area stores named characters, roles, descriptions, backstories, traits, and relationships. Character portraits can also be generated locally.

Characters derived from Synopsis / Draft or later outline extension material are merged with existing records rather than being treated as unrelated duplicates whenever the application can identify the same character.

### World

World stores reusable setting information such as geography, culture/customs, history, locations, factions, and other project setting material supported by the current workflow.

The chapter workflows do not need to inject the entire World document. They can select a relevant subset for the current chapter.

---

## Author Profile

The Author area stores the preferences that guide generated story material.

The profile can include creative intent and writing-style choices such as:

- emotional journey and lasting reader impression
- themes and unique elements
- inspirations and things to avoid
- point of view
- pacing
- prose density
- dialogue frequency
- violence and romance level
- genre and other chapter-writing preferences

The full Author Profile is available to the workflows that are designed to apply it, including Synopsis / Draft and chapter outline generation.

---

## Chapter generation

Chapter workflows execute the outline rather than replacing it.

### Write Chapter

`Write Chapter` uses the selected chapter's outline plan plus a planning checklist and the **relevant Characters and World** selected for that chapter. For normal sequential writing, the ending of the previous chapter can also be used for immediate continuity.

The workflow follows a planner → writer → evaluator → continuation pattern. Long chapters can continue through multiple passes while preserving the end-of-chapter checkpoint.

### Change Chapter

`Change Chapter` rewrites an existing chapter according to a user request.

Before planning the change, the user's request can go through an internal consistency precheck. When clear contradictions or impossible sequencing are found inside the request, the request is minimally repaired before the rewrite planner continues.

The chapter rewrite receives scoped project context and does not automatically use the previous chapter or the UI Chat conversation as hidden context.

### Chapter-scoped context

```text
Full Character DB ──────┐
                        ├──> Relevant chapter context ──> Chapter workflow
Full World ─────────────┘

Outline Chapter N ──────────────────────────────────────> Chapter workflow
Author Profile ─────────────────────────────────────────> Chapter workflow
```

The goal is to reduce irrelevant prompt context and keep each generation focused on the current chapter.

---

## Memory

Memory is a separate story-state feature used to preserve important information across the larger workflow.

It can be inspected and edited manually. `Generate Full Book` does not automatically update Story Memory between chapters.

Memory is not silently treated as the same thing as chapter prose. The chapter writing workflows use their defined chapter context rather than relying on UI Chat history.

---

## Chat

The top-level Chat tab is a general-purpose writing assistant.

With story context enabled, Chat can work with broader project information and its own conversation history. You can also attach a chapter to a chat message for focused discussion.

Chat history remains separate from the controlled context used by the dedicated chapter-writing workflows.

---

## Features

| Area | What it does |
|---|---|
| 📚 **Projects** | Create, open, search, rename, and delete projects. |
| ✍️ **Synopsis / Draft** | Write or paste raw story material, validate consistency, repair contradictions when needed, develop a structured draft with the Author Profile, then build Characters and World from the finalized draft. |
| 🧭 **Outline** | Generate a fixed number of chronological chapter source blocks, turn them into detailed chapter treatments, edit them, and extend an existing outline. |
| 👤 **Characters** | Manage characters, traits, backstories, relationships, and portraits. |
| 🌍 **World** | Manage reusable setting and world information. |
| 📖 **Chapters** | Generate chapters, continue them, review them, change them, edit them manually, and generate the remaining book. |
| 🧠 **Memory** | Maintain reusable story-state information. |
| 🎨 **Author** | Define creative intent and writing-style preferences. |
| 💬 **Chat** | General writing assistance with optional project context and chapter attachment. |
| 🖼️ **Images** | Generate book covers, scene illustrations, locations, objects/items, and character portraits locally. |
| 📊 **Stats** | Inspect word counts, chapter counts, reading-time information, review state, and progress. |
| 🔎 **Search** | Search project content with normal, case-sensitive, or regex-based matching. |
| ⚙️ **Models & Settings** | Configure local GGUF models, context size, GPU layers, threads, generation parameters, language, and other exposed options. |
| 🖥️ **Console** | Inspect runtime logs and generation diagnostics. |
| ⇩ **Export Book** | Export the finished book to Word, PDF, Markdown, or plain text. |

---

## Using the UI

### 1. Create or open a project

Use the **Projects** panel to create a new project or continue an existing one.

### 2. Configure local models

Open **Settings** and configure the models directory and hardware/generation settings. Open **Models** to assign GGUF models to the tasks you intend to use.

### 3. Write the story in Synopsis / Draft

Go to **Story → Synopsis / Draft** and write or paste your story material into the editor.

Press **Generate Draft** to run the normalization pipeline. The current text in the editor is sent directly to the workflow, even when it has not been saved manually yet.

When the operation finishes, the result becomes the project's normalized Synopsis / Draft and Characters/World are updated from that final result.

### 4. Generate the Outline

Go to **Story → Outline**, choose the requested number of chapters, and run **Generate Outline**.

Each chapter is generated from its own source block with relevant canon and Author Profile context.

### 5. Extend the Outline

Use **Extend Outline** when you want to add new chapter material to an existing outline. Provide the new story information in the extension dialog.

### 6. Review Characters and World

Inspect the Characters and World tabs after the draft or outline-extension workflow has updated them. Manual edits remain available at any time.

### 7. Write chapters

Go to **Story → Chapters**, select a chapter, and use the available generation actions.

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

Use **Export Book** from the application window to create the final manuscript in one of the supported formats.

---

## Workflow Architecture

The current design separates source drafting, story structure, canon, and chapter execution.

### End-to-end pipeline

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
                           ▼
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
            ┌──────────────┴──────────────┐
            ▼                             ▼
     semantic chapter split        relevant canon
            │                             │
            └──────────────┬──────────────┘
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

### Generate Outline context flow

```text
Final Synopsis / Draft
        │
        ▼
Semantic split into exactly N blocks
        │
        ├──────── Chapter 1 source ──> relevant Characters + World ──┐
        ├──────── Chapter 2 source ──> relevant Characters + World ──┤
        ├──────── Chapter 3 source ──> relevant Characters + World ──┤
        └──────── Chapter N source ──> relevant Characters + World ──┘
                                                                     │
                         Author Profile ─────────────────────────────┤
                                                                     ▼
                                                     Chapter treatment generation
                                                                     │
                                                                     ▼
                                                           Bounded continuation
```

### Chapter writing context

```text
               Outline Chapter N
                      │
                      ├── Chapter plan
                      ├── Continuity
                      ├── Planning checklist
                      └── Previous chapter ending* 
                              │
                              ▼
                  Relevant Characters / World
                              │
                              ▼
                       Author Profile
                              │
                              ▼
                       Chapter writer
                              │
                         evaluator
                       ┌──────┴──────┐
                       ▼             ▼
                   complete     incomplete
                                    │
                                    ▼
                               continuation
```

`*` Previous-chapter continuity is used by the normal sequential Write Chapter flow where applicable; it is not automatically injected into Change Chapter.

---

## Local-first design

The application is designed around local inference with GGUF models through `llama-cpp-python`.

The model layer can unload and reload models as tasks change, supports GPU layer configuration, CPU thread settings, and exposes generation controls such as temperature, Top P, and Top K.

The application also includes hardware-aware behavior for local CUDA setups and model-specific handling for supported MoE configurations.

---

## Images

The Images area provides separate local image-generation workflows for:

- character portraits
- book covers
- scene illustrations
- locations
- object/item images

Image generation is intentionally separate from the text-generation pipeline so a story-writing operation does not require an image model.

---

## Quick Start

### Requirements

- Python 3.10+
- PySide6
- `llama-cpp-python`
- Local GGUF language models
- Optional CUDA-capable GPU configuration for accelerated inference

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

## Development status

AI Story Studio is an active development project. The workflow architecture is evolving, and local-model behavior can vary by model family, quantization, context size, and hardware.

The repository contains automated tests covering important workflow, context, continuation, and outline behaviors.

---

## License

MIT