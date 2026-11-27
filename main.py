"""MuseDashChartFixer 入口：无参数启动 GUI；带 --cli 进入命令行模式。"""
import os
import sys
import traceback


def _frozen_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _log_crash(exc_text):
    """GUI 启动/运行异常时写日志，方便用户反馈（窗口模式下没有控制台）。"""
    candidates = [
        os.path.join(_frozen_dir(), "MuseDashChartFixer_crash.log"),
        os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".", "MuseDashChartFixer_crash.log"),
    ]
    for path in candidates:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(exc_text + "\n")
            return path
        except Exception:
            continue
    return None


def _ensure_console_for_cli():
    """--windowed 打包后 exe 没有控制台；--cli 模式时借用父控制台并重开标准流。"""
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(-1):
            kernel32.AllocConsole()
        try:
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except Exception:
            pass
    except Exception:
        pass


def _show_startup_error(text):
    """窗口模式下启动失败时，尽量弹一个错误框，避免“双击没反应”。"""
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Muse Dash 谱面急救箱 - 启动失败", text)
        root.destroy()
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--cli":
        _ensure_console_for_cli()
        import cli
        return cli.main(argv[1:])
    try:
        import gui
        return gui.main()
    except Exception:
        detail = traceback.format_exc()
        log_path = _log_crash(detail)
        hint = ("程序启动时发生错误：\n\n" + detail[-1500:] +
                "\n\n日志文件：" + (log_path or "（写入失败）"))
        _show_startup_error(hint)
        raise


if __name__ == "__main__":
    sys.exit(main())