# -*- coding: utf-8 -*-
"""Muse Dash 谱面急救箱 —— tkinter 图形界面（仅标准库）。

本模块只通过 core.py 的公开 API 工作，不实现任何转换逻辑：
    detect_game_dir / find_custom_albums / default_offline_dir / scan / convert

SPEC R2 的 plan 取值：cam（就地修复 .mdm，默认）/ cam_folder（解压为 CAM 文件夹）/
euterpe（Euterpe 离线谱）/ both（cam + euterpe）。

所有耗时的 core 调用都在后台线程中执行；界面更新通过 queue + root.after 完成，
不会阻塞 Tk 主线程。转换取消采用 threading.Event：progress 回调返回前检查取消标记，
一旦用户取消就抛出内部异常中止 core 操作（不改动 core 的函数签名）。
"""
from __future__ import annotations

import datetime
import os
import queue
import subprocess
import sys
import threading
import traceback

import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk

try:
    import core
except Exception as _core_exc:  # pragma: no cover - 仅在 core.py 无法导入时发生
    core = None
    _CORE_IMPORT_ERROR = _core_exc
else:
    _CORE_IMPORT_ERROR = None


APP_TITLE = "Muse Dash 谱面急救箱（喵斯兔损坏谱面修复）"
APP_VERSION = "1.1.0"

PLAN_CAM = "cam"
PLAN_CAM_FOLDER = "cam_folder"
PLAN_EUTERPE = "euterpe"
PLAN_BOTH = "both"

PLAN_CHOICES = (
    (PLAN_CAM, "① 方案1 修复 .mdm（推荐，默认）"),
    (PLAN_EUTERPE, "② 方案2 转换为 Euterpe 离线谱"),
)
PLAN_TEXTS = dict(PLAN_CHOICES)
PLAN_NEEDS_CUSTOM_ALBUMS = ()
PLAN_NEEDS_OFFLINE = (PLAN_EUTERPE,)

_CA_DIR_NAMES = {"custom_albums", "customalbums", "custom albums"}


class OperationCancelled(Exception):
    """用户主动取消当前后台操作。"""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _now_text():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _normalize_path(raw):
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text))


def _looks_like_custom_albums(path):
    try:
        return os.path.basename(os.path.normpath(path)).lower() in _CA_DIR_NAMES
    except Exception:
        return False


def _short_exc(exc):
    if isinstance(exc, NotImplementedError):
        return "core 模块对应功能尚未实现（NotImplementedError），请稍后再试。"
    text = str(exc).strip()
    if text:
        return "%s: %s" % (type(exc).__name__, text)
    return type(exc).__name__


def _item_name(item):
    name = str(getattr(item, "chart_name", "") or "").strip()
    if name:
        return name
    path = str(getattr(item, "path", "") or "")
    base = os.path.basename(path.rstrip("\\/")) if path else ""
    if base.lower().endswith(".mdm"):
        base = base[:-4]
    return base or "（未命名）"


def _item_author(item):
    author = str(getattr(item, "author", "") or "").strip()
    return author or "—"


def _item_needs_fix(item):
    return bool(getattr(item, "needs_fix", False))


def _issue_list(item, attr):
    values = getattr(item, attr, None) or []
    try:
        return [str(value).strip() for value in values if str(value).strip()]
    except TypeError:
        return [str(values)]


def _item_issues(item):
    return _issue_list(item, "issues")


def _item_warnings(item):
    return _issue_list(item, "warnings")


_SKIP_MESSAGE_KEYWORDS = ("跳过", "已存在", "仅适用", "不适用", "无需")


def _result_status(result):
    ok = bool(getattr(result, "ok", False))
    message = str(getattr(result, "message", "") or "")
    if any(keyword in message for keyword in _SKIP_MESSAGE_KEYWORDS):
        return "skipped"
    return "success" if ok else "failed"


def _result_item(result):
    return getattr(result, "item", None)


def _open_with_system(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _enable_dpi_awareness():
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _print_error(text):
    try:
        if sys.stderr is not None:
            print(text, file=sys.stderr)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class ChartFixerApp:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.worker = None
        self.items = []
        self.items_by_path = {}
        self.log_lines = []
        self._closed = False
        self._active_backup_dir = ""

        self.game_dir_var = tk.StringVar(value="")
        self.custom_albums_var = tk.StringVar(value="（尚未选择游戏目录）")
        self.offline_var = tk.StringVar(value="（尚未选择游戏目录）")
        self.backup_var = tk.StringVar(value="（尚未选择游戏目录）")
        self.scan_stats_var = tk.StringVar(value="扫描统计：尚未扫描")
        self.plan_var = tk.StringVar(value=PLAN_CAM)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")

        self._setup_window()
        self._setup_style()
        self._build_ui()

        self.root.report_callback_exception = self._report_callback_exception
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._refresh_dirs()
        self._append_log(
            "欢迎使用 Muse Dash 谱面急救箱：方案1=就地修复 .mdm 内 info.json，方案2=转换为 Euterpe 原生离线谱。"
        )
        self._append_log("源谱面文件始终保留；被修改的 info.json 会备份到 <游戏目录>/_ChartFixerBackup。")
        self.root.after(120, self._pump)
        self.root.after(260, self._auto_detect_startup)

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _setup_window(self):
        self.root.title(APP_TITLE)
        # 按屏幕大小自适应，避免小屏/高缩放下窗口底部按钮与日志被裁掉
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
        except tk.TclError:
            screen_w, screen_h = 1280, 800
        width = min(1160, max(940, screen_w - 80))
        height = min(840, max(620, screen_h - 140))
        pos_x = max(0, (screen_w - width) // 2)
        pos_y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry("%dx%d+%d+%d" % (width, height, pos_x, pos_y))
        self.root.minsize(min(940, width), min(600, height))
        # 双击启动时窗口可能被其他程序挡住，用户会误以为没启动：先置顶抢焦点，再取消置顶
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _setup_style(self):
        families = set(tkfont.families(self.root))
        family = "TkDefaultFont"
        for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Noto Sans CJK SC"):
            if candidate in families:
                family = candidate
                break
        else:
            try:
                family = tkfont.nametofont("TkDefaultFont").actual("family")
            except tk.TclError:
                family = "TkDefaultFont"
        self.font_family = family
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
            "TkFixedFont",
        ):
            try:
                tkfont.nametofont(name).configure(family=family, size=9)
            except tk.TclError:
                pass
        style = ttk.Style(self.root)
        try:
            if sys.platform.startswith("win") and "vista" in style.theme_names():
                style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=(family, 9))
        style.configure("Heading.TLabel", font=(family, 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Status.TLabel", foreground="#0b6b3a")
        style.configure("Treeview", rowheight=23)
        style.configure("Treeview.Heading", font=(family, 9, "bold"))

    def _build_ui(self):
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=(10, 2))
        ttk.Label(header, text="Muse Dash 谱面急救箱", style="Heading.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="  修复喵斯兔（MDT）生成的损坏谱面，让 Euterpe v1 重新识别为离线谱",
            style="Hint.TLabel",
        ).pack(side="left", padx=(6, 0), pady=(6, 0))

        self._build_game_frame()
        self._build_toolbar()
        self._build_plan_frame()
        # 先占住底部按钮与日志区；窗口变小时优先压缩扫描结果区，保证底部始终可见
        self._build_bottom()
        self._build_log()
        self._build_tree()

    def _build_game_frame(self):
        frame = ttk.LabelFrame(self.root, text="游戏目录")
        frame.pack(fill="x", padx=10, pady=(6, 4))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Muse Dash 游戏目录：").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=(6, 2))
        self.entry_game = ttk.Entry(frame, textvariable=self.game_dir_var)
        self.entry_game.grid(row=0, column=1, sticky="ew", pady=(6, 2))
        self.entry_game.bind("<Return>", self._refresh_dirs)
        self.entry_game.bind("<FocusOut>", self._refresh_dirs)
        self.btn_detect = ttk.Button(frame, text="自动检测", width=10, command=self.on_detect)
        self.btn_detect.grid(row=0, column=2, padx=4, pady=(6, 2))
        self.btn_browse = ttk.Button(frame, text="浏览…", width=10, command=self.on_browse)
        self.btn_browse.grid(row=0, column=3, padx=(0, 8), pady=(6, 2))

        ttk.Label(frame, text="Custom_Albums：").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=1)
        ttk.Label(frame, textvariable=self.custom_albums_var, foreground="#0a6b3d").grid(
            row=1, column=1, columnspan=3, sticky="w"
        )
        ttk.Label(frame, text="Euterpe 离线目录：").grid(row=2, column=0, sticky="w", padx=(8, 4), pady=1)
        ttk.Label(frame, textvariable=self.offline_var, foreground="#0a6b3d").grid(
            row=2, column=1, columnspan=3, sticky="w"
        )
        ttk.Label(frame, text="备份目录：").grid(row=3, column=0, sticky="w", padx=(8, 4), pady=1)
        ttk.Label(frame, textvariable=self.backup_var, foreground="#8a5a00").grid(
            row=3, column=1, columnspan=3, sticky="w"
        )
        ttk.Label(frame, textvariable=self.scan_stats_var, style="Hint.TLabel").grid(
            row=4, column=1, columnspan=3, sticky="w", pady=(2, 6)
        )

    def _build_toolbar(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=(0, 4))
        self.btn_scan = ttk.Button(frame, text="扫描", width=10, command=self.on_scan)
        self.btn_scan.pack(side="left")
        self.btn_cancel = ttk.Button(frame, text="取消", width=8, command=self.on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(frame, orient="horizontal", mode="indeterminate", length=240)
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 10))
        ttk.Label(frame, textvariable=self.status_var, style="Status.TLabel", anchor="w", width=30).pack(
            side="right"
        )

    def _build_plan_frame(self):
        frame = ttk.LabelFrame(self.root, text="处理方案")
        frame.pack(fill="x", padx=10, pady=(0, 4))

        radio_frame = ttk.Frame(frame)
        radio_frame.pack(fill="x", padx=8, pady=(6, 2))
        self.plan_radios = []
        for index, (value, text) in enumerate(PLAN_CHOICES):
            radio = ttk.Radiobutton(radio_frame, text=text, value=value, variable=self.plan_var)
            radio.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 18), pady=1)
            self.plan_radios.append(radio)

        action_row = ttk.Frame(frame)
        action_row.pack(fill="x", padx=8, pady=(4, 2))
        self.chk_dry = ttk.Checkbutton(action_row, text="仅预览，不写入文件", variable=self.dry_run_var)
        self.chk_dry.pack(side="left")
        self.btn_convert = ttk.Button(action_row, text="开始转换", width=12, command=self.on_convert)
        self.btn_convert.pack(side="right")

        ttk.Label(
            frame,
            text=(
                "提示：方案① 只处理「状态＝待修复」的谱面，直接就地修复 .mdm 包内的 info.json"
                "（原 info.json 自动备份）；方案② 会把扫描到的谱面全部转换为 "
                "Euterpe_Charts/Offline 下的原生离线谱（合法谱面也会转换）。转换前请先关闭游戏。"
            ),
            style="Hint.TLabel",
            wraplength=1100,
        ).pack(anchor="w", padx=8, pady=(0, 6))

    def _build_tree(self):
        frame = ttk.LabelFrame(self.root, text="扫描结果")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        container = ttk.Frame(frame)
        container.pack(fill="both", expand=True, padx=6, pady=6)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            container,
            columns=("name", "author", "issues", "status"),
            show="headings",
            height=9,
        )
        self.tree.heading("name", text="谱面名")
        self.tree.heading("author", text="作者")
        self.tree.heading("issues", text="问题摘要")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=250, minwidth=140, anchor="w", stretch=False)
        self.tree.column("author", width=140, minwidth=80, anchor="w", stretch=False)
        self.tree.column("issues", width=560, minwidth=200, anchor="w", stretch=True)
        self.tree.column("status", width=90, minwidth=70, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("needs_fix", foreground="#c0392b")
        self.tree.tag_configure("ok", foreground="#1e8449")
        self.tree.bind("<Double-1>", self._on_tree_double_click)

    def _build_log(self):
        frame = ttk.LabelFrame(self.root, text="运行日志")
        frame.pack(fill="x", side="bottom", padx=10, pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(
            frame,
            height=6,
            wrap="word",
            state="disabled",
            font=(self.font_family, 9),
            relief="solid",
            borderwidth=1,
        )
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_bottom(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", side="bottom", padx=10, pady=(0, 10))
        ttk.Button(frame, text="打开 Custom_Albums", command=self.on_open_custom_albums).pack(side="left")
        ttk.Button(
            frame, text="打开 Euterpe_Charts/Offline", command=self.on_open_offline
        ).pack(side="left", padx=(6, 0))
        ttk.Button(frame, text="打开备份目录", command=self.on_open_backup).pack(side="left", padx=(6, 0))
        ttk.Button(frame, text="保存日志", command=self.on_save_log).pack(side="left", padx=(6, 0))
        ttk.Button(frame, text="退出", width=8, command=self._on_close).pack(side="right")

    # ------------------------------------------------------------------
    # 日志 / 线程消息
    # ------------------------------------------------------------------
    def _append_log(self, message):
        line = "[%s] %s" % (_now_text(), message)
        self.log_lines.append(line)
        widget = getattr(self, "log_text", None)
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.insert("end", line + "\n")
            widget.see("end")
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _progress(self, message):
        if self.cancel_event.is_set():
            raise OperationCancelled()
        text = str(message).strip() or "处理中…"
        self.queue.put(("progress", text))

    def _pump(self):
        if self._closed:
            return
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_message(message)
            except Exception:  # pragma: no cover - 防御性
                traceback.print_exc()
        if not self._closed:
            try:
                self.root.after(100, self._pump)
            except tk.TclError:
                pass

    def _handle_message(self, message):
        kind = message[0]
        if kind == "progress":
            text = message[1]
            self.status_var.set(text)
            self._append_log(text)
        elif kind == "detect_done":
            path = message[1] if len(message) > 1 else None
            error = message[2] if len(message) > 2 else None
            self._finish_operation()
            if error:
                self._append_log("自动检测失败：" + str(error))
            if path:
                self.game_dir_var.set(str(path))
                self._refresh_dirs()
                self._append_log("已自动检测到游戏目录：" + str(path))
                self.status_var.set("已检测到游戏目录")
            else:
                self._append_log("未能自动检测到游戏目录，请点击【浏览…】手动选择 Muse Dash 游戏根目录。")
                self.status_var.set("未检测到游戏目录，请手动选择")
        elif kind == "scan_done":
            items = message[1] if len(message) > 1 else []
            self._finish_operation()
            self._show_scan_items(list(items or []))
        elif kind == "convert_done":
            results = message[1] if len(message) > 1 else []
            dry_run = bool(message[2]) if len(message) > 2 else False
            self._finish_operation()
            self._show_convert_results(list(results or []), dry_run)
        elif kind == "error":
            label = message[1] if len(message) > 1 else "操作"
            text = message[2] if len(message) > 2 else "未知错误"
            detail = message[3] if len(message) > 3 else ""
            self._finish_operation()
            if label == "扫描":
                self.scan_stats_var.set("扫描统计：扫描失败")
            self._append_log("%s失败：%s" % (label, text))
            if detail:
                for detail_line in str(detail).rstrip().splitlines():
                    self._append_log("    " + detail_line)
            self.status_var.set("%s失败" % label)
            messagebox.showerror("%s失败" % label, str(text), parent=self.root)
        elif kind == "cancelled":
            label = message[1] if len(message) > 1 else "操作"
            self._finish_operation()
            if label == "扫描":
                self.scan_stats_var.set("扫描统计：扫描已取消")
            self._append_log("已取消%s。" % label)
            self.status_var.set("已取消")

    def _start_operation(self, label, worker):
        if self.busy:
            return False
        self.busy = True
        self.cancel_event.clear()
        self._set_busy(True)
        self.status_var.set("%s中…" % label)
        try:
            self.progress.start(15)
        except tk.TclError:
            pass
        self._append_log("开始%s…" % label)
        self.worker = threading.Thread(target=self._run_guarded, args=(label, worker), daemon=True)
        self.worker.start()
        return True

    def _run_guarded(self, label, worker):
        try:
            worker()
        except OperationCancelled:
            self.queue.put(("cancelled", label))
        except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
            detail = traceback.format_exc()
            self.queue.put(("error", label, _short_exc(exc), detail))

    def _finish_operation(self):
        self.busy = False
        self.worker = None
        self.cancel_event.clear()
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self._set_busy(False)
        self.status_var.set("就绪")

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        widgets = [
            self.entry_game,
            self.btn_detect,
            self.btn_browse,
            self.btn_scan,
            self.btn_convert,
            self.chk_dry,
        ]
        widgets.extend(self.plan_radios)
        for widget in widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.btn_cancel.configure(state="normal" if busy else "disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 目录解析
    # ------------------------------------------------------------------
    def _require_core(self):
        if core is None:
            raise RuntimeError("无法导入 core 模块：%s" % _CORE_IMPORT_ERROR)
        return core

    def _resolve_paths(self):
        raw = self.game_dir_var.get().strip()
        if not raw:
            return None, None, None
        game_path = _normalize_path(raw)
        direct_custom_albums = None
        if _looks_like_custom_albums(game_path):
            direct_custom_albums = game_path
            game_path = os.path.dirname(game_path)
        custom_albums = direct_custom_albums
        if custom_albums is None and core is not None:
            try:
                custom_albums = core.find_custom_albums(game_path)
            except Exception as exc:
                self._append_log("查找 Custom_Albums 失败：" + _short_exc(exc))
        if not custom_albums:
            candidate = os.path.join(game_path, "Custom_Albums")
            if os.path.isdir(candidate):
                custom_albums = candidate
        offline = None
        if core is not None:
            try:
                offline = core.default_offline_dir(game_path)
            except Exception as exc:
                self._append_log("计算 Euterpe 离线目录失败：" + _short_exc(exc))
        if not offline:
            offline = os.path.join(game_path, "Euterpe_Charts", "Offline")
        return game_path, custom_albums, offline

    def _backup_dir_for(self, game_dir):
        if not game_dir:
            return ""
        return os.path.join(game_dir, "_ChartFixerBackup")

    def _refresh_dirs(self, _event=None):
        try:
            game, custom_albums, offline = self._resolve_paths()
        except Exception as exc:
            self._append_log("解析目录失败：" + _short_exc(exc))
            game, custom_albums, offline = None, None, None
        if not game:
            self.custom_albums_var.set("（尚未选择游戏目录）")
            self.offline_var.set("（尚未选择游戏目录）")
            self.backup_var.set("（尚未选择游戏目录）")
            return
        self.custom_albums_var.set(custom_albums or "未找到（请确认游戏目录是否正确）")
        self.offline_var.set(offline or "未找到")
        self.backup_var.set(self._backup_dir_for(game) + "（转换时自动创建）")

    def _auto_detect_startup(self):
        if self._closed or self.busy:
            return
        if self.game_dir_var.get().strip():
            return
        self._start_operation("自动检测", self._work_detect)

    # ------------------------------------------------------------------
    # 按钮回调
    # ------------------------------------------------------------------
    def on_browse(self):
        if self.busy:
            return
        initial = self.game_dir_var.get().strip() or os.path.expanduser("~")
        if not os.path.isdir(initial):
            initial = os.path.expanduser("~")
        try:
            chosen = filedialog.askdirectory(
                parent=self.root,
                title="选择 Muse Dash 游戏根目录",
                initialdir=initial,
                mustexist=False,
            )
        except tk.TclError:
            chosen = ""
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        self.game_dir_var.set(chosen)
        self._refresh_dirs()
        self._append_log("已选择游戏目录：" + chosen)

    def on_detect(self):
        if self.busy:
            return
        self._start_operation("自动检测", self._work_detect)

    def on_scan(self):
        if self.busy:
            return
        game, custom_albums, _offline = self._resolve_paths()
        if not game:
            messagebox.showwarning(
                "请选择游戏目录",
                "请先选择 Muse Dash 游戏根目录，或点击【自动检测】。",
                parent=self.root,
            )
            return
        if not os.path.isdir(game):
            messagebox.showerror("目录不存在", "游戏目录不存在：\n%s" % game, parent=self.root)
            return
        if not custom_albums or not os.path.isdir(custom_albums):
            messagebox.showerror(
                "找不到 Custom_Albums",
                "在游戏目录中找不到 Custom_Albums 文件夹：\n%s\n\n"
                "请确认选择的是 Muse Dash 游戏根目录。" % game,
                parent=self.root,
            )
            return
        self.scan_stats_var.set("扫描统计：扫描中…")
        self._append_log("扫描目录：" + custom_albums)
        self._start_operation("扫描", lambda: self._work_scan(custom_albums))

    def on_cancel(self):
        if not self.busy or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self._append_log("已请求取消，正在等待当前步骤结束…")
        self.status_var.set("正在取消…")
        try:
            self.btn_cancel.configure(state="disabled")
        except tk.TclError:
            pass

    def on_convert(self):
        if self.busy:
            return
        if not self.items:
            messagebox.showinfo("请先扫描", "请先点击【扫描】列出需要处理的谱面。", parent=self.root)
            return
        plan = self.plan_var.get()
        if plan not in PLAN_TEXTS:
            plan = PLAN_CAM
        # 方案2 是“迁移为原生格式”，合法 CAM 谱面也可以转换；
        # 其它方案只处理损坏/需要修复的谱面。
        if plan == PLAN_EUTERPE:
            targets = list(self.items)
        else:
            targets = [item for item in self.items if _item_needs_fix(item)]
        if not targets:
            messagebox.showinfo(
                "无需处理",
                "扫描结果中没有需要修复的谱面。"
                if plan != PLAN_EUTERPE
                else "扫描结果中没有可转换的谱面。",
                parent=self.root,
            )
            return
        game, custom_albums, offline = self._resolve_paths()
        if not game:
            messagebox.showwarning(
                "请选择游戏目录",
                "请先选择 Muse Dash 游戏根目录，或点击【自动检测】。",
                parent=self.root,
            )
            return
        plan = self.plan_var.get()
        if plan not in PLAN_TEXTS:
            plan = PLAN_CAM
        plan_text = PLAN_TEXTS[plan]
        if plan in PLAN_NEEDS_CUSTOM_ALBUMS and (
            not custom_albums or not os.path.isdir(custom_albums)
        ):
            messagebox.showerror(
                "找不到 Custom_Albums",
                "所选方案需要 Custom_Albums 目录（解压为 CAM 文件夹）：\n%s\n\n"
                "请确认游戏目录是否正确。" % (custom_albums or game),
                parent=self.root,
            )
            return
        if plan in PLAN_NEEDS_OFFLINE and not offline:
            messagebox.showerror(
                "无法确定 Euterpe 离线目录",
                "所选方案需要 <游戏目录>/Euterpe_Charts/Offline 目录。\n"
                "请确认游戏目录是否正确。",
                parent=self.root,
            )
            return
        backup_dir = self._backup_dir_for(game)
        dry_run = bool(self.dry_run_var.get())
        if not dry_run:
            detail = (
                "即将对 %d 个谱面执行：\n%s\n\n"
                "Custom_Albums：\n%s\n\n"
                "Euterpe 离线目录：\n%s\n\n"
                "备份目录：\n%s\n\n建议先关闭游戏再执行。是否继续？"
                % (len(targets), plan_text, custom_albums or "（不使用）", offline or "（不使用）", backup_dir)
            )
            if plan == PLAN_BOTH:
                detail += "\n注意：方案④ 会同时生成 CAM 文件夹与 Euterpe 离线谱，可能产生重复项。"
            if not messagebox.askyesno("确认执行", detail, parent=self.root):
                return
        self._active_backup_dir = backup_dir
        self._start_operation(
            "转换",
            lambda: self._work_convert(targets, plan, game, custom_albums, offline, dry_run),
        )

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    def _work_detect(self):
        path = None
        error = None
        try:
            module = self._require_core()
            path = module.detect_game_dir()
        except Exception as exc:  # noqa: BLE001
            error = _short_exc(exc)
        self.queue.put(("detect_done", path, error))

    def _work_scan(self, root_dir):
        module = self._require_core()
        items = module.scan([root_dir], recursive=True, progress=self._progress)
        if items is None:
            raise RuntimeError("core.scan 返回了空结果（None）")
        self.queue.put(("scan_done", list(items)))

    def _work_convert(self, items, plan, game_dir, custom_albums, offline_dir, dry_run):
        module = self._require_core()
        results = module.convert(
            items,
            plan,
            game_dir=game_dir,
            custom_albums_dir=custom_albums,
            offline_dir=offline_dir,
            dry_run=dry_run,
            progress=self._progress,
        )
        if results is None:
            raise RuntimeError("core.convert 返回了空结果（None）")
        self.queue.put(("convert_done", list(results), dry_run))

    # ------------------------------------------------------------------
    # 结果展示
    # ------------------------------------------------------------------
    def _show_scan_items(self, items):
        self.items = list(items)
        self.items_by_path = {}
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            return
        needs_fix_count = 0
        for item in self.items:
            name = _item_name(item)
            author = _item_author(item)
            problems = _item_issues(item) + ["提示：" + text for text in _item_warnings(item)]
            issues = "；".join(problems) or "—"
            if len(issues) > 260:
                issues = issues[:258] + "…"
            needs_fix = _item_needs_fix(item)
            if needs_fix:
                needs_fix_count += 1
            status = "待修复" if needs_fix else "正常"
            try:
                row = self.tree.insert(
                    "",
                    "end",
                    values=(name, author, issues, status),
                    tags=("needs_fix" if needs_fix else "ok",),
                )
            except tk.TclError:
                continue
            path = str(getattr(item, "path", "") or "")
            self.items_by_path[path] = row
        total = len(self.items)
        self.scan_stats_var.set(
            "扫描统计：共 %d 项，待修复 %d 项，正常 %d 项" % (total, needs_fix_count, total - needs_fix_count)
        )
        self._append_log("扫描完成：共 %d 个候选项，其中 %d 个需要修复。" % (total, needs_fix_count))
        for item in self.items:
            if not _item_needs_fix(item):
                continue
            path = str(getattr(item, "path", "") or "")
            self._append_log("  [待修复] %s（%s）" % (_item_name(item), path))
            for issue in _item_issues(item):
                self._append_log("      - " + issue)
            for warning in _item_warnings(item):
                self._append_log("      - 提示：" + warning)
        if not self.items:
            self.scan_stats_var.set("扫描统计：未发现谱面")
            self.status_var.set("扫描完成：未发现谱面")
            messagebox.showinfo(
                "扫描完成",
                "未在 Custom_Albums 中发现任何 .mdm 谱面或谱面文件夹。",
                parent=self.root,
            )
            return
        if needs_fix_count == 0:
            self.status_var.set("扫描完成：全部正常")
            messagebox.showinfo(
                "扫描完成",
                "共发现 %d 个谱面，全部正常，无需修复。" % len(self.items),
                parent=self.root,
            )
            return
        self.status_var.set("扫描完成：%d 项待修复" % needs_fix_count)
        self._append_log("请选择处理方案后点击【开始转换】。")

    def _show_convert_results(self, results, dry_run):
        counts = {"success": 0, "skipped": 0, "failed": 0}
        labels = {"success": "成功", "skipped": "跳过", "failed": "失败"}
        for result in results:
            category = _result_status(result)
            counts[category] += 1
            item = _result_item(result)
            name = _item_name(item) if item is not None else "（未知项）"
            message = str(getattr(result, "message", "") or "")
            target = str(getattr(result, "target", "") or "")
            line = "[%s] %s" % (labels[category], name)
            if target:
                line += " -> " + target
            if message:
                line += "（%s）" % message
            self._append_log(line)
            if item is not None and not dry_run:
                path = str(getattr(item, "path", "") or "")
                self._update_row_status(path, labels[category])
        self._append_log(
            "统计：成功 %d 项 / 跳过 %d 项 / 失败 %d 项。"
            % (counts["success"], counts["skipped"], counts["failed"])
        )
        if dry_run:
            self._append_log("（仅预览模式，未写入任何文件。）")
        elif self._active_backup_dir:
            self._append_log("原 info.json 备份目录：" + self._active_backup_dir)
        if not results:
            self.status_var.set("转换完成：没有返回结果")
            messagebox.showwarning(
                "转换完成",
                "core.convert 没有返回任何处理结果，请查看运行日志。",
                parent=self.root,
            )
            return
        summary = [
            "成功：%d 项" % counts["success"],
            "跳过：%d 项" % counts["skipped"],
            "失败：%d 项" % counts["failed"],
        ]
        if dry_run:
            summary.insert(0, "（仅预览模式，未写入任何文件）\n")
        elif self._active_backup_dir:
            summary.append("\n备份目录：\n" + self._active_backup_dir)
        body = "\n".join(summary)
        self.status_var.set(
            "转换完成：成功 %d / 跳过 %d / 失败 %d"
            % (counts["success"], counts["skipped"], counts["failed"])
        )
        if counts["failed"]:
            messagebox.showwarning(
                "转换完成（有失败项）",
                body + "\n\n详情请查看运行日志，或点击【保存日志】。",
                parent=self.root,
            )
        else:
            messagebox.showinfo("转换完成", body, parent=self.root)

    def _update_row_status(self, path, text):
        row = self.items_by_path.get(path)
        if not row:
            return
        try:
            values = list(self.tree.item(row, "values"))
        except tk.TclError:
            return
        if len(values) >= 4:
            values[3] = text
            try:
                self.tree.item(row, values=values)
            except tk.TclError:
                pass

    def _on_tree_double_click(self, event):
        try:
            row = self.tree.identify_row(event.y)
        except tk.TclError:
            return
        if not row:
            return
        try:
            values = self.tree.item(row, "values")
        except tk.TclError:
            return
        self._append_log("选中谱面：" + " | ".join(str(value) for value in values))

    # ------------------------------------------------------------------
    # 目录 / 日志按钮
    # ------------------------------------------------------------------
    def _open_path(self, path):
        if not os.path.exists(path):
            messagebox.showinfo("目录不存在", "目录不存在：\n%s" % path, parent=self.root)
            return
        try:
            _open_with_system(path)
        except Exception as exc:
            messagebox.showerror(
                "无法打开目录",
                "%s\n\n%s" % (path, _short_exc(exc)),
                parent=self.root,
            )

    def on_open_custom_albums(self):
        _game, custom_albums, _offline = self._resolve_paths()
        if not custom_albums:
            messagebox.showinfo(
                "未找到 Custom_Albums",
                "请先选择正确的 Muse Dash 游戏根目录。",
                parent=self.root,
            )
            return
        self._open_path(custom_albums)

    def on_open_offline(self):
        _game, _custom_albums, offline = self._resolve_paths()
        if not offline:
            messagebox.showinfo(
                "未找到 Euterpe 离线目录",
                "请先选择正确的 Muse Dash 游戏根目录。",
                parent=self.root,
            )
            return
        if not os.path.isdir(offline):
            messagebox.showinfo(
                "目录尚未创建",
                "%s\n\n执行方案③/④ 转换后会自动创建。" % offline,
                parent=self.root,
            )
            return
        self._open_path(offline)

    def on_open_backup(self):
        game, _custom_albums, _offline = self._resolve_paths()
        if not game:
            messagebox.showinfo(
                "未找到游戏目录",
                "请先选择正确的 Muse Dash 游戏根目录。",
                parent=self.root,
            )
            return
        backup_dir = self._backup_dir_for(game)
        if not os.path.isdir(backup_dir):
            messagebox.showinfo(
                "备份目录尚未创建",
                "%s\n\n首次执行转换时会自动创建，并保存原始 info.json。" % backup_dir,
                parent=self.root,
            )
            return
        self._open_path(backup_dir)

    def on_save_log(self):
        content = "\n".join(self.log_lines).strip()
        if not content:
            messagebox.showinfo("没有日志", "当前还没有日志内容。", parent=self.root)
            return
        default_name = "MuseDashChartFixer_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            path = filedialog.asksaveasfilename(
                parent=self.root,
                title="保存运行日志",
                defaultextension=".log",
                initialfile=default_name,
                filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
        except tk.TclError:
            path = ""
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig") as handle:
                handle.write(content + "\n")
        except OSError as exc:
            messagebox.showerror("保存失败", "%s\n\n%s" % (path, _short_exc(exc)), parent=self.root)
            return
        self._append_log("日志已保存到：" + path)
        self.status_var.set("日志已保存")
        messagebox.showinfo("保存成功", "日志已保存：\n%s" % path, parent=self.root)

    # ------------------------------------------------------------------
    # 关闭窗口
    # ------------------------------------------------------------------
    def _report_callback_exception(self, exc_type, exc_value, exc_tb):
        try:
            self._append_log("界面回调异常：" + str(exc_value))
            for line in traceback.format_exception(exc_type, exc_value, exc_tb):
                for sub in str(line).rstrip().splitlines():
                    self._append_log("    " + sub)
        except Exception:
            pass

    def _on_close(self):
        if self.busy:
            confirmed = messagebox.askyesno(
                "正在处理",
                "当前还有操作正在进行。\n要取消并退出吗？",
                parent=self.root,
            )
            if not confirmed:
                return
            self.cancel_event.set()
        self._closed = True
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv=None):
    del argv  # 图形界面模式忽略命令行参数
    _enable_dpi_awareness()
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        _print_error("无法启动图形界面：" + _short_exc(exc))
        return 1
    try:
        ChartFixerApp(root)
        root.mainloop()
    except Exception as exc:  # noqa: BLE001
        _print_error("图形界面运行出错：" + _short_exc(exc))
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())