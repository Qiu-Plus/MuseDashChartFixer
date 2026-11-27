# -*- coding: utf-8 -*-
"""Muse Dash 谱面急救箱 —— 命令行模式（仅标准库）。

由 main.py 在 argv[0] == "--cli" 时调用：return cli.main(argv[1:])。

plan 取值：cam（就地修复 .mdm，默认，推荐）/ euterpe（转换为 Euterpe 离线谱）。

用法示例：
    python main.py --cli --scan "D:\\Muse Dash\\Custom_Albums"
    python main.py --cli --game-dir "D:\\Muse Dash" --plan cam --dry-run
    python main.py --cli --scan "D:\\Muse Dash\\Custom_Albums" --plan euterpe --json

退出码：0=成功；1=有谱面转换失败；2=参数或环境错误；130=用户中断。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import traceback

try:
    import core
except Exception as _core_exc:  # pragma: no cover - 仅在 core.py 无法导入时发生
    core = None
    _CORE_IMPORT_ERROR = _core_exc
else:
    _CORE_IMPORT_ERROR = None


EXIT_OK = 0
EXIT_FAILED_ITEMS = 1
EXIT_ERROR = 2
EXIT_CANCELLED = 130

CA_DIR_NAMES = {"custom_albums", "customalbums", "custom albums"}

PLAN_TEXT = {
    "cam": "方案1 修复 .mdm（就地修复包内 info.json，推荐）",
    "euterpe": "方案2 转换为 Euterpe 离线谱",
}
PLAN_NEEDS_CUSTOM_ALBUMS = ()
PLAN_NEEDS_OFFLINE = ("euterpe",)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _clean_path(raw):
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text))


def _looks_like_custom_albums(path):
    try:
        return os.path.basename(os.path.normpath(path)).lower() in CA_DIR_NAMES
    except Exception:
        return False


def _short_exc(exc):
    if isinstance(exc, NotImplementedError):
        return "core 模块对应功能尚未实现（NotImplementedError）"
    text = str(exc).strip()
    if text:
        return "%s: %s" % (type(exc).__name__, text)
    return type(exc).__name__


def _item_name(item):
    if item is None:
        return "（未知项）"
    name = str(getattr(item, "chart_name", "") or "").strip()
    if name:
        return name
    path = str(getattr(item, "path", "") or "")
    base = os.path.basename(path.rstrip("\\/")) if path else ""
    if base.lower().endswith(".mdm"):
        base = base[:-4]
    return base or "（未命名）"


def _item_needs_fix(item):
    return bool(getattr(item, "needs_fix", False))


def _string_list(item, attr):
    values = getattr(item, attr, None) or []
    try:
        return [str(value).strip() for value in values if str(value).strip()]
    except TypeError:
        return [str(values)]


def _item_issues(item):
    return _string_list(item, "issues")


def _item_errors(item):
    return _string_list(item, "errors")


def _item_warnings(item):
    return _string_list(item, "warnings")


_SKIP_MESSAGE_KEYWORDS = ("跳过", "已存在", "仅适用", "不适用", "无需")


def _classify_result(result):
    ok = bool(getattr(result, "ok", False))
    message = str(getattr(result, "message", "") or "")
    if any(keyword in message for keyword in _SKIP_MESSAGE_KEYWORDS):
        return "skipped"
    return "success" if ok else "failed"


def _item_to_dict(item):
    return {
        "path": str(getattr(item, "path", "") or ""),
        "kind": str(getattr(item, "kind", "") or ""),
        "chart_name": str(getattr(item, "chart_name", "") or ""),
        "author": str(getattr(item, "author", "") or ""),
        "issues": _item_issues(item),
        "errors": _item_errors(item),
        "warnings": _item_warnings(item),
        "needs_fix": _item_needs_fix(item),
    }


def _result_to_dict(result):
    item = getattr(result, "item", None)
    return {
        "path": str(getattr(item, "path", "") or "") if item is not None else "",
        "chart_name": _item_name(item) if item is not None else "",
        "ok": bool(getattr(result, "ok", False)),
        "target": getattr(result, "target", None),
        "message": str(getattr(result, "message", "") or ""),
        "category": _classify_result(result),
    }


# ---------------------------------------------------------------------------
# 输出（人类可读 / JSON 二选一）
# ---------------------------------------------------------------------------
class _Output:
    def __init__(self, json_mode=False):
        self.json_mode = bool(json_mode)
        self.errors = []
        self.payload = {
            "ok": True,
            "exit_code": EXIT_OK,
            "errors": [],
            "game_dir": None,
            "custom_albums_dir": None,
            "offline_dir": None,
            "backup_dir": None,
            "scan_paths": [],
            "plan": None,
            "dry_run": False,
            "scan": None,
            "convert": None,
            "stats": {},
        }

    def info(self, message=""):
        if self.json_mode:
            return
        self._emit(str(message), "stdout")

    def progress(self, message=""):
        text = str(message).strip()
        if not text:
            return
        if self.json_mode:
            self._emit("… " + text, "stderr")
        else:
            self._emit("  … " + text, "stdout")

    def warn(self, message):
        self._emit("警告：" + str(message), "stderr")

    def error(self, message):
        text = str(message)
        self.errors.append(text)
        self._emit("错误：" + text, "stderr")

    def _emit(self, text, stream):
        try:
            target = sys.stdout if stream == "stdout" else sys.stderr
            if target is None:
                return
            print(text, file=target)
        except Exception:
            pass

    def finalize(self, code):
        self.payload["exit_code"] = int(code)
        self.payload["ok"] = int(code) == EXIT_OK
        self.payload["errors"] = list(self.errors)
        if not self.json_mode:
            return
        try:
            data = json.dumps(self.payload, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 JSON 输出失败
            data = json.dumps(
                {"ok": False, "exit_code": EXIT_ERROR, "error": "JSON 序列化失败：%s" % exc},
                ensure_ascii=False,
            )
        self._emit(data, "stdout")


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py --cli",
        description="Muse Dash 谱面急救箱（喵斯兔损坏谱面修复）命令行模式。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python main.py --cli --scan "D:\\Muse Dash\\Custom_Albums"\n'
            '  python main.py --cli --game-dir "D:\\Muse Dash" --plan cam --dry-run\n'
            '  python main.py --cli --scan "D:\\Muse Dash\\Custom_Albums" --plan euterpe --json\n'
            "\n退出码：0=成功；1=有谱面转换失败；2=参数或环境错误；130=用户中断。"
        ),
    )
    parser.add_argument(
        "--game-dir",
        metavar="PATH",
        default=None,
        help="Muse Dash 游戏根目录（用于定位 Custom_Albums、Euterpe_Charts/Offline 与备份目录）",
    )
    parser.add_argument(
        "--scan",
        metavar="PATH",
        dest="scan_paths",
        action="append",
        default=None,
        help="要扫描的目录或 .mdm 文件，可多次指定；省略时自动扫描游戏目录下的 Custom_Albums",
    )
    parser.add_argument(
        "--plan",
        choices=("cam", "euterpe"),
        default=None,
        help=(
            "转换方案：cam=就地修复 .mdm（默认，推荐）；"
            "euterpe=转换为 Euterpe 离线谱；不指定则只扫描不写入"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不写入任何文件（需配合 --plan 使用）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出完整结果（便于脚本处理）",
    )
    parser.add_argument("--version", action="version", version="MuseDashChartFixer 1.1.0")
    return parser


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _ensure_standard_streams():
    """兼容 PyInstaller --windowed：无控制台时 sys.stdout/stderr 可能为 None。

    如果从命令行启动，先尝试附加到父进程控制台；否则退化为内存流，
    保证 argparse 的 --help / 错误提示不会因为 stdout 为 None 而崩溃。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.kernel32.AttachConsole(-1)
        except Exception:
            pass
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                try:
                    setattr(
                        sys,
                        name,
                        open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1),
                    )
                    continue
                except Exception:
                    pass
                setattr(sys, name, io.StringIO())
        return
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, io.StringIO())


def _configure_encoding():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _find_custom_albums_safe(game_dir, output):
    if not game_dir:
        return None
    try:
        found = core.find_custom_albums(game_dir)
    except Exception as exc:  # noqa: BLE001
        output.warn("定位 Custom_Albums 失败：" + _short_exc(exc))
        return None
    if found:
        return found
    candidate = os.path.join(game_dir, "Custom_Albums")
    if os.path.isdir(candidate):
        return candidate
    return None


def _default_offline_dir_safe(game_dir, output):
    if not game_dir:
        return ""
    found = ""
    try:
        found = core.default_offline_dir(game_dir) or ""
    except Exception as exc:  # noqa: BLE001
        output.warn("计算 Euterpe 离线目录失败：" + _short_exc(exc))
    if found:
        return found
    return os.path.join(game_dir, "Euterpe_Charts", "Offline")


def _derive_custom_albums_from_paths(paths):
    for path in paths:
        current = os.path.abspath(path)
        if os.path.isfile(current):
            current = os.path.dirname(current)
        if _looks_like_custom_albums(current):
            return current
    return None


def _run(args, output):
    if core is None:
        output.error("无法导入 core 模块：" + _short_exc(_CORE_IMPORT_ERROR))
        return EXIT_ERROR

    game_dir = _clean_path(args.game_dir) if args.game_dir else ""
    direct_custom_albums = None
    if game_dir:
        if _looks_like_custom_albums(game_dir):
            direct_custom_albums = game_dir
            game_dir = os.path.dirname(game_dir)
        if not os.path.isdir(game_dir):
            output.error("游戏目录不存在：%s" % game_dir)
            return EXIT_ERROR

    scan_paths = []
    for raw in (args.scan_paths or []):
        cleaned = _clean_path(raw)
        if cleaned:
            scan_paths.append(cleaned)
    if scan_paths:
        missing = [path for path in scan_paths if not os.path.exists(path)]
        if missing:
            for path in missing:
                output.error("扫描路径不存在：%s" % path)
            return EXIT_ERROR
    else:
        candidate = direct_custom_albums or _find_custom_albums_safe(game_dir, output)
        if candidate:
            scan_paths = [candidate]
    if not scan_paths:
        output.error(
            "未指定扫描路径：请使用 --scan PATH 指定要扫描的目录或 .mdm 文件，"
            "或使用 --game-dir 指定游戏目录以便自动定位 Custom_Albums。"
        )
        return EXIT_ERROR

    custom_albums_dir = direct_custom_albums or _find_custom_albums_safe(game_dir, output)
    if not custom_albums_dir:
        custom_albums_dir = _derive_custom_albums_from_paths(scan_paths)
    if not game_dir and custom_albums_dir:
        # 从 Custom_Albums 反推游戏根目录，便于确定 <游戏目录>/_ChartFixerBackup
        game_dir = os.path.dirname(os.path.abspath(custom_albums_dir))
    offline_dir = _default_offline_dir_safe(game_dir, output) if game_dir else ""
    if not offline_dir and custom_albums_dir:
        offline_dir = os.path.join(
            os.path.dirname(os.path.abspath(custom_albums_dir)), "Euterpe_Charts", "Offline"
        )
    backup_dir = os.path.join(game_dir, "_ChartFixerBackup") if game_dir else ""

    plan = args.plan
    dry_run = bool(args.dry_run)
    if dry_run and not plan:
        output.warn("--dry-run 需要配合 --plan 才有意义；当前仅扫描，不写入任何文件。")
    if plan == "cam" and not game_dir:
        output.error(
            "所选方案需要游戏根目录（用于把原始 info.json 备份到 <游戏目录>/_ChartFixerBackup）："
            "请用 --game-dir 指定，或让 --scan 指向 Custom_Albums 目录。"
        )
        return EXIT_ERROR
    if plan in PLAN_NEEDS_CUSTOM_ALBUMS and not custom_albums_dir:
        output.error("所选方案需要 Custom_Albums 目录：请用 --game-dir 指定 Muse Dash 游戏根目录。")
        return EXIT_ERROR
    if plan in PLAN_NEEDS_OFFLINE and not offline_dir:
        output.error(
            "所选方案需要 Euterpe 离线目录（<游戏目录>/Euterpe_Charts/Offline）："
            "请用 --game-dir 指定游戏根目录。"
        )
        return EXIT_ERROR

    output.payload.update(
        {
            "game_dir": game_dir or None,
            "custom_albums_dir": custom_albums_dir or None,
            "offline_dir": offline_dir or None,
            "backup_dir": backup_dir or None,
            "scan_paths": list(scan_paths),
            "plan": plan,
            "dry_run": dry_run,
        }
    )

    output.info("Muse Dash 谱面急救箱 — 命令行模式")
    output.info("游戏目录：%s" % (game_dir or "（未指定）"))
    output.info("Custom_Albums：%s" % (custom_albums_dir or "（未找到）"))
    output.info("Euterpe 离线目录：%s" % (offline_dir or "（未找到）"))
    output.info("备份目录：%s" % (backup_dir or "（未指定游戏目录）"))
    output.info("扫描路径（%d 个）：" % len(scan_paths))
    for path in scan_paths:
        output.info("  - " + path)
    output.info("正在扫描…")

    try:
        items = core.scan(
            scan_paths,
            recursive=True,
            progress=lambda message: output.progress(str(message)),
        )
    except Exception as exc:  # noqa: BLE001
        output.error("扫描失败：" + _short_exc(exc))
        return EXIT_ERROR
    if items is None:
        output.error("扫描失败：core.scan 返回了 None")
        return EXIT_ERROR
    items = list(items)
    need_fix = [item for item in items if _item_needs_fix(item)]

    output.payload["scan"] = [_item_to_dict(item) for item in items]
    output.payload["stats"] = {
        "scan_total": len(items),
        "needs_fix": len(need_fix),
        "success": 0,
        "skipped": 0,
        "failed": 0,
    }

    output.info("")
    output.info("扫描完成：共 %d 个候选项，其中 %d 个需要修复。" % (len(items), len(need_fix)))
    for index, item in enumerate(items, 1):
        status = "待修复" if _item_needs_fix(item) else "正常"
        output.info(
            "[%d/%d] [%s] %s  作者：%s"
            % (
                index,
                len(items),
                status,
                _item_name(item),
                str(getattr(item, "author", "") or "—"),
            )
        )
        path = str(getattr(item, "path", "") or "")
        if path:
            output.info("      路径：" + path)
        issues = _item_issues(item)
        if issues:
            output.info("      问题：" + "；".join(issues))
        warnings = _item_warnings(item)
        if warnings:
            output.info("      提示：" + "；".join(warnings))

    if not plan:
        output.info("")
        output.info("当前为「仅扫描」模式（未指定 --plan），没有写入任何文件。")
        if need_fix:
            output.info(
                "如需修复，请追加参数：--plan cam|euterpe（可再加 --dry-run 预览）。"
            )
        return EXIT_OK

    # 方案2 是“迁移为原生格式”，合法 CAM 谱面也可以转换；其它方案只处理待修复谱面。
    convert_items = list(items) if plan == "euterpe" else list(need_fix)
    if not convert_items:
        output.info("")
        output.info("没有需要转换的谱面，跳过转换。")
        return EXIT_OK

    output.info("")
    output.info(
        "开始转换：%s%s"
        % (PLAN_TEXT.get(plan, plan), "（仅预览，不写入文件）" if dry_run else "")
    )
    try:
        results = core.convert(
            convert_items,
            plan,
            game_dir=game_dir or None,
            custom_albums_dir=custom_albums_dir or None,
            offline_dir=offline_dir or None,
            dry_run=dry_run,
            progress=lambda message: output.progress(str(message)),
        )
    except Exception as exc:  # noqa: BLE001
        output.error("转换失败：" + _short_exc(exc))
        return EXIT_ERROR
    if results is None:
        output.error("转换失败：core.convert 返回了 None")
        return EXIT_ERROR
    results = list(results)

    stats = {"success": 0, "skipped": 0, "failed": 0}
    labels = {"success": "成功", "skipped": "跳过", "failed": "失败"}
    output.info("")
    for result in results:
        category = _classify_result(result)
        stats[category] += 1
        item = getattr(result, "item", None)
        name = _item_name(item)
        message = str(getattr(result, "message", "") or "")
        target = str(getattr(result, "target", "") or "")
        line = "[%s] %s" % (labels[category], name)
        if target:
            line += " -> " + target
        if message:
            line += "（%s）" % message
        output.info(line)

    output.payload["convert"] = [_result_to_dict(result) for result in results]
    output.payload["stats"].update(stats)

    output.info("")
    output.info(
        "转换完成：成功 %d 项 / 跳过 %d 项 / 失败 %d 项。"
        % (stats["success"], stats["skipped"], stats["failed"])
    )
    if dry_run:
        output.info("（以上为仅预览结果，未写入任何文件。）")
    elif backup_dir:
        output.info("原 info.json 备份目录：%s" % backup_dir)
    if not results:
        output.error("转换没有产生任何结果。")
        return EXIT_FAILED_ITEMS
    if stats["failed"]:
        return EXIT_FAILED_ITEMS
    return EXIT_OK


def main(argv=None):
    _ensure_standard_streams()
    _configure_encoding()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_OK
        try:
            return int(code)
        except (TypeError, ValueError):
            return EXIT_ERROR
    output = _Output(json_mode=bool(args.json))
    try:
        code = _run(args, output)
    except KeyboardInterrupt:
        output.error("已取消（Ctrl+C）。")
        code = EXIT_CANCELLED
    except Exception as exc:  # noqa: BLE001 - 命令行入口必须兜住所有异常
        output.error("发生未处理的异常：" + _short_exc(exc))
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        code = EXIT_ERROR
    output.finalize(code)
    return code


if __name__ == "__main__":
    sys.exit(main())