"""翻译引擎守护进程管理 — PopTrans internal/backend/supervisor.go 的 Python 移植。

职责对齐：
- 以独立子进程拉起 `python -m backend.api_server`（注入 TRANSLATE_SERVER_PORT，
  CREATE_NO_WINDOW 隔离控制台信号，日志按 settings.logging_enabled 落盘 translate.log）；
- 1 秒轮询 /health 转发模型加载状态（就绪超时 30 分钟，容纳首次模型下载）；
- 检测「翻译引擎初始化失败」前缀并上报友好错误；
- 进程意外退出检测；Stop = taskkill /T /F 结束进程树；
- AI 空闲回收：ai_idle_minutes 无请求后自动结束引擎释放显存/内存，
  下次翻译自动重新拉起（PopTrans「AI 空闲回收」同款行为）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .nvidia_runtime import subprocess_env

CREATE_NO_WINDOW = 0x08000000
STARTUP_READY_TIMEOUT = 30 * 60          # 首次运行可能需要下载约 1.13GB 模型
POLL_INTERVAL = 1.0
IDLE_TICK_MS = 30_000

INIT_FAILURE_PREFIX = "翻译引擎初始化失败"


def _health_opener():
    """绕过系统代理直连 127.0.0.1（对齐 PopTrans 仅监听回环的行为）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_health(base_url: str, timeout: float = 5.0) -> dict:
    opener = _health_opener()
    with opener.open(base_url.rstrip("/") + "/health", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


class EngineSupervisor(QObject):
    # raw_status 为引擎 translator_status 原文（UI 层负责映射成友好文案）
    status_changed = Signal(str)
    ready_changed = Signal(bool, dict)      # (translator_ready, device_info)
    failed = Signal(str)                    # 友好错误文案
    idle_exited = Signal()
    process_died = Signal()
    _idle_timer_command = Signal(bool)      # 跨线程投递到 GUI 线程：True=start / False=stop

    def __init__(
        self,
        root: Path,
        port_provider: Callable[[], int],
        logging_provider: Callable[[], bool],
        idle_minutes_provider: Callable[[], int],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.root = root
        self._port_provider = port_provider
        self._logging_provider = logging_provider
        self._idle_minutes_provider = idle_minutes_provider

        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._log_file = None
        self._poll_thread: Optional[threading.Thread] = None
        self._shutdown_thread: Optional[threading.Thread] = None
        self._stopping = False
        self._spawning = False
        self._owned = True          # 当前引擎是否为本进程拉起（可 taskkill 结束）
        self._adopted = False       # 是否处于"领养常驻引擎"模式
        self._ready = False
        self._device_info: dict = {}
        self._last_activity = time.time()

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(IDLE_TICK_MS)
        self._idle_timer.timeout.connect(self._check_idle)
        self._idle_timer_command.connect(self._handle_idle_timer_command)

    # ── 状态查询 ─────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (self._proc is not None and self._proc.poll() is None) or self._adopted

    def device_info(self) -> dict:
        with self._lock:
            return dict(self._device_info)

    def touch(self) -> None:
        """刷新活动时间戳（每次翻译/OCR 请求时调用）。"""
        with self._lock:
            self._last_activity = time.time()

    # ── 生命周期 ─────────────────────────────────────────────

    def ensure_running(self) -> None:
        """确保引擎子进程存活；未运行则由后台线程清扫残留并拉起。

        清扫（netstat/PowerShell）与 Popen 全部在后台线程执行，
        绝不阻塞 UI 主线程的动画渲染循环。
        """
        with self._lock:
            if self._stopping or self._spawning:
                return
            if (self._proc is not None and self._proc.poll() is None) or self._adopted:
                self._last_activity = time.time()
                self._ensure_idle_timer()
                return
            self._spawning = True
        threading.Thread(target=self._spawn_flow, daemon=True).start()

    def _spawn_flow(self) -> None:
        try:
            with self._lock:
                if self._stopping:
                    return
                if (self._proc is not None and self._proc.poll() is None) or self._adopted:
                    return
            # 领养：端口上已有健康引擎（常驻遗留 / 上次未随 UI 退出），
            # 直接挂载监视而不重新拉起，模型保持已加载状态，零等待
            try:
                base = f"http://127.0.0.1:{int(self._port_provider())}"
                health = fetch_health(base, timeout=3)
            except Exception:
                health = None
            if health is not None and health.get("status") == "ok":
                ready = bool(health.get("translator_ready"))
                device = health.get("device_info") or {}
                status_text = str(health.get("translator_status") or "")
                with self._lock:
                    self._owned = False
                    self._adopted = True
                    self._ready = ready
                    self._device_info = dict(device)
                    self._last_activity = time.time()
                if status_text:
                    self.status_changed.emit(status_text)
                self.ready_changed.emit(ready, dict(device))
                if self._poll_thread is None or not self._poll_thread.is_alive():
                    self._poll_thread = threading.Thread(
                        target=self._poll_loop, args=[None, not ready], daemon=True
                    )
                    self._poll_thread.start()
                self._ensure_idle_timer()
                return
            with self._lock:
                self._sweep_leftover_engines()
                self._spawn_locked()
        finally:
            with self._lock:
                self._spawning = False

    def _backend_command(self) -> tuple[list[str], str]:
        """对齐 supervisor.go backendCommand：优先同目录 ai_engine.exe（打包态），
        其次 python -m backend.api_server（源码开发态）。"""
        engine_path = self.root / "ai_engine.exe"
        if engine_path.is_file():
            return [str(engine_path)], "ai_engine.exe"
        if getattr(sys, "frozen", False):
            return [], ""
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        return [str(python), "-m", "backend.api_server"], "python -m backend.api_server"

    def _spawn_locked(self) -> None:
        port = int(self._port_provider())
        env = subprocess_env()
        env["TRANSLATE_SERVER_PORT"] = str(port)

        command, backend_name = self._backend_command()
        if not command:
            self.failed.emit("找不到本地 AI 引擎（ai_engine.exe），请重新安装")
            self._close_log()
            return

        kwargs = dict(
            cwd=str(self.root),
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        if self._logging_provider():
            log_path = self.root / "translate.log"
            self._log_file = open(log_path, "ab")
            kwargs["stdout"] = self._log_file
            kwargs["stderr"] = self._log_file
        else:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL

        try:
            self._proc = subprocess.Popen(command, **kwargs)
        except OSError:
            self.failed.emit("本地翻译引擎启动失败，请点击重试")
            self._close_log()
            return

        self._ready = False
        self._device_info = {}
        self._stopping = False
        self._adopted = False
        self._last_activity = time.time()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_loop, args=[self._proc], daemon=True
            )
            self._poll_thread.start()
        self._ensure_idle_timer()

    def _ensure_idle_timer(self) -> None:
        # QTimer 只能在其所属线程操作；工作线程通过信号队列投递到 GUI 线程
        self._idle_timer_command.emit(True)

    def _handle_idle_timer_command(self, start: bool) -> None:
        if start:
            if not self._idle_timer.isActive():
                self._idle_timer.start()
        else:
            self._idle_timer.stop()

    def _poll_loop(self, proc: Optional[subprocess.Popen], waiting_start: bool = True) -> None:
        """健康轮询：等待就绪（最多 30 分钟），之后转为轻量存活监测。

        proc 为 None 表示领养模式（引擎常驻、非本进程子进程）：
        无法用 poll() 探活，改用连续健康检查失败计数判定引擎死亡。
        """
        base = f"http://127.0.0.1:{int(self._port_provider())}"
        deadline = time.time() + STARTUP_READY_TIMEOUT
        waiting = waiting_start
        last_status = ""
        adopt_failures = 0

        while not self._stopping:
            if proc is not None and proc.poll() is not None:
                with self._lock:
                    self._proc = None
                    self._ready = False
                    self._close_log()
                if not self._stopping:
                    if waiting:
                        self.failed.emit("本地翻译引擎意外退出，请点击重试")
                    else:
                        self.process_died.emit()
                return

            try:
                health = fetch_health(base)
            except Exception:
                health = None

            if proc is None:
                # 领养模式：连续多次健康检查失败视为常驻引擎已死亡
                if health is None:
                    adopt_failures += 1
                    if adopt_failures >= 10:
                        with self._lock:
                            self._ready = False
                            self._adopted = False
                        self.process_died.emit()
                        return
                else:
                    adopt_failures = 0

            if health is not None and health.get("status") == "ok":
                status_text = str(health.get("translator_status") or "")
                ready = bool(health.get("translator_ready"))
                device = health.get("device_info") or {}
                if status_text and status_text != last_status:
                    last_status = status_text
                    self.status_changed.emit(status_text)
                if status_text.startswith(INIT_FAILURE_PREFIX):
                    self.ready_changed.emit(False, device)
                    self.failed.emit("本地 GGUF 引擎初始化失败，请点击重试")
                    self._stop_core()
                    return
                if ready and waiting:
                    # 仅在首次就绪时记录状态与活动时间戳；此后轮询只做存活监测，
                    # 活动时间戳完全由用户请求（touch）驱动，否则空闲休眠永不触发
                    with self._lock:
                        self._ready = True
                        self._device_info = dict(device)
                        self._last_activity = time.time()
                    self.ready_changed.emit(True, dict(device))
                    waiting = False
            elif waiting and health is not None:
                # /health 可达但模型仍在加载：状态文本已在上方转发
                pass

            if waiting and time.time() > deadline:
                self.failed.emit("引擎启动超时，请检查网络或点击重试")
                self._stop_core()
                return

            time.sleep(POLL_INTERVAL if waiting else 3.0)

    # ── 空闲休眠 ─────────────────────────────────────────────

    def _check_idle(self) -> None:
        idle_minutes = int(self._idle_minutes_provider())
        if idle_minutes <= 0:
            return
        with self._lock:
            if not self._ready:
                return
            if not ((self._proc is not None and self._proc.poll() is None) or self._adopted):
                return
            idle_seconds = time.time() - self._last_activity
        if idle_seconds >= idle_minutes * 60:
            self.stop(permanent=False)
            self.idle_exited.emit()

    # ── 停止 / 重启 ───────────────────────────────────────────

    def stop(self, permanent: bool = True) -> None:
        """停止引擎。非阻塞：进程终止与残留清扫在后台线程执行。

        permanent=True（应用退出）：保持 _stopping，禁止后续自动拉起；
        permanent=False（空闲休眠）：完成后清除 _stopping，下次请求可自动唤醒。
        """
        with self._lock:
            self._stopping = True
            self._ready = False
            self._adopted = False
            proc = self._proc
            self._proc = None
        self._idle_timer_command.emit(False)
        if proc is None and not permanent:
            with self._lock:
                self._stopping = False
            return
        self._shutdown_thread = threading.Thread(
            target=self._shutdown_worker, args=(proc, permanent), daemon=True
        )
        self._shutdown_thread.start()

    def _shutdown_worker(self, proc, permanent: bool) -> None:
        self._terminate_process_tree(proc)
        if proc is None:
            # proc 为空说明此前处于"领养常驻引擎"模式：
            # taskkill 无从下手，改按端口+命令行清扫结束它
            try:
                self._sweep_leftover_engines()
            except Exception:
                pass
        self._close_log()
        if permanent:
            try:
                self._sweep_leftover_engines()
            except Exception:
                pass
        else:
            with self._lock:
                self._stopping = False

    def detach_resident(self) -> bool:
        """模型常驻：应用退出时解除接管，引擎留在后台继续运行（模型保持已加载）。

        返回 True 表示确实有引擎被留驻；False 表示当前没有引擎。
        """
        with self._lock:
            alive = (self._proc is not None and self._proc.poll() is None) or self._adopted
            self._proc = None
            self._owned = False
            self._adopted = False
            self._stopping = False
        self._idle_timer_command.emit(False)
        self._close_log()
        return alive

    def join_shutdown(self, timeout: float = 3.0) -> None:
        """等待后台终止/清扫完成（应用退出路径使用，防止孤儿引擎）。"""
        thread = self._shutdown_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _stop_core(self) -> None:
        """核心停止：可在工作线程中安全调用（不触碰 QTimer）。"""
        with self._lock:
            self._stopping = True
            self._ready = False
            self._adopted = False
            proc = self._proc
            self._proc = None
        self._terminate_process_tree(proc)
        if proc is None:
            try:
                self._sweep_leftover_engines()
            except Exception:
                pass
        self._close_log()

    def restart(self) -> None:
        """后台线程执行 停止 → 等待退出 → 重新拉起，全程不阻塞 UI。"""
        def worker() -> None:
            self.stop(permanent=True)
            self.join_shutdown(8.0)
            time.sleep(0.15)
            with self._lock:
                self._stopping = False
            self.ensure_running()

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _terminate_process_tree(proc: Optional[subprocess.Popen]) -> None:
        """对齐 supervisor.go terminateProcessTree：taskkill /T /F 结束整棵进程树。"""
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                return
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def _sweep_leftover_engines(self) -> None:
        """兜底清理：结束端口上残留的本项目引擎进程。

        对齐 supervisor.go killEngineByPath：应用被强杀时子进程可能遗留并
        占住端口，导致下次启动的引擎无法绑定。仅当端口监听进程的命令行
        确认是 `backend.api_server` 时才动手，避免误伤其他程序。
        任何异常都不能阻断引擎启动流程，因此整体兜底捕获。
        """
        try:
            pids = self._listening_pids_on_port()
            for pid in pids:
                if not self._is_our_engine(pid):
                    continue
                subprocess.run(
                    ["taskkill", "/PID", pid, "/T", "/F"],
                    creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
        except Exception:
            return

    def _listening_pids_on_port(self) -> list[str]:
        # netstat 输出为控制台编码（中文系统 GBK），这里按字节读取后宽松解码，
        # 端口/LISTENING/PID 均为 ASCII，不受影响
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        output = (result.stdout or b"").decode("utf-8", "replace")
        port_suffix = f":{int(self._port_provider())}"
        pids: list[str] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(port_suffix) and parts[3] == "LISTENING":
                if parts[4] not in pids:
                    pids.append(parts[4])
        return pids

    def _is_our_engine(self, pid: str) -> bool:
        query = (
            "if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ "
            "(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine "
            "}}".format(pid=pid)
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        # PowerShell 输出编码随系统区域设置，宽松解码只匹配 ASCII 模块名
        stdout = (result.stdout or b"").decode("utf-8", "replace")
        return "backend.api_server" in stdout or "ai_engine.exe" in stdout.lower()
