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
    <img src="https://img.shields.io/badge/License-MIT-0F7660" alt="MIT License" />
  </p>

  <p>
    <a href="#demo">Demo</a> ·
    <a href="#screenshots">Screenshots</a> ·
    <a href="#how-it-works">How it works</a> ·
    <a href="#using-the-ui">Using the UI</a> ·
    <a href="#recommended-models">Recommended models</a> ·
    <a href="#quick-start">Quick Start</a>
  </p>
</div>

> **Status: In Development** — AI Story Studio is already usable, but it is **not a final or stable release yet**. The interface, workflows, model support, and behavior may continue to change while development progresses.

> **Documentation policy:** this README describes the **current graphical interface**. Internal classes, backend APIs, development scaffolding, prompt files, unexposed workflow tasks, and planned features are intentionally omitted.

---

## What is AI Story Studio?

AI Story Studio is a desktop application for writers who want to work with **local AI models** instead of relying on a hosted writing service.

It brings the main stages of novel creation into one workspace: story planning, characters, world notes, chapters, contextual chat, local image generation, project organization, statistics, search, and export.

The idea is deliberately simple: **you follow the workflow, and the AI helps at each stage.**

## How it works

The main writing process is intentionally straightforward:

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
Edit / Review / Continue
      ↓
Finish the Book
      ↓
Export
```

You do not need to understand agents, APIs, pipelines, or prompt files to use the application. The UI handles the underlying context and model calls for you.

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
| 🧭 **Outline** | Edit, generate, extend, and track planned chapters. |
| 👤 **Characters** | Manage character details, relationships, and AI-generated portraits. |
| 🌍 **World** | Keep world and setting notes manually. |
| 📖 **Chapters** | Add, generate, read, edit, change, save, mark ready, and delete chapters. Generate the remaining book from the outline. |
| 🧠 **Memory** | Maintain story memory automatically during chapter generation while keeping manual editing available. |
| 🎨 **Author** | Define creative intent and writing-style preferences. |
| 💬 **Chat** | Ask questions, brainstorm, request writing help, attach a chapter, and control story context. |
| 🖼️ **Images** | Generate book covers, scenes, locations, objects/items, and character portraits. Image generation currently operates as a separate tool from the main writing workflow. |
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

You can type the synopsis yourself or click **Generate Synopsis**. A synopsis should establish the premise, main characters, central conflict, and stakes.

After generation, read through it and edit anything you want before continuing.

### 5. Build the Outline

Open **Story → Outline**.

Click **Generate Outline** to open the outline setup dialog. There you can choose the number of chapters and optionally define the author's creative intent and writing-style preferences used for the generation.

The outline is then shown in the editor and can still be edited manually.

You can also use **Extend Outline** when you want to append additional chapters to an existing outline.

The outline uses chapter headings such as:

```markdown
## Chapter 1: Title
## Chapter 2: Title
## Chapter 3: Title
```

### 6. Add Characters

Open **Story → Characters**.

Click **+ Add Character** to create a character. You can provide:

- Name
- Role
- Physical description
- Backstory
- Traits
- Relationships with other characters

Characters can later be edited or deleted. A character can also have an AI-generated portrait.

### 7. Add World & Setting information

Open **Story → World**.

Use this space for the information the story needs to remain consistent: locations, rules, history, politics, culture, technology, magic systems, or other setting details.

This section is intentionally manual in the current UI.

### 8. Define the Author profile

Open **Story → Author**.

The **Author Profile** is where you can establish long-term creative preferences such as themes, emotional goals, inspirations, point of view, pacing, dialogue style, description density, violence, romance, genre tags, and target chapter length.

These settings are stored in the project and can also be used by the outline-generation workflow.

### 9. Generate Chapters

Open **Story → Chapters**.

After an outline exists, select a chapter from the chapter list.

There are three different generation actions:

| Action | What it does |
|---|---|
| **Generate Chapter** | Generates the **currently selected chapter**. |
| **Generate Next Chapter** | Finds the first chapter in the outline that still has no content and generates it. |
| **Generate Full Book** | Generates all remaining chapters from the outline in order. |

This distinction is important. **Generate Chapter** works on the chapter you selected; it does not mean “create the next chapter.”

Once a chapter has content, you can switch between **Read** and **Edit** mode, save changes, use **Change Chapter**, mark it ready, or delete it.

The book-style reader lets you move between chapters and pages without modifying the stored chapter text.

### 10. Use Story Memory

Open **Story → Memory** to inspect the story memory maintained by the application.

During chapter generation, the application can update memory automatically. The memory page also allows manual editing when needed.

### 11. Use Chat when you want assistance

The top-level **Chat** tab is a general-purpose writing assistant.

You can send questions, brainstorm ideas, ask for writing help, enable or disable project context, and attach a chapter to the conversation.

With context enabled, the chat can use information from the current project such as the synopsis, outline, characters and relationships, world notes, memory, and conversation summary.

### 12. Generate images separately

The top-level **Images** tab provides local image generation for:

- Book Cover
- Scene Illustration
- Location
- Object / Item
- Character portraits through the character workflow

Image generation **works**, but it is currently a **separate tool and is not yet integrated into the main synopsis → outline → characters/world → chapters workflow**. Generated images are saved inside the active project.

Open **Images**, enter a prompt, optionally set a negative prompt, seed, dimensions, steps, and CFG, then click **Generate**.

### 13. Check progress and find text

Use **Stats** to see project progress, chapter counts, word counts, reading-time information, and review status.

Use **Search** to find text across the project.

### 14. Export the finished book

Use **Export Book** from the top-right of the application window.

The current UI supports:

- Word (`.docx`)
- PDF (`.pdf`)
- Markdown (`.md`)
- Plain text (`.txt`)

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

## Story workspace reference

### Synopsis

Write manually or use **Generate Synopsis** to create a draft that you can refine.

### Outline

Edit the outline directly, generate it from the outline wizard, or use **Extend Outline** to append more chapters.

### Characters

Create and edit characters, relationships, and optional portraits.

### World & Setting

Store setting information manually so the story has a stable reference for later generations.

### Author

Maintain the project's creative intent and writing-style preferences.

### Chapters

Generate individual chapters, generate the next unfinished chapter, or generate the remaining book. Read, edit, review, change, save, mark ready, and delete chapters from the chapter workspace.

### Memory, Stats & Search

Use Memory for accumulated story state, Stats for project progress, and Search for text lookup.

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

All generated images are saved encrypted inside the active project.

---

## Models & Settings

The **Models** tab is where you assign local GGUF models to the task types exposed by the application.

The **Settings** tab contains the current UI controls for model and hardware configuration, generation behavior, response language, maximum tokens per pass, mature/unrestricted content, diagnostics, MoE performance, and image-generation configuration.

---

## Console

The Console is an advanced diagnostics view for runtime output and generation information such as token count, speed, elapsed time, time to first token, current state, and other runtime details.

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
2. Open **Models** and assign a GGUF model to the tasks you plan to use.
3. Create a project from **Projects → + New**.
4. Open **Story → Synopsis** and write or generate the synopsis.
5. Open **Story → Outline** and generate or write the outline.
6. Add **Characters**, **World**, and an **Author Profile** as needed.
7. Open **Story → Chapters** and generate chapters from the outline.
8. Use **Chat**, **Images**, **Stats**, **Search**, and **Console** as needed.
9. Export the finished manuscript with **Export Book**.

---

## Current scope

AI Story Studio is a local desktop application. Language and image models are **not bundled** with the repository and must be provided by the user.

The main writing workflow is the **Story** workspace. Image generation is functional, but it is currently a standalone companion tool rather than part of the automatic story-generation pipeline.

This README intentionally follows the **UI currently shipped**. New user-facing features should be added here when they are actually visible and usable in the application.

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
