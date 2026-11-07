# 本地翻译悬浮气泡（PopTrans AI 引擎整合版）

Windows 本地离线翻译工具：复古六色苹果 Logo 悬浮气泡 + macOS 浅色翻译面板。
翻译能力完整对齐 [PopTrans](https://github.com/ifocus9/PopTrans) 的 Python 本地 AI 推理引擎：
`backend/`（FastAPI + llama-cpp-python）加载腾讯 **Hy-MT2-1.8B GGUF** 模型推理，
**RapidOCR** 提供截图文字识别，全部本地运行，不依赖 Node.js、npm 或在线翻译 API。

## 核心特性

- **苹果气泡常驻**：气泡在桌面呼吸浮动；划词/截图后气泡平滑飞至光标附近，点击即展开翻译面板。
- **划词快捷翻译**（默认 `Ctrl+Alt+Q`）：任意应用选中文本后按快捷键，自动模拟 `Ctrl+C` 取词。
- **截图 OCR 翻译**（默认 `Ctrl+Alt+E`，`ocr_enabled` 开启后生效）：全屏框选 → RapidOCR 识别 → 立即翻译。
- **流式输出**：OpenAI 兼容 `/v1/chat/completions`（SSE）流式返回，译文逐字出现。
- **GPU 自动加速与安全降级**：`acceleration_device` 支持 `auto` / `gpu` / `cpu`，
  优先启用 CUDA/Vulkan 卸载（`n_gpu_layers=-1`），失败平滑回退 CPU（`n_gpu_layers=0`）。
- **AI 空闲回收**：`ai_idle_minutes`（默认 15 分钟）无请求后引擎自动退出释放显存/内存，
  下次翻译自动重新拉起。
- **LRU 结果缓存 / 长文分块 / 中英自动互译**：由 `backend/translator.py` 提供（与 PopTrans 一致）。

## 启动

双击 `start.bat`。首次运行会自动安装依赖（含按显卡情况选择 llama-cpp-python 版本），
并在引擎首次加载时下载 Hy-MT2 翻译模型（约 1.13GB，支持断点续传），
进度显示在面板译文区。下载完成后完全离线。

## 配置（settings.json，保存即热加载）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `hotkey` | `<ctrl>+<alt>+q` | 划词翻译快捷键（表达式格式与 PopTrans 兼容） |
| `hotkey_display` | `Ctrl+Alt+Q` | 快捷键展示文本（自动派生） |
| `ocr_enabled` | `false` | 是否启用截图 OCR 翻译 |
| `ocr_hotkey` / `ocr_hotkey_display` | `<ctrl>+<alt>+e` / `Ctrl+Alt+E` | OCR 快捷键 |
| `server_port` | `8989` | 本地引擎端口（1024–65535，修改后引擎自动重启） |
| `acceleration_device` | `auto` | 推理设备：auto / gpu / cpu |
| `ai_idle_minutes` | `15` | 引擎空闲多久自动休眠（0 = 永不） |
| `ui_idle_minutes` | `5` | 预留（面板隐藏空闲） |
| `theme` | `system` | 预留（当前固定浅色） |
| `logging_enabled` | `false` | 引擎日志落盘 `translate.log` |
| `launch_at_login` | `false` | 开机自启（HKCU Run 注册表键，面板复选框同步） |

## 本地 API（与 PopTrans 完全一致）

- `GET /health` — 模型就绪状态与硬件信息
- `POST /v1/chat/completions` — OpenAI 兼容翻译（支持 SSE 流式）
- `POST /api/v1/ocr` — RapidOCR 识别
- `POST /api/v1/ocr_translate` — OCR 识别 + 翻译

详见 PopTrans 的 `docs/API.md`。

## 使用

- 点击苹果悬浮球打开主面板；展开时气泡停靠在面板右侧外部 16px 处，不遮挡按钮。
- 输入文本，约 600ms 自动翻译；`Ctrl+Enter` 立即翻译。
- 标题旁方向药丸：`自动`（中英互译）→ `中 → 英` → `英 → 中` 循环。
- 译文区域支持选中复制；拖动气泡或面板顶栏时保持停靠关系。
- 右键悬浮球：打开面板 / 划词翻译 / 截图翻译 / 退出。
- 引擎空闲休眠后再次翻译会自动唤醒（显示"正在唤醒本地 GGUF 模型…"）。

模型位于 `models/Hy-MT2-1.8B-GGUF/`，OCR 模型位于 `models/rapidocr/`；
运行日志见 `.runtime/diagnostics.log`（不保存输入文本）与 `translate.log`（需开启 `logging_enabled`）。

## 从源码构建发行版（打包为独立软件）

无需修改任何代码即可把项目打包成免安装的发布目录（内置 Python 运行时，
用户无需安装依赖）：

```bat
scripts\build_all.bat
```

构建流程（PyInstaller ≥6.0，见 `requirements-build.txt`）：

| 步骤 | 产物 | 说明 |
| --- | --- | --- |
| `scripts\build_ai_engine.bat` | `dist\ai_engine.exe` | Python AI 引擎单文件封装（FastAPI + llama.cpp + RapidOCR；CUDA 构建自动收入 cudart/cublas，GPU 版自包含） |
| `scripts\build_ui.bat` | `dist\LocalTranslateBubble.exe` + `_internal\` | PySide6 悬浮气泡界面（onedir，秒开） |
| `scripts\package_zip.bat [版本号]` | `LocalTranslateBubble-vX.Y.Z-windows-x64.zip` | 发布 ZIP，**默认排除约 1.1GB GGUF 大模型**（用户首次运行自动下载），如需附带离线大模型请手动复制 `models\Hy-MT2-1.8B-GGUF\` 到压缩包内 |

发布目录结构（与 PopTrans 的 `dist-go` 同构）：

```text
dist/
├── LocalTranslateBubble.exe      # 悬浮气泡主程序（双击启动）
├── _internal/                    # UI 运行时
├── ai_engine.exe                 # 本地 AI 引擎
├── icon.ico
└── models/
    ├── rapidocr/                 # OCR ONNX 模型（随包分发）
    └── Hy-MT2-1.8B-GGUF/         # 翻译模型（首启自动下载或手动放入）
```

打包态行为差异（已内置支持）：启动跳过 pip 引导；引擎优先拉起同级 `ai_engine.exe`；
`settings.json` 生成在 exe 同级目录；`POPTRANS_OFFLINE=1` 环境变量可禁用联网下载。

### 构建变体

- **GPU 版（默认，随本机环境）**：安装 cu124 轮 + `nvidia-cuda-runtime-cu12`/`nvidia-cublas-cu12`（参考 README 上文依赖说明），`ai_engine.spec` 会自动把 cudart/cublas 收入 exe。
- **CPU 版（普适发布）**：先卸载 CUDA 版 llama-cpp-python 再安装 CPU 版（`pip install llama-cpp-python --force-reinstall`，Python ≤3.12 有官方预编译轮），然后重新构建。

## 开源说明

- 本项目基于 **MIT License** 开源（见 `LICENSE`）。
- 翻译引擎与交互设计整合自 [PopTrans](https://github.com/ifocus9/PopTrans)（MIT），感谢原作者；本项目的 PySide6 悬浮气泡壳与交互为独立实现。
- 模型：腾讯 [Hy-MT2-1.8B](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF)（模型遵循其官方许可）、RapidOCR（Apache-2.0）。

