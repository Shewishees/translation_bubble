<div align="center">

<img src="images/bubble.png" width="96" alt="LocalTranslateBubble icon">

# 本地翻译悬浮球 (translation_bubble)

**Windows offline floating translator: chat-bubble widget + macOS-light panel + local GGUF model**

[![Build (Windows)](https://github.com/Shewishees/translation_bubble/actions/workflows/build.yml/badge.svg)](https://github.com/Shewishees/translation_bubble/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11%20x64-blue)]()
[![Engine](https://img.shields.io/badge/Engine-PopTrans%20AI-orange)](https://github.com/ifocus9/PopTrans)

[简体中文](README.md) · [English](README_EN.md)

*Selection translation · Screenshot OCR · Streaming output · Auto GPU acceleration · Fully offline*

<img src="images/preview.png" width="720" alt="LocalTranslateBubble preview: translation panel and floating bubble">

</div>

---

**LocalTranslateBubble** is a fully offline translator for Windows: a chat-bubble widget breathes on
your desktop, translate selected text or a screenshot with one keystroke, and watch the translation
**stream in character by character** in a macOS-light panel. The inference engine is ported from
[PopTrans](https://github.com/ifocus9/PopTrans)'s Python AI engine: `llama.cpp` runs Tencent's
**Hy-MT2-1.8B** GGUF model locally, and **RapidOCR** handles screenshot text recognition —
no cloud, no telemetry, your text never leaves the machine.

## ✨ Features

- **🌀 Floating bubble widget**: breathing at 120 FPS with sub-pixel rendering; flies smoothly to your cursor after a capture, click to expand the panel.
- **✍️ Selection quick-translate** (default `Alt+Q`): select text anywhere, press the hotkey — the tool simulates `Ctrl+C` (clipboard backed up and restored), moves the panel and bubble next to your cursor, and streams the translation immediately.
- **📷 Screenshot OCR translation** (default `Alt+W`): drag a rounded selection on a screen snapshot → RapidOCR → instant translation.
- **🖱️ Click-outside auto-hide**: the panel smoothly collapses on focus loss **keeping your translation history**; a 📌 pin in the title bar pauses auto-hide; bubble clicks are debounced (200 ms) to avoid close/reopen flicker.
- **⌨️ Visual hotkey recorder**: the ⚙ button in the title bar opens a preferences view — click a pill, press the new combo (Esc cancels, Backspace disables), applied instantly with conflict detection (soft red hint).
- **⚡ Streaming output**: OpenAI-compatible `/v1/chat/completions` (SSE); 256-entry LRU cache returns repeated text instantly.
- **🎮 Auto GPU acceleration with safe fallback**: `acceleration_device = auto/gpu/cpu`; full offload via CUDA/Vulkan (3–5× faster on an RTX 5070), automatic fallback to CPU on any failure.
- **🛌 AI idle recycling**: the engine sleeps after 15 idle minutes to free VRAM, and wakes automatically on the next translation.
- **⚙️ settings.json hot reload**: field-compatible with PopTrans; edits apply instantly without restarting.

## 🚀 Getting started

1. Download `LocalTranslateBubble-vX.Y.Z-windows-x64.zip` from [Releases](https://github.com/Shewishees/translation_bubble/releases) and extract it;
2. Run `LocalTranslateBubble.exe` — on first start the translation model (~1.1 GB) downloads automatically (resumable), or drop it into `models/Hy-MT2-1.8B-GGUF/` yourself;
3. When the panel shows **GPU acceleration ready / CPU mode ready**, you're translating offline.

> 💡 Requirements: Windows 10/11 x64; 8 GB RAM for CPU mode; ≥ 2 GB VRAM for GPU offload.

## ⌨️ Shortcuts

| Action | Shortcut | Notes |
| --- | --- | --- |
| Open the panel | Click the bubble | Bubble docks 16 px to the panel's right |
| Selection translate | `Alt+Q` | Panel and bubble fly to the cursor, auto-filled and translated |
| Screenshot OCR | `Alt+W` | Rounded full-screen selection, `Esc` to cancel |
| Summon panel | `Alt+B` | Instantly show/hide the panel (state preserved) |
| Pin panel | Title-bar 📌 | While pinned, clicking outside won't auto-hide |
| Preferences | Title-bar ⚙ / right-click bubble | Record custom global hotkeys |

<p align="center"><img src="images/settings.png" width="420" alt="Preferences view: hotkey recorder pills"></p>
| Translate now | `Ctrl+Enter` | Skips the 600 ms debounce |
| Switch direction | Pill next to the title | Auto (zh↔en) → zh→en → en→zh |
| Quit | Right-click the bubble | Open panel / capture / screenshot / quit |

## 💻 Run from source

```bat
python -m pip install -r requirements.txt
python bootstrap_backend.py      :: first run: install deps, prompt model download
start.bat                        :: launch (missing models are auto-downloaded)
```

Development dependency: Python 3.12+ recommended; for GPU acceleration install the matching
`llama-cpp-python` build (see build variants below).

## 📁 Project layout

```text
translation_bubble/
├── bubble/                     # PySide6 floating-bubble application (UI package)
│   ├── launcher.py             # entry point: starts the app, captures startup errors
│   ├── app.py                  # bubble window / translation panel / controller
│   ├── engine_supervisor.py    # engine daemon (health polling / idle sleep / sweep)
│   ├── settings_manager.py     # settings.json (PopTrans schema + hot reload)
│   ├── hotkey_manager.py       # global hotkeys (RegisterHotKey + native filter)
│   ├── text_capture.py         # selection capture (simulated Ctrl+C + clipboard restore)
│   ├── screen_capture.py       # screenshot selection overlay
│   ├── bootstrap_backend.py    # first run: dependency install / OCR model placement
│   ├── nvidia_runtime.py       # CUDA runtime DLL path injection
│   └── diagnostics.py          # diagnostics log (no input text recorded)
├── backend/                    # AI engine service (ported from PopTrans)
│   ├── api_server.py           # FastAPI: /health, /v1/chat/completions, OCR
│   ├── translator.py           # llama.cpp wrapper (auto/gpu/cpu + safe fallback)
│   ├── ocr_service.py          # RapidOCR wrapper (scale retries + line merging)
│   ├── server_config.py        # TRANSLATE_SERVER_PORT parsing
│   ├── runtime_paths.py        # source/frozen dual-mode paths
│   └── ai_engine.spec          # engine PyInstaller spec
├── scripts/                    # build / package / preview scripts (bat + py)
├── tests/                      # unit tests (23)
├── assets/                     # icons and SVG assets
├── images/                     # README previews
└── LocalTranslateBubble.spec   # UI PyInstaller spec
```

## 📦 Building a release / CI

Build a portable release (bundled Python runtime, outputs `dist\`):

```bat
scripts\setup_build_env.bat      :: create a clean build venv (auto GPU/CPU detection)
scripts\build_all.bat            :: produce dist\LocalTranslateBubble.exe + ai_engine.exe
scripts\package_zip.bat 1.0.0    :: pack a release ZIP (1.1 GB model excluded by default)
```

**GitHub Actions**: pushing a `v*` tag builds everything on `windows-latest`, packs the ZIP and
attaches it to the matching Release (see [`.github/workflows/build.yml`](.github/workflows/build.yml)).

| Artifact | Notes |
| --- | --- |
| `dist\LocalTranslateBubble.exe` | PySide6 floating-bubble UI (onedir) |
| `dist\ai_engine.exe` | Single-file AI engine (FastAPI + llama.cpp + RapidOCR; CUDA runtime bundled) |
| `dist\models\rapidocr\` | OCR ONNX models |

Build variants: the GPU build is produced automatically on machines with NVIDIA drivers; run the
same scripts on a machine without `nvidia-smi` to get the universal CPU build.

## ⚙️ Configuration (settings.json, hot-reloaded on save)

| Field | Default | Description |
| --- | --- | --- |
| `hotkey` / `hotkey_display` | `<alt>+q` / `Alt+Q` | Selection-translate hotkey (visual recorder via ⚙, empty = disabled) |
| `show_hotkey` / `show_hotkey_display` | `<alt>+b` / `Alt+B` | Summon/hide the panel |
| `ocr_enabled` / `ocr_hotkey` / `ocr_hotkey_display` | `true` / `<alt>+w` / `Alt+W` | Screenshot OCR translation |
| `server_port` | `8989` | Local engine port (1024–65535; changing it restarts the engine) |
| `acceleration_device` | `auto` | Inference device: auto / gpu / cpu |
| `ai_idle_minutes` | `15` | Idle minutes before the engine sleeps (0 = never) |
| `launch_at_login` | `false` | Launch at login (HKCU Run key, synced with the panel checkbox) |
| `logging_enabled` | `false` | Write engine logs to `translate.log` |

## 🔌 Local HTTP API (PopTrans-compatible)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Model readiness and hardware info (`device_info`) |
| `POST` | `/v1/chat/completions` | OpenAI-compatible translation (`stream=true` for SSE) |
| `POST` | `/api/v1/ocr` | RapidOCR image recognition (multipart) |
| `POST` | `/api/v1/ocr_translate` | OCR + translation in one call |

## 📄 License & credits

- Released under the **MIT License** (see [LICENSE](LICENSE)).
- The AI engine and interaction design are ported from [PopTrans](https://github.com/ifocus9/PopTrans) (MIT) — many thanks to the original author; the PySide6 floating-bubble shell and interactions are an independent implementation.
- Model: Tencent [Hy-MT2-1.8B-GGUF](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF) (official license applies), RapidOCR (Apache-2.0).

<div align="center"><sub>If this project helps you, a ⭐ would be appreciated.</sub></div>
