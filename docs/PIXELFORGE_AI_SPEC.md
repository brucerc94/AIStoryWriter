# PixelForge AI — Product Specification and Engineering Plan

> **Status:** Specification / implementation contract  
> **Project type:** Local-first, agent-driven Aseprite control application  
> **Primary stack:** Python 3.10+, PySide6, llama-cpp-python, GGUF, Lua extension, WebSocket  
> **Working name:** PixelForge AI  
> **Reference behavior:** https://github.com/with-pebbly/aseprite-ai-artist  
> **Existing local-model reference:** https://github.com/brucerc94/AIStoryWriter

---

## 1. Purpose

Build a Python application that behaves like Aseprite AI Artist: a capable agentic control layer for Aseprite, not merely an AI image generator or a chat UI.

The user issues a request in natural language. The application inspects the actual Aseprite document, chooses a workflow, plans a sequence of safe structured operations, executes them against the editor, checks the resulting pixels, corrects specific defects, validates the result, and reports what was actually completed.

The application must work with local GGUF models through llama-cpp-python. It must not require cloud inference, an OpenAI API key, Ollama, OpenCode, Claude Code, or Node.js at runtime.

Python owns the orchestration, model access, tool schemas, validation, knowledge retrieval, local service, project state, and UI. A small Lua extension inside Aseprite performs editor operations that must execute through Aseprite's scripting API. The extension is a transport and command adapter, not a second AI implementation.

The goal is functional parity with the reference project's core behavior over time, while keeping the implementation native to Python and maintainable. Do not claim complete feature parity until each capability exists and passes its acceptance tests.

## 2. Non-goals

- Do not modify or merge into AIStoryWriter. It remains a separate project.
- Do not create a standalone raster editor that imitates Aseprite but cannot operate on the real Aseprite document.
- Do not rely on a model generating an entire PNG as the only drawing method.
- Do not let the model execute arbitrary Python, shell commands, or arbitrary Lua by default.
- Do not require a second model for critic/reviewer roles. Logical agents share one loaded model initially.
- Do not build headless rendering first. Live-editor operation is the first milestone; headless mode is a later compatible adapter.
- Do not introduce a database, a complex plugin framework, or speculative abstractions without a concrete requirement.
- Do not silently resize a canvas, flatten layers, change color mode, overwrite files, or discard unsaved user work.

## 3. Product principles

1. **Real editor control:** operations must be executed in the actual open Aseprite document during live mode.
2. **Inspect before edit:** query the document, canvas dimensions, palette, layers, frames, tags, and relevant pixels before drawing.
3. **Plan before pixel changes:** define canvas constraints, palette, light direction, view, target layers, and expected frames.
4. **Batch operations:** one logical drawing action should map to one editor transaction / undo step whenever feasible.
5. **Draw, look, fix, validate:** a tool success response is not proof of visual quality.
6. **Minimal correction:** fix only the regions implicated by review rather than redrawing correct work.
7. **Explicit failure:** when disconnected or uncertain, stop and report the failure. Never silently fall back to modifying a file behind the live editor.
8. **Local-first:** all prompts, model inference, pixel analysis, and project storage stay on the user's machine unless the user explicitly exports or shares a file.
9. **One model initially:** specialist agents are separate behaviors, prompts, and tool permissions—not separate model instances.
10. **Deterministic code for deterministic work:** Python/Lua, not the LLM, must handle grid dimensions, color conversion, pixel loops, geometry checks, validation, diffs, and protocol framing.
11. **User control:** preserve undo, expose action history, support cancellation, and request confirmation for destructive or high-impact operations.
12. **Traceability:** every action has a validated input, structured result, log entry, and defined error state.

## 4. Technical foundation from AIStoryWriter

AIStoryWriter already includes useful patterns and code around local inference. The new project must reuse the designs and, where safe and practical, selected engine components instead of independently reinventing GGUF configuration.

Reviewed areas include:

- `engine/chat.py`: `LLMEngine`, GGUF loading/unloading, chat completions, streaming, cancellation checks, hardware-aware settings.
- `engine/gguf_meta.py`: metadata-only inspection of GGUF model architecture and expert information.
- `engine/llama_features.py`: runtime capability detection for installed `llama-cpp-python` APIs.
- `engine/models.py`: typed data models and dataclass conventions.
- `engine/storage.py`: file-based persistence and separation of storage from UI.
- `engine/workflow.py`: background workflow execution with Qt threading/signals.
- `ui/main.py`, `ui/settings.py`, `ui/styles.py`: PySide6 application structure, configurable model settings, and a dark theme.
- `tests/`: automated tests for context integrity, workflow boundaries, and regression prevention.

Reference repository: https://github.com/brucerc94/AIStoryWriter

### Reuse policy

- First inspect imports and public interfaces before copying anything.
- Do not import AIStoryWriter modules by referencing a sibling directory or relying on the user's current working directory.
- Do not add AIStoryWriter as a runtime dependency unless the user deliberately extracts a shared package.
- Initially implement a small `ChatModelPort` adapter against the existing `LLMEngine` interface. Keep a clean boundary so an extracted shared package can replace the adapter later.
- Copy only code whose license permits it and preserve required notices.
- Keep the AIStoryWriter repository unchanged.
- Create independent tests for the behavior needed by PixelForge AI.
- Do not presume that the existing chat generator already handles agent tool-calls. Verify the installed API, chat template, tool-call format, and parsing behavior.

## 5. High-level architecture

Use a dependency direction similar to Clean Architecture:

```text
PySide6 UI / ViewModels
        |
        v
Application Use Cases and Workflow Orchestrator
        |
        +-------------------+--------------------+
        |                   |                    |
        v                   v                    v
Agent Runtime        Pixel-Art Services     Project/History Services
        |                   |                    |
        v                   v                    v
ChatModelPort       Tool Registry          Repository Interfaces
        |                   |                    |
        v                   v                    v
llama-cpp-python    Tool Validators        JSON/Markdown Storage
        |
        v
One locally loaded GGUF model

Aseprite Client (Python WebSocket)
        |
        v
Local Bridge / Command Router
        |
        v
Lua Extension inside Aseprite
        |
        v
Aseprite document, layers, cels, frames, palette, tags, export
```

The UI must never instantiate `Llama`, generate prompts, compile grids, or send raw protocol messages directly. Agents must never access Qt widgets. Domain models must not import Qt, llama.cpp, WebSocket libraries, or Lua details.

### Main layers

**Domain**
- Pure data types, constraints, and domain-level validation.
- No Qt or I/O dependencies.
- Types: `SpriteDocumentInfo`, `CanvasSize`, `Palette`, `PixelGrid`, `LayerInfo`, `FrameInfo`, `AnimationTag`, `DrawingPlan`, `PixelArtFinding`, `ToolAction`, `WorkflowRun`.

**Application**
- Use cases and workflows.
- Coordinates model requests, tool execution, review rounds, cancellation, history, and save/export confirmation.
- Depends on interfaces, not on concrete UI or transport implementations.

**Infrastructure**
- llama-cpp-python adapter.
- WebSocket client/bridge and message protocol.
- Lua extension deployment/package.
- File-based project storage.
- Export and diagnostic services.

**Agents**
- Specialized policies and workflows using one shared model runtime.
- Agents can call only explicitly granted tools.
- Every agent returns typed data or validated tool proposals, not arbitrary executable code.

**Knowledge**
- Versioned Markdown rules, task skills, templates, palette data and checklists.
- Loaded selectively per task; never inject the entire knowledge base into every prompt.

## 6. Recommended repository layout

```text
PixelForgeAI/
├── README.md
├── SPEC.md
├── AGENTS.md
├── CHANGELOG.md
├── LICENSE
├── requirements.txt
├── setup.bat
├── run.bat
├── main.py
├── pyproject.toml
├── .gitignore
├── config/
│   └── defaults.toml
├── assets/
│   ├── icon/
│   └── ui/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── INSTALL.md
│   ├── SECURITY.md
│   ├── TOOLS.md
│   ├── WORKFLOWS.md
│   ├── DEVELOPMENT.md
│   └── screenshots/
├── extension/
│   └── pixelforge.lua
├── knowledge/
│   ├── README.md
│   ├── rules/
│   ├── skills/
│   ├── templates/
│   └── palettes/
├── src/
│   └── pixelforge/
│       ├── __init__.py
│       ├── bootstrap.py
│       ├── domain/
│       │   ├── models/
│       │   ├── protocols/
│       │   └── validation/
│       ├── application/
│       │   ├── use_cases/
│       │   ├── workflows/
│       │   ├── services/
│       │   └── ports/
│       ├── agents/
│       │   ├── studio_agent.py
│       │   ├── brief_agent.py
│       │   ├── concept_agent.py
│       │   ├── drawing_agent.py
│       │   ├── palette_agent.py
│       │   ├── shading_agent.py
│       │   ├── rig_agent.py
│       │   ├── animation_agent.py
│       │   ├── review_agent.py
│       │   └── repair_agent.py
│       ├── tools/
│       │   ├── registry.py
│       │   ├── schemas.py
│       │   ├── session_tools.py
│       │   ├── inspect_tools.py
│       │   ├── drawing_tools.py
│       │   ├── palette_tools.py
│       │   ├── structure_tools.py
│       │   ├── transform_tools.py
│       │   ├── asset_tools.py
│       │   ├── validation_tools.py
│       │   └── export_tools.py
│       ├── infrastructure/
│       │   ├── llm/
│       │   │   ├── chat_model_port.py
│       │   │   ├── llama_cpp_adapter.py
│       │   │   ├── model_manager.py
│       │   │   ├── tool_call_parser.py
│       │   │   └── context_manager.py
│       │   ├── aseprite/
│       │   │   ├── client.py
│       │   │   ├── bridge.py
│       │   │   ├── protocol.py
│       │   │   ├── errors.py
│       │   │   └── extension_installer.py
│       │   ├── storage/
│       │   │   ├── project_repository.py
│       │   │   ├── settings_repository.py
│       │   │   └── history_repository.py
│       │   ├── export/
│       │   └── logging/
│       ├── pixelart/
│       │   ├── grid.py
│       │   ├── color.py
│       │   ├── palette.py
│       │   ├── render.py
│       │   ├── diff.py
│       │   └── validators.py
│       ├── knowledge/
│       │   ├── loader.py
│       │   └── selector.py
│       └── ui/
│           ├── main_window.py
│           ├── controllers/
│           ├── viewmodels/
│           ├── panels/
│           ├── widgets/
│           ├── dialogs/
│           ├── resources.py
│           └── styles/
└── tests/
    ├── unit/
    │   ├── test_grid.py
    │   ├── test_palette.py
    │   ├── test_color.py
    │   ├── test_schema_validation.py
    │   ├── test_protocol.py
    │   ├── test_tool_call_parser.py
    │   └── test_pixelart_rules.py
    ├── integration/
    │   ├── test_llm_adapter.py
    │   ├── test_aseprite_client.py
    │   └── test_workflows.py
    └── e2e/
        └── test_live_sprite_workflow.py
```

The layout is a target organization, not a mandate to create empty modules. Add a module when there is concrete behavior to put in it. Keep the first working implementation small, but do not collapse unrelated responsibilities into one file.

## 7. Aseprite integration architecture

Aseprite's Lua scripting API is the in-editor execution side. The Python application must communicate with an extension that connects to a local WebSocket bridge; the bridge accepts requests, routes them to the attached editor, and returns structured replies.

### Live mode

- Bind listening sockets to `127.0.0.1` only.
- Use separate configurable ports for editor-to-bridge and Python-client-to-bridge connections.
- Include a protocol version and capability handshake.
- Use monotonically unique request IDs.
- Route responses to the request that originated them.
- Serialize operations that mutate the same document.
- Support connection state, reconnect backoff, timeout handling, and explicit cancellation boundaries.
- Provide `preflight` / `health_check` before every workflow.
- If the editor disconnects, stop mutating operations and return `not_connected`. Never silently switch to writing a saved file.
- Keep the bridge independent from the UI lifetime where practical, to avoid losing the editor connection when the window is restarted.
- Never bind to `0.0.0.0` by default.

### Extension responsibilities

- Register a finite table of allowed named commands.
- Validate command arguments again on the editor side.
- Execute operations through documented Aseprite APIs.
- Batch related changes into a single undoable transaction whenever supported.
- Return structured success or error payloads.
- Report active sprite/document state after changes when needed.
- Avoid arbitrary shell/process calls.
- Never load model code or construct prompts.
- Check whether a requested target layer/frame exists before mutating it.
- Respect unsaved edits and current document state.

### Protocol contract

Each request should include:

```json
{
  "protocol_version": 1,
  "request_id": "unique-id",
  "command": "draw.grid",
  "arguments": {},
  "timeout_ms": 15000
}
```

Success response:

```json
{
  "protocol_version": 1,
  "request_id": "unique-id",
  "ok": true,
  "result": {}
}
```

Failure response:

```json
{
  "protocol_version": 1,
  "request_id": "unique-id",
  "ok": false,
  "error": {
    "code": "invalid_args",
    "message": "Human-readable explanation",
    "details": {}
  }
}
```

Protocol requirements:
- UTF-8 JSON messages.
- Maximum message size configurable and bounded.
- Validate all inputs and reject unknown commands.
- Stable error codes such as `not_connected`, `invalid_args`, `not_found`, `timeout`, `conflict`, `unsupported`, `internal_error`.
- Do not expose Python tracebacks, filesystem secrets, or environment variables to the model.
- Requests have an idempotency policy: read-only requests can be retried; mutating commands must not be blindly retried after an ambiguous timeout.
- If a mutating request times out, query document state before retrying.

## 8. Functional requirements and tool surface

Implement tools with strict, typed schemas. Prefer compact tools whose verbs are explicit and whose output is structured. Tools must return actionable errors the model can use to recover from a rejected request.

### 8.1 Session and document

- `preflight`: report connection, extension version, capabilities, mode and readiness.
- `sprite_info`: dimensions, color mode, palette, active layer/frame, layers, cels, frame durations, tags, selection, dirty/unsaved state if the API exposes it.
- `sprite_manage`: list, create, open, activate, save, save_as, close, set properties, resize canvas, slices.
- `read_pixels`: query distinct colours and row-major pixel values for a bounded region or frame.
- All document coordinates are integer pixel coordinates; frame indices are 1-based at tool boundaries and mapped consistently in the adapter.

### 8.2 Look / inspect

- `look.preview`: nearest-neighbour upscale, transparent checkerboard and a bounded image response.
- `look.ascii`: exact one-glyph-per-pixel rows, stable palette-index legend, origin, coordinate information.
- `look.filmstrip`: render all relevant frames into one bounded preview.
- `look.diff`: pixel-level changes between two frames or snapshots, including changed bounding box and percentage changed.
- `look.onion`: compositing view of a target frame and ghosted neighbours.
- `look.compare`: reference and art side-by-side without mutating visibility in the live document.
- Inspection of layers/regions must never alter user art.

### 8.3 Drawing

Provide batch drawing operations:
- `pixels`: explicit coordinate/color edits, bounded list size.
- `line`, `polyline`.
- `rect`, `ellipse`, optional fill.
- `fill`: seed point, exact/tolerance mode, contiguous flag.
- `replace`: replace color globally or in a region.
- `clear`: bounded region or explicit whole-cel operation.
- `blit`: copy a region with flips/transparent handling.
- `grid`: textual grid, legend and transparent policy.
- `dither`: controlled patterns such as checker/Bayer/noise with seed.
- `gradient`: discrete bands with a bounded number of steps.
- `text`: bitmap-font rendering, with metrics before mutation.

Batch requirements:
- One logical draw request = one editor transaction where feasible.
- Bound points/operations/region sizes.
- Preserve palette policy by default; snap or refuse off-palette colours according to explicit settings.
- Enforce canvas and region bounds before sending to Lua.
- Never silently pad/truncate malformed grid rows.
- Transparent glyph behavior must be explicit: erase or skip.
- Report painted, skipped, erased, and changed-pixel counts.
- Keep an undoable history in Aseprite.
- Prefer a grid for small sprites (typically <= 48x48); use geometric/region operations for large regular forms.

### 8.4 Selection and transform

- Select none/all/rectangle/ellipse/color/invert/grow/shrink where the API supports it.
- Transform one cel by translating, flipping, rotating, scaling, outlining or cropping to content.
- Clearly state when an operation is cel-scoped and reject ambiguous layer/frame targets.
- Any lossy transformation must be explicit and included in the result report.

### 8.5 Palette and colour

- Get/set palette; load common palette formats where supported; choose preset; create colour ramps; analyze; extract from reference art.
- Provide palette search and a bundled small curated set first. Large catalogues can be added separately with their own attribution/data source.
- Convert RGB ↔ perceptual color space in deterministic Python code.
- Palette locking is enabled by default.
- Compute nearest palette entry by perceptual distance and report the movement/error.
- Analyse luminance contrast, duplicate colours, unused entries and off-palette pixels.
- Never modify an existing palette without previewing the effect and obtaining confirmation if existing pixels may be changed.

### 8.6 Layers, cels, frames and tags

- List/create/rename/delete/reorder/activate layers; visibility, opacity, blend, lock and groups where supported.
- List/add/duplicate/delete/reorder/activate frames; set per-frame duration.
- Create/move/copy/link/unlink/clear/delete cels where supported.
- Create/update/delete animation tags.
- Batch rig setup for characters with named body parts.
- Confirm destructive actions such as layer merge/delete or frame deletion.
- Do not automatically flatten the sprite.
- Validate expected layer occupancy, frame ranges, overlap constraints and tag coverage.

### 8.7 References and assets

- Import an image as a named, locked, semi-transparent reference layer.
- List/remove reference layers.
- Sample a palette from a reference region.
- Split storyboard sheets into panels and align panels to target frames when dimensions are valid.
- Do not scrape or download reference images without explicit user instruction.
- Do not silently treat a reference image as editable art.

### 8.8 Recolour and pixel-art craft

- Shade a material using explicit ramps and a stated light direction.
- Snap palette colours, replace, hue-shift and desaturate.
- Apply consistent outline rules, cluster clean-up and controlled dithering.
- Avoid soft antialiasing and semi-transparent pixels unless requested.
- All operations have bounded scope and produce a report.

### 8.9 Validation / linter

Implement deterministic validation checks:
- off-palette colours;
- semi-transparent pixels;
- isolated/orphan pixels;
- duplicate/broken outline patterns where heuristics can identify them;
- excessive near-duplicate palette colours;
- empty layers or unexpected missing cels;
- inconsistent frame timing;
- untagged animation sequences;
- canvas/atlas/export constraints;
- user-provided expectations for layer frame ranges and prohibited overlaps.

Validation findings include severity, code, message, layer/frame, coordinate or region when possible, confidence and suggested action. Heuristic findings must be labelled as heuristics, not presented as absolute visual truth.

### 8.10 Export

Support, using Aseprite's supported CLI/API or extension operations as appropriate:
- PNG single frame;
- GIF animation;
- individual numbered frames;
- spritesheet;
- JSON atlas with frame and tag metadata;
- native Aseprite document copy.
- Prefer tags for exporting animation sequences.
- Validate target path, extension and overwrite policy.
- Do not report a successful export until the destination exists and basic output validation passes.
- Unity is a primary target; Godot/Tiled formats can be added after the core tool layer is proven.
- Exports must never replace the source `.aseprite` silently.

## 9. Agent and workflow model

All agents are logical roles around one shared loaded GGUF engine. Each has a specific system prompt, allowed tool list, context requirements, output schema and maximum step budget.

### Studio workflow

The top-level `StudioAgent` is the entry point for multi-step requests. It must:
1. run preflight and inspect the document;
2. classify the request;
3. decide whether key requirements are missing;
4. create a short explicit plan;
5. select the correct specialized workflow;
6. execute one stage at a time;
7. inspect after any pixel-changing stage;
8. run validation;
9. repair only high-value findings, within a bounded iteration limit;
10. save/export only if requested or clearly authorised;
11. report changes, saved paths, warnings and anything left incomplete.

### Workflows / skills

Provide equivalent workflows:
- `brief`: collect essential constraints once for an open-ended request.
- `concept`: formalize art direction, pose, view, palette, proportion, light and reference handling.
- `new`: create a new sprite or document after confirming dimensions/settings.
- `palette`: select, inspect and maintain a palette.
- `draw`: draw a sprite from silhouette to finish.
- `shade`: apply material shading consistently.
- `rig`: separate a character into reusable, animation-ready layers.
- `animate`: plan key poses, frames, timing and tags.
- `tileset`: plan tile size, adjacency, edge matching, autotile rules and export.
- `review`: cold-read the art and produce ranked findings.
- `fix`: address only named/located issues.
- `export`: create requested outputs and validate them.
- `submit`: final review/checklist and user-facing report.
- `studio`: orchestrate requests across multiple workflows.

Workflows are Markdown under `knowledge/skills/<name>.md`. Rule files belong under `knowledge/rules/` and are selected by task. A common rules schema should include applicability, objective, constraints, steps, checks, anti-patterns and references/examples.

### Single-model policy

- One model loaded at a time by default.
- The same model can be used for planning, drawing, and reviewing with distinct task prompts.
- Do not claim a critic is independent if it is the same model in a separate prompt. Label it as a separate review pass.
- Make an independent critic model an optional future feature, not a requirement.
- Preserve an interface that could support multiple models later without loading multiple models now.

## 10. Tool-calling strategy for GGUF

Do not assume every GGUF model supports native tool calling. Implement an explicit capability-aware strategy:

1. Check what the installed `llama-cpp-python` exposes and what the current GGUF chat template supports.
2. If native tool calls are supported by both the model template and the runtime, parse them through the supported API.
3. Otherwise request a strict JSON action envelope from the model and validate it against the same tool schemas.
4. If both formats fail validation, return a concise structured error with the schema violation and offer a bounded retry.
5. Only the Python ToolExecutor can invoke registered tools.
6. Never execute arbitrary JSON keys as a function name; resolve tools through an allowlisted registry.
7. Validate argument types, required fields, numeric ranges, path rules and operational preconditions.
8. Set maximum tool rounds, tokens, wall-clock duration and output size per workflow.
9. Log tool name, validated arguments (redacted where necessary), request ID, outcome and duration; do not log the full GGUF prompt by default.
10. Tool results must be compact and structured; large images should be represented as bounded previews or structured metadata rather than huge base64 blobs in the context.

Recommended generic action envelope when native tools are unavailable:

```json
{
  "type": "tool_call",
  "name": "draw.grid",
  "arguments": {
    "x": 0,
    "y": 0,
    "legend": {".": null, "X": "#252525"},
    "rows": ["....", ".XX.", ".XX.", "...."],
    "transparent": "skip"
  }
}
```

This envelope is an implementation example, not an assumption about any particular model's native format.

## 11. Local inference adapter

Implement `ChatModelPort` with a minimal interface:

- `is_available() -> bool`
- `model_loaded() -> bool`
- `load_model(config, progress_callback) -> None`
- `unload_model() -> None`
- `generate(messages, generation_options, cancel_token) -> GenerationResult`
- `generate_stream(..., token_callback, cancel_token) -> GenerationResult`
- `capabilities() -> ModelCapabilities`

Use AIStoryWriter's `engine/chat.py`, `engine/gguf_meta.py`, and `engine/llama_features.py` as the starting reference. Keep hardware detection, optional parameters and model-template handling guarded by actual runtime capabilities. A missing optional feature must not break basic dense-model inference.

Constraints:
- load the model lazily when a task needs it;
- only one active model by default;
- lock access to the model instance;
- cancel via a cooperative cancellation token;
- never block the Qt main thread;
- distinguish model load failure, generation failure, cancellation and invalid structured output;
- keep context budgeting separate from the UI chat transcript;
- maintain short, explicit context windows for each task and select only relevant knowledge rules;
- support safe unload on shutdown and clear any model references to release resources;
- do not change the existing AIStoryWriter repository to make PixelForge AI work.

### Model configuration

Persist locally:
- selected GGUF path;
- context length;
- GPU layer count;
- generation CPU threads and batch threads;
- temperature, top-p, top-k and repetition controls where supported;
- output token limit;
- relevant chat-template/thinking options where supported;
- tool-calling mode (automatic/native/JSON);
- timeouts and workflow limits.

Settings must use safe defaults and validate model paths before loading. Avoid hardcoding paths from the developer's machine. Preserve the user's established llama.cpp/CUDA setup where possible; do not reinstall the runtime automatically without explicit consent.

## 12. Context and memory manager

Do not blindly append all historical tool output to every model call.

The context manager creates a bounded task context containing:
- current user objective;
- current sprite/document facts;
- accepted art specification;
- active palette and layer/frame names;
- current workflow stage and plan;
- relevant knowledge rules;
- recent tool calls and summarized outcomes;
- previous review findings still unresolved;
- user decisions and protected constraints.

Keep complete local history for audit, but feed only relevant state to the model. Compress repetitive transport output; retain exact pixel rows only for the region being edited. After each tool call, update structured state rather than asking the model to remember every detail from prose.

Do not allow summaries to overwrite exact canvas state from the editor. The editor is authoritative for actual document dimensions, palette, pixels, frame state, and file state.

## 13. Drawing data contracts

### PixelGrid

A grid contains:
- integer `x`, `y` origin;
- `rows: list[str]`;
- one-character to colour/null legend;
- transparent policy (`erase` or `skip`);
- target sprite/layer/frame;
- expected dimensions;
- optional label/operation ID.

Validation:
- non-empty grid;
- every row has equal width;
- each key is one Unicode code point (recommend ASCII glyphs for model reliability);
- every used glyph appears in the legend;
- palette/colour policy is satisfied;
- grid fits within the canvas;
- maximum width, height and area are within configured bounds;
- transparency semantics are explicit;
- invalid grids fail before reaching Aseprite;
- never silently truncate or pad malformed rows.

### Batch

A drawing batch contains a bounded list of validated operations, target sprite/layer/frame, a human-readable label and expected effect. It is sent as one transaction whenever possible. Return summary statistics and changed bounds.

### Pixel diff and snapshots

Before high-impact or iterative edits, record a lightweight snapshot of the target layer/frame/region or export a temporary copy using a safe path. Compare after edits. Preserve the ability to restore changed regions, and report the measured percent changed. A “small fix” should stay scoped to the finding's region.

## 14. Pixel-art knowledge and validation quality

Start with carefully curated rules derived from the publicly available concepts in the reference project, rather than mass-copying repository content. Record source and license for any text/templates/data reproduced.

Minimum rule categories:
- core pixel-art principles;
- palette and colour ramps;
- light/shading/materials;
- silhouettes/forms/proportions;
- outlines, lines and clusters;
- antialiasing/dithering/textures;
- scale/readability;
- character anatomy/pose, heads/faces, hands, hair/clothing;
- views/directions;
- timing/spacing and animation cycles;
- layers/rigging;
- tiles/autotiling;
- perspective/isometric;
- terrain, foliage, water, architecture, interiors, props, vehicles and effects;
- UI and bitmap typography;
- platform/style constraints;
- review checklist and visual artifact detection.

Rules should be actionable, not merely prose. Include concrete checks and templates where appropriate, but do not promise that deterministic heuristics can detect all subjective art defects.

## 15. UI requirements

Create a polished dark desktop interface using PySide6, suited to sprite editing rather than a general chatbot.

### Main workspace

- left navigation/project and document list;
- central art preview with checkerboard transparency, nearest-neighbour zoom, pixel grid toggle and zoom controls;
- right inspector with palette, layers, document properties and AI actions;
- bottom timeline when animation is active;
- bottom/log drawer for workflow progress and diagnostics;
- AI instruction composer and readable action/plan list;
- status indicators for model, Aseprite connection, active document and unsaved state.

### Workflow affordances

- Start/stop generation;
- expose the current plan and stage;
- show concise tool activity;
- preview or inspect changes after each stage;
- undo in Aseprite and optional application-level rollback;
- confirmation on destructive operations;
- show unresolved review issues and bounded repair rounds;
- save/export only via explicit operations and show output paths;
- handle disconnected editor and model-loading states as real visible statuses.

### UI quality rules

- UI widgets and styles live in separate modules.
- No business logic in widget classes beyond view adaptation.
- Use Qt signals/slots and QThread/worker patterns for long-running tasks.
- All widget updates occur on the GUI thread.
- Do not create timers/workers that continually poll without need.
- Avoid giant monolithic `MainWindow` implementations.
- Keep empty states, loading states, error states and disabled actions designed, not improvised.
- Use consistent spacing, typography, icon sizing, tooltips and accessible labels.
- Create an app icon and include screenshots in documentation once a working UI exists.

## 16. Storage and project format

Use simple local file storage initially. No database is required for the first iteration.

Example:

```text
data/
  settings.json
  projects/
    <project-id>/
      project.json
      project.md
      history.jsonl
      runs/
        <run-id>.json
      snapshots/
      exports/
```

- Settings and projects are independent from the GGUF weights.
- Never commit model files, personal art, generated exports, secrets or runtime logs.
- Save project state atomically using temporary files and replace.
- Validate project schema versions and provide migrations for future formats.
- Keep histories bounded/rotatable; store image artefacts in files and refer to them by path/ID, not giant inline base64 strings.
- Project deletion and overwrite must be explicit.
- Do not encrypt ordinary art/history by default unless a real requirement exists; if encryption is added, document recovery/key management.

## 17. Security and data safety

Threat model: a local development application with control over the user's Aseprite session.

- Bind local server sockets to loopback only.
- No telemetry, remote inference or network egress by default.
- Treat prompts, reference text, project names and model output as untrusted input.
- Use an allowlisted tool registry; validate every argument both in Python and in Lua for mutating commands.
- Do not enable arbitrary `run_lua` by default.
- Do not run shell commands or import arbitrary Python modules from model-generated output.
- File paths must be normalized and checked against allowed destinations where appropriate.
- Ask before overwriting files or destructive canvas/layer/frame changes.
- For uncertain mutating timeouts, inspect the document before retrying.
- Avoid sensitive logs; provide a debug switch for verbose prompts only with clear warning.
- Ensure one malformed model response cannot create unbounded operations, memory consumption or a UI hang.
- Set connection frame-size limits, operation-count limits, coordinate bounds and request timeouts.

## 18. Clean Code standard

Mandatory:
- Python 3.10 compatible syntax. Do not require newer syntax/features unless project baseline is intentionally changed.
- Type hints on public functions and non-trivial methods.
- Small cohesive modules and functions with descriptive names.
- Dataclasses/enums or an explicit validation library for typed contracts.
- Dependency injection for model, Aseprite client, storage and tool registry.
- Stable exception types; catch exceptions at system boundaries and translate to structured errors.
- Use standard `logging`, not `print` for application diagnostics.
- No silent broad `except: pass` on essential paths.
- No global state for mutable app services; one explicit runtime container owns singleton services.
- No circular imports.
- No UI importing implementation details of infrastructure.
- No hardcoded user-specific paths, model names, ports or GPU settings.
- No magic numbers without named constants/configuration.
- No speculative frameworks or needless abstractions.
- Keep code formatted and lintable; configure Ruff and a type checker as practical.
- Use docstrings for public interfaces and non-obvious decisions, not redundant narration.
- Use meaningful error messages and include enough context to debug safely.
- Avoid backwards-compatibility shims before there is a public compatibility contract.
- Keep README and docs aligned with current implementation; never document unimplemented features as finished.

## 19. Testing strategy

### Unit tests (no Aseprite or GGUF required)
- grid validation and deterministic compilation;
- transparent erase/skip behavior;
- colour parsing, RGB/CIELAB distance and palette snapping;
- operation schemas and bounds;
- animation timing and tag-range validation;
- pixel diff/snapshot utilities;
- tool-call parser and malformed JSON recovery;
- context selection, token budgets and bounded history;
- error mapping and idempotency policy.

### Integration tests
- model adapter using a mocked `ChatModelPort`;
- optional manual GGUF smoke test, clearly marked and skipped without a configured model;
- WebSocket protocol framing and request/response routing;
- connection loss, timeout, reconnect and stale response handling;
- Lua command responses against an installed Aseprite where available;
- storage atomicity and schema migration.

### End-to-end tests
- preflight a connected Aseprite;
- create a small test sprite in a temporary/scratch document;
- apply one grid batch;
- retrieve preview and ASCII grid;
- validate expected dimensions and colours;
- modify one bounded region;
- verify diff;
- save to an explicit temporary path and verify output;
- confirm user document is not inadvertently overwritten.

### Test principles
- Don't use real model inference for every unit test.
- Don't depend on window focus in normal unit tests.
- Keep integration/e2e tests clearly identified by their requirements.
- Every fixed bug should add a regression test.
- A test must not overwrite a user's working art.

## 20. Setup and launch scripts

`setup.bat`:
- detect Windows and Python 3.10+;
- create `.venv` if missing;
- activate the venv;
- install pure-Python/UI requirements;
- install the existing `llama-cpp-python` build only if explicitly configured/needed; do not automatically replace a CUDA build with a CPU build;
- check import/version for PySide6 and llama_cpp;
- report what succeeded and what needs manual configuration;
- never download GGUF model weights automatically.

`run.bat`:
- verify `.venv` exists and use its Python executable directly;
- configure only project-specific environment variables, without overwriting the user's system configuration;
- check model/extension connectivity in the application;
- launch `main.py`;
- preserve error output and return a nonzero exit code on failure.

Requirements must stay lean. Separate optional development/test dependencies from runtime dependencies. Do not list Node.js, Ollama, OpenCode or a cloud SDK as required.

## 21. Observability and diagnostics

- One root logging configuration with rotating file and console handlers.
- Per-module logger names.
- Workflow run ID, tool request ID, elapsed time and outcome.
- User-friendly errors in UI; technical detail in logs without secrets.
- Health panel shows Python version, llama-cpp-python availability/build, selected model state, Aseprite extension version/capabilities, bridge connectivity, and protocol version.
- Include a diagnostic export that is opt-in and omits prompts/secrets by default.

## 22. Acceptance criteria / definition of done

A feature is not done just because it is implemented. It must have typed contracts, tests, error handling, documentation and user-visible behavior.

### MVP acceptance

1. Application starts via `run.bat` without a cloud account.
2. Uses the existing local GGUF/llama-cpp-python configuration when selected; does not download weights.
3. Confirms the actual Aseprite extension is connected before executing editing actions.
4. Reads the active sprite dimensions and palette accurately.
5. Asks for a brief only when required constraints are genuinely missing.
6. Generates a valid small pixel grid from a natural-language request.
7. Validates it in Python and sends it as a single logical edit to the real Aseprite document.
8. Returns a preview and exact grid read-back.
9. Supports bounded revision and a concrete validation report.
10. Can stop generation and handles connection loss without silent fallback.
11. Does not block the UI during inference or network operations.
12. Does not modify the AIStoryWriter repository.
13. Includes clean README, setup documentation, unit tests and a visible log/diagnostics surface.

### Full-feature acceptance

- Each supported tool has schemas, validation, structured output and tests.
- The documented workflows can be run end-to-end.
- Palette, layers, cels, frames, tags, references, transformations and exports behave consistently.
- Review reports include locations and a bounded correction plan.
- Animation is inspected as a filmstrip and timing is validated.
- Existing user edits are protected and destructive operations require confirmation.
- Security restrictions and loopback-only listener are covered by tests.
- README feature claims reflect actual implemented features.

## 23. Phased implementation roadmap

### Phase 0 — Repository foundation
- Set up package layout, app bootstrap, configuration, logging, dependency boundaries, test configuration and documentation.
- Confirm Python environment and existing llama-cpp-python install.
- No model installation or UI-heavy work yet.

**Exit criteria:** app launches and tests run.

### Phase 1 — Protocol and Aseprite connection
- Implement versioned WebSocket protocol, bridge, Lua extension, handshake, preflight, timeouts, routing, errors and reconnection.
- Add read-only sprite inspection.
- Implement safe extension installation and a doctor/diagnostics command or UI.

**Exit criteria:** Python can inspect an open sprite and reliably detects disconnects.

### Phase 2 — Deterministic drawing tools
- Implement PixelGrid model/compiler, operation schemas, batch drawing, palette handling, preview, ASCII read-back and diff.
- Add transactional/undo behavior where supported.
- Use deterministic fake editor/protocol tests plus a manual Aseprite smoke test.

**Exit criteria:** draw/read/edit/undo workflow is stable without an LLM.

### Phase 3 — GGUF tool-use loop
- Adapter around the existing LLM engine interface.
- Model capability detection and native-tool or constrained-JSON tool calling.
- Tool registry, context manager, budgets, cancellation, logs and one drawing agent.
- Ensure inference never runs on the Qt main thread.

**Exit criteria:** the configured local model can invoke a bounded action, receive its result, and continue safely.

### Phase 4 — Studio workflow and visual review
- Add plan/report types, knowledge selector, review and repair loops, pixel-art linting and run history.
- Implement brief, concept, draw, review, fix and export workflows.

**Exit criteria:** request → plan → draw → inspect → repair → validate works with bounded iterations.

### Phase 5 — Artwork production workflows
- Palette, shade, rig, animation, reference, transform, tileset and export workflows.
- Layer/frame/tag management and Unity-oriented spritesheet metadata.

**Exit criteria:** create, review, animate and export a complete asset through the real editor.

### Phase 6 — Product polish
- Refine dark UI, icon, layout, project/history browser, settings/diagnostics, installer and docs.
- Validate clean install behavior without assumptions about developer machine paths.

**Exit criteria:** repeatable clean setup, regression suite, helpful diagnostics and current documentation.

## 24. Implementation order guardrails

- Do not implement all eighteen or more tools before the connection and first draw workflow are reliable.
- Do not build the multi-agent workflow before the single-model/tool loop works.
- Do not focus on animations before frames/layers/cels/tags have tests.
- Do not add headless mode before live mode is correct unless a clear automated use case justifies it.
- Do not introduce image-generation dependencies as part of the text-model runtime.
- Do not treat a screenshot/preview alone as a pixel-accurate inspection; retain ASCII/grid and deterministic pixel read-back.
- Do not silently use file edits to recover from a live editor disconnect.
- Do not run multiple model copies for logical agents by default.
- Do not claim visual-critic independence when review is the same GGUF in a second pass.

## 25. Documentation requirements

Maintain:
- `README.md`: product value, capabilities, limitations, screenshots, quick start, local GGUF setup.
- `SPEC.md`: this product/engineering contract.
- `AGENTS.md`: concise mandatory coding rules and repository orientation for AI coding tools.
- `docs/ARCHITECTURE.md`: real component diagram and dependency boundaries.
- `docs/INSTALL.md`: Python, extension, model and troubleshooting setup.
- `docs/SECURITY.md`: local service, file paths, destructive actions, untrusted model output.
- `docs/TOOLS.md`: complete documented tool schemas/behavior.
- `docs/WORKFLOWS.md`: actual skill/workflow ordering and acceptance criteria.
- `docs/DEVELOPMENT.md`: environment, test commands, linting and release tasks.
- `CHANGELOG.md`: user-visible change history.

Every page must distinguish implemented capabilities from planned ones.

## 26. Licensing and provenance

- Aseprite AI Artist is the reference for behavior and architecture.
- AIStoryWriter is the user's own existing reference repository.
- For any copied implementation code, templates, rule text, palettes or assets, verify applicable license and preserve required notices/attribution.
- Prefer independently written Python implementations based on documented behavior and original rule text.
- Do not bundle Aseprite binaries, proprietary sprites, or third-party art without permission.
- The final app's license should be explicitly chosen and documented rather than assumed from its references.

## 27. Final engineering rule

The application must behave as an agentic Aseprite control system: inspect the editor's true state, decide, take validated actions, observe the result, correct specific defects, validate and report truthfully. The local GGUF model proposes and reasons; deterministic Python and Lua execute safe operations; Aseprite remains the authority for the actual artwork.

A beautiful interface without that loop is not this product.
