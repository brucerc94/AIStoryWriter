# AI Story Studio

A desktop application for writing AI-assisted novels, powered by local language models via **llama-cpp-python**. Run everything on your own machine — no cloud, no API keys, no data leaves your computer.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Features

- **Full novel pipeline** — guided workflow from synopsis → outline → chapters → review, all in one app
- **Multi-agent architecture** — separate Writer, Reviewer, and Memory agents, each assignable to a different GGUF model
- **Per-task model assignment** — use a fast model for summaries and a larger one for prose; temperatures are configurable per task
- **Streaming output** — tokens stream into the UI in real time; the interface never freezes
- **Human-in-the-loop approvals** — the app pauses after outline and chapter review steps so you can approve or reject before continuing
- **Story memory** — the app auto-summarizes older context to keep the model focused without losing continuity
- **Chat panel** — talk directly to any loaded model while working on your project
- **Export** — save the finished novel as `.docx` or `.pdf`
- **GPU-aware loading** — auto-detects NVIDIA cards and handles GTX 16-series / MoE models correctly (GGML_CUDA_FORCE_MMQ, flash-attention toggle, MoE expert offloading)
- **Project management** — create, open, and organize multiple novels as independent projects
- **Dark theme UI** built with PySide6

---

## Screenshots

> *(Add screenshots here once the app is running)*

---

## Requirements

| Dependency | Purpose | Required |
|---|---|---|
| Python 3.10+ | Runtime | ✅ |
| PySide6 | Desktop UI | ✅ |
| llama-cpp-python | Local LLM inference | ✅ (for inference) |
| python-docx | Export to DOCX | Optional |
| reportlab | Export to PDF | Optional |

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-username/AIStoryStudio.git
cd AIStoryStudio

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install required dependencies
pip install PySide6

# 4. Install llama-cpp-python (CPU build)
pip install llama-cpp-python

# For GPU acceleration (CUDA):
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python --force-reinstall --no-cache-dir

# 5. (Optional) Install export dependencies
pip install python-docx reportlab
```

---

## Usage

```bash
python main.py
```

### Getting started

1. **Settings** — point the app at your folder of `.gguf` model files and configure GPU layers / thread count.
2. **Model Assignments** — assign a GGUF model to each task type (chat, synopsis, outline, chapter writing, review, memory update, etc.). You can use the same model for all tasks or mix and match.
3. **New Project** — create a project, give your novel a title and genre, and add characters.
4. **Write** — use the workflow buttons to generate a synopsis, then an outline, then chapters one by one (or all at once). Approve or regenerate each step as you go.
5. **Export** — when your novel is done, export it to DOCX or PDF from the story panel.

---

## Project Structure

```
AIStoryStudio/
├── main.py                  # Entry point
├── agents/
│   ├── manager.py           # Task routing & next-step suggestion
│   ├── writer.py            # Prompt building for writing tasks
│   ├── reviewer.py          # Prompt building for review tasks
│   └── memory.py            # Story memory management
├── engine/
│   ├── chat.py              # LLM engine wrapper (llama-cpp-python)
│   ├── workflow.py          # Full pipeline orchestration (QThread)
│   ├── models.py            # Data models (Project, Chapter, Character…)
│   ├── context.py           # Context window management & summarization
│   ├── storage.py           # Project persistence (JSON on disk)
│   ├── export.py            # DOCX / PDF export
│   ├── gguf_meta.py         # GGUF metadata reader (MoE detection)
│   └── llama_features.py    # Runtime introspection of llama-cpp-python
└── ui/
    ├── main.py              # MainWindow, tab layout
    ├── story.py             # Story writing panel
    ├── chat.py              # Chat panel
    ├── projects.py          # Project manager panel
    ├── settings.py          # App settings & model assignments panel
    ├── widgets.py           # Shared custom widgets
    └── styles.py            # Global stylesheet & color tokens
```

---

## Workflow Pipeline

```
Idea → Synopsis → Outline → [Review] → Chapters → [Review per chapter] → Memory update → Export
```

Each step runs in a background thread so the UI stays responsive. Steps that require judgment (outline review, chapter review) pause and show the generated content for your approval before proceeding.

---

## Model Tips

- Any GGUF model compatible with llama-cpp-python works.
- For best results, use a **7B–13B instruct model** for chapter writing and a smaller/faster model for summaries and memory updates.
- If you have a GTX 16-series GPU (e.g. GTX 1660), the app will automatically disable flash attention and force MMQ kernels — no manual configuration needed.
- MoE models (Mixtral, Qwen-MoE, etc.) are auto-detected and loaded with optimized batch sizes and expert offloading when available.

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you'd like to change.

1. Fork the repo
2. Create your branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

---

## License

MIT — see [LICENSE](LICENSE) for details.