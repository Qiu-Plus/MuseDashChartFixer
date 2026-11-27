"""MuseDashChartFixer 核心逻辑（仅标准库）。实现细节见 SPEC.md（含修订 R2）。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Optional


# ============================================================================
# 常量与正则
# ============================================================================

#: 合法场景编号（scene_01 .. scene_12）
SCENE_PATTERN = re.compile(r"^scene_(0[1-9]|1[0-2])$")

#: 合法隐藏模式
HIDE_MODE_VALUES = ("CLICK", "PRESS", "TOGGLE")

#: Custom_Albums 目录的常见拼写（比较时统一 casefold）
CAM_DIR_NAMES = ("custom_albums", "customalbums", "custom albums", "custom_album")

#: 合法 CAM info.json 必填的顶层字段（顺序与 build_cam_info 一致）
CAM_TOP_KEYS = (
    "name",
    "author",
    "bpm",
    "scene",
    "sceneEgg",
    "levelDesigner",
    "levelDesigner1",
    "levelDesigner2",
    "levelDesigner3",
    "levelDesigner4",
    "levelDesigner5",
    "difficulty1",
    "difficulty2",
    "difficulty3",
    "difficulty4",
    "difficulty5",
    "hideBmsMode",
    "hideBmsDifficulty",
    "hideBmsMessage",
    "unlockLevel",
    "searchTags",
    "battleUi",
)

#: Windows 保留文件名（小写保存，比较时用 casefold；"CON"、"CON.txt" 都非法）
WINDOWS_RESERVED_NAMES = frozenset(
    name.casefold()
    for name in (["CON", "PRN", "AUX", "NUL"]
                 + ["COM%d" % i for i in range(1, 10)]
                 + ["LPT%d" % i for i in range(1, 10)])
)

#: Windows 非法文件名字符
ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: UTF-8 BOM
UTF8_BOM = b"\xef\xbb\xbf"

#: 包内需要忽略、不写进 manifest.files 的条目
IGNORED_MANIFEST_FILES = frozenset(["info.json", "manifest.epk"])

#: 方案1 中包内含 map4/map5 的警告文案（SPEC R2 §8）
MAP45_WARNING = (
    "包含 map4/map5 难度：方案 1 的 .mdm 导入可能只保留前三个难度，更推荐方案 2"
)

#: CAM 信息里合法的难度/谱师槽位编号
MAP_SLOTS = (1, 2, 3, 4, 5)


# ============================================================================
# 数据结构（字段与 SPEC §5 完全一致，GUI/CLI 依赖）
# ============================================================================


@dataclass
class ScanItem:
    """扫描结果：一个 .mdm 文件或一个 CAM 文件夹。"""

    path: str
    kind: str  # "mdm" | "folder"
    chart_name: str = ""
    author: str = ""
    issues: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    needs_fix: bool = False
    payload: dict | None = None


@dataclass
class ActionResult:
    """单个谱面的转换结果（convert 返回，不会抛异常给 UI）。"""

    item: ScanItem
    ok: bool
    target: str | None = None
    message: str = ""


# ============================================================================
# 通用小工具
# ============================================================================


def _notify(progress: Optional[Callable[[str], None]], message: str) -> None:
    """安全地调用进度回调；回调自身出错也绝不向外抛。"""
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        pass


def _number_text(value) -> str:
    """把 int/float 转成不带冗余 0 的字符串（94.1 -> "94.1"，160.0 -> "160"）。"""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        text = ("%.6f" % value).rstrip("0").rstrip(".")
        return text if text else "0"
    return ""


def _text(value) -> str:
    """把任意值安全地转成字符串；None/bool/非法类型返回空串。"""
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _number_text(value)
    return ""


def _pick_text(*values, default: str = "") -> str:
    """按顺序取第一个非空字符串值。"""
    for value in values:
        text = _text(value).strip()
        if text:
            return text
    return default


def _parse_positive_number(value) -> Optional[float | int]:
    """解析正数（bpm），整数返回 int，小数返回 float；非法/非正返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text:
                return None
            number = float(text)
        else:
            return None
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number.is_integer() and abs(number) < 1e16:
        return int(number)
    return float(number)


def _rating_text(value) -> str:
    """把难度值统一成字符串（保持 "8.2"/"?"/"10" 这样的原始形态）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return _number_text(value)
    return ""


def _parse_bpm_text(text: str) -> str:
    """bpm 的规范字符串：int -> "160"，float -> "94.1"。"""
    return _number_text(_parse_positive_number(text) if not isinstance(text, (int, float)) else text)


def _format_bpm(value) -> str:
    """把 manifest 里的 bpm（int/float）格式化成 CAM 需要的字符串。"""
    number = _parse_positive_number(value)
    if number is None:
        return "120"
    return _number_text(number)


def _norm_pkg_name(name: str) -> str:
    """包内条目名统一成 POSIX 风格、去掉前导斜杠。"""
    return str(name or "").replace("\\", "/").lstrip("/")


def _basename_lower(name: str) -> str:
    """包内条目的 basename 小写。"""
    return _norm_pkg_name(name).rsplit("/", 1)[-1].casefold()


def _is_symlink_zipinfo(info: zipfile.ZipInfo) -> bool:
    """判断 zip 条目是否是符号链接（防止解压出逃逸链接）。"""
    try:
        mode = (info.external_attr >> 16) & 0o170000
    except Exception:
        return False
    return mode == 0o120000


# ----------------------------------------------------------------------------
# 文件名校验（Windows 非法字符 / 保留名 / 尾部空格与点）
# ----------------------------------------------------------------------------


def _safe_component(name: str) -> str:
    """清洗单个文件名/目录名片段，返回可以安全落盘的名称。"""
    text = str(name or "")
    # 去掉 BOM、换行等控制字符
    text = text.replace("\ufeff", "").replace("\u3000", " ")
    text = ILLEGAL_FILENAME_CHARS.sub("_", text).strip()
    text = text.rstrip(" .")
    if not text:
        return ""
    # Windows 保留名（"CON"、"CON.txt" 都非法）
    stem = text.split(".", 1)[0].strip().casefold()
    if stem in WINDOWS_RESERVED_NAMES:
        text = "_" + text
    if len(text) > 120:
        text = text[:120].rstrip(" .")
    return text


def _safe_filename(name: str, default: str = "Untitled") -> str:
    """生成安全的文件夹名 / 谱面名。"""
    text = _safe_component(name)
    if not text or text in (".", ".."):
        return default
    return text


def _safe_member_relpath(name: str) -> Optional[str]:
    """把 zip/包内条目名转成安全的相对路径；穿越/空路径返回 None。"""
    normalized = _norm_pkg_name(name)
    parts = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            # 拒绝任何向上穿越
            return None
        clean = _safe_component(part)
        if not clean:
            continue
        parts.append(clean)
    if not parts:
        return None
    return "/".join(parts)


def _is_inside(root: str, target: str) -> bool:
    """判断 target 是否安全地位于 root 目录内部。"""
    try:
        root_real = os.path.normcase(os.path.realpath(root))
        target_real = os.path.normcase(os.path.realpath(target))
        return os.path.commonpath([root_real, target_real]) == root_real
    except (OSError, ValueError):
        return False


def _decode_text_bytes(data: bytes) -> str:
    """解码 BMS/文本字节：优先 UTF-8（含 BOM），失败再试日文 cp932。"""
    if not isinstance(data, (bytes, bytearray)):
        return ""
    raw = bytes(data)
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _read_text_file(path: str) -> str:
    """读取文本文件，带编码兜底。"""
    try:
        with open(path, "rb") as handle:
            return _decode_text_bytes(handle.read())
    except OSError:
        return ""


def _write_bytes_atomic(path: str, data: bytes) -> None:
    """原子写入字节（同目录临时文件 + os.replace）。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


# ============================================================================
# BMS 头部兜底解析（SPEC §1.4）
# ============================================================================


def _parse_bms_header(text: str) -> dict:
    """解析 BMS 文本里的头部命令（#BPM/#GENRE/#PLAYLEVEL/#TITLE/#ARTIST）。"""
    info = {
        "bpm": None,
        "genre": None,
        "title": None,
        "artist": None,
        "playlevel": None,
        "designer": None,
    }
    if not isinstance(text, str):
        return info
    for line in text.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if not stripped.startswith("#"):
            continue
        body = stripped[1:]
        match = re.match(r"([A-Za-z0-9_]+)\s*(.*)$", body)
        if not match:
            continue
        command = match.group(1).upper()
        rest = match.group(2).strip()
        if command == "BPM" and info["bpm"] is None:
            info["bpm"] = _parse_positive_number(rest)
        elif command == "GENRE" and info["genre"] is None:
            info["genre"] = rest
        elif command == "TITLE" and info["title"] is None:
            info["title"] = rest
        elif command == "ARTIST" and info["artist"] is None:
            info["artist"] = rest
        elif command == "PLAYLEVEL" and info["playlevel"] is None:
            info["playlevel"] = rest
        elif command in ("LEVELDESIGN", "MAKER") and info["designer"] is None:
            info["designer"] = rest
    return info


def _find_entry(entries, wanted_lower: str) -> Optional[str]:
    """在条目列表里按 basename 找条目（忽略大小写）。"""
    for name in entries:
        if _basename_lower(name) == wanted_lower:
            return name
    return None


def _primary_bms_info(bms_texts: dict) -> dict:
    """取编号最小的一份 BMS（通常 map1/map2）的头部信息用于兜底。"""
    if not isinstance(bms_texts, dict):
        return _parse_bms_header("")
    chosen = None
    for slot in MAP_SLOTS:
        for name, text in bms_texts.items():
            if _basename_lower(name) == "map%d.bms" % slot:
                chosen = (name, text)
                break
        if chosen:
            break
    if chosen is None:
        for name, text in bms_texts.items():
            chosen = (name, text)
            break
    return _parse_bms_header(chosen[1] if chosen else "")


def _bms_info_for_slot(bms_texts: dict, slot: int) -> dict:
    """取某个难度槽位的 BMS 头部信息。"""
    if isinstance(bms_texts, dict):
        for name, text in bms_texts.items():
            if _basename_lower(name) == "map%d.bms" % slot:
                return _parse_bms_header(text)
    return _parse_bms_header("")


# ============================================================================
# 统一中间模型 manifest（SPEC §2.1）
# ============================================================================


def _is_euterpe_style(raw_info: dict) -> bool:
    """判断 info 是否是 Euterpe manifest 风格（含 schema/meta/files）。"""
    if not isinstance(raw_info, dict):
        return False
    if isinstance(raw_info.get("meta"), dict):
        return True
    if isinstance(raw_info.get("files"), dict):
        return True
    return "schema" in raw_info and "name" not in raw_info


def to_manifest(raw_info: dict, bms_texts: dict, file_sizes: dict, fallback_name: str = "") -> dict:
    """把 CAM info / Euterpe manifest / BMS 兜底信息统一成 Euterpe manifest 结构。"""
    raw = raw_info if isinstance(raw_info, dict) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    primary_bms = _primary_bms_info(bms_texts)

    # ---- 名称 / 作者 ----
    name = _pick_text(meta.get("name"), raw.get("name"), fallback_name, default="Untitled")
    author = _pick_text(meta.get("author"), raw.get("author"), default="Unknown")

    # ---- bpm：meta.bpm / 顶层 bpm -> BMS #BPM -> 120 ----
    bpm = None
    for candidate in (meta.get("bpm"), raw.get("bpm")):
        bpm = _parse_positive_number(candidate)
        if bpm is not None:
            break
    if bpm is None:
        bpm = _parse_positive_number(primary_bms.get("bpm"))
    if bpm is None:
        bpm = 120

    # ---- scene：meta.scene / 顶层 scene -> BMS #GENRE -> scene_01 ----
    scene = ""
    for candidate in (meta.get("scene"), raw.get("scene")):
        text = _text(candidate).strip()
        if SCENE_PATTERN.match(text):
            scene = text
            break
    if not scene:
        genre = _text(primary_bms.get("genre")).strip()
        if SCENE_PATTERN.match(genre):
            scene = genre
    if not scene:
        scene = "scene_01"

    # ---- 文件信息（顺序即包内文件顺序）----
    file_names = list(file_sizes.keys()) if isinstance(file_sizes, dict) else []
    sizes = {}
    for rel_name in file_names:
        try:
            size_value = int(file_sizes[rel_name])
        except (TypeError, ValueError):
            continue
        sizes[_norm_pkg_name(rel_name)] = size_value

    def find_package_file(wanted_lower: str) -> Optional[str]:
        for rel_name in sizes:
            if _basename_lower(rel_name) == wanted_lower:
                return rel_name
        return None

    # ---- maps：只保留包内确实有 mapN.bms 的槽位 ----
    maps = {}
    source_maps = meta.get("maps") if isinstance(meta.get("maps"), dict) else {}
    for slot in MAP_SLOTS:
        if find_package_file("map%d.bms" % slot) is None:
            continue
        slot_meta = source_maps.get("map%d" % slot)
        if not isinstance(slot_meta, dict):
            slot_meta = {}
        rating = ""
        for candidate in (slot_meta.get("rating"), raw.get("difficulty%d" % slot)):
            text = _rating_text(candidate)
            if text:
                rating = text
                break
        if not rating:
            header = _bms_info_for_slot(bms_texts, slot)
            rating = _text(header.get("playlevel")).strip()
        if not rating:
            rating = "1"

        charters = []
        raw_charters = slot_meta.get("charters")
        if isinstance(raw_charters, list):
            for value in raw_charters:
                text = _text(value).strip()
                if text:
                    charters.append(text)
        if not charters:
            designer = (
                _text(raw.get("levelDesigner%d" % slot)).strip()
                or _text(raw.get("levelDesigner")).strip()
                or _text(raw.get("charter")).strip()
                or "Unknown"
            )
            charters = [designer]
        maps["map%d" % slot] = {
            "rating": rating,
            "charters": charters,
            "predicted_rating": None,
        }

    # ---- 隐藏设置 ----
    mode_text = _text(meta.get("hide_mode")).strip()
    if not mode_text:
        mode_text = _text(raw.get("hideBmsMode")).strip()
    mode_upper = mode_text.upper()
    hide_mode = mode_upper if mode_upper in HIDE_MODE_VALUES else "CLICK"

    hide_rating_override = _pick_text(
        raw.get("hideBmsDifficulty"),
        meta.get("hide_rating_override"),
        default="0",
    )
    hide_message = _pick_text(
        raw.get("hideBmsMessage"),
        meta.get("hide_message"),
        default="",
    )

    keywords_raw = raw.get("searchTags")
    if not isinstance(keywords_raw, list):
        keywords_raw = meta.get("search_keywords")
    if isinstance(keywords_raw, list):
        search_keywords = [str(item) for item in keywords_raw if isinstance(item, str) and item.strip()]
    else:
        search_keywords = []

    # ---- 其余 meta 字段：保留原值（类型合法时），否则补默认值 ----
    description = meta.get("description")
    if not isinstance(description, str):
        description = None
    safe_for_streamer = meta.get("safe_for_streamer")
    if not isinstance(safe_for_streamer, bool):
        safe_for_streamer = False
    name_romanized = _pick_text(meta.get("name_romanized"), default="")
    bpm_min = meta.get("bpm_min") if isinstance(meta.get("bpm_min"), (int, float)) and not isinstance(meta.get("bpm_min"), bool) else None
    bpm_max = meta.get("bpm_max") if isinstance(meta.get("bpm_max"), (int, float)) and not isinstance(meta.get("bpm_max"), bool) else None
    background_video_opacity = meta.get("background_video_opacity")
    if not isinstance(background_video_opacity, (int, float)) or isinstance(background_video_opacity, bool):
        background_video_opacity = None
    cover_dominant_color = meta.get("cover_dominant_color")
    if not isinstance(cover_dominant_color, str):
        cover_dominant_color = None
    uploader = meta.get("uploader") if isinstance(meta.get("uploader"), dict) else None
    created_at = meta.get("created_at") if isinstance(meta.get("created_at"), (int, float)) and not isinstance(meta.get("created_at"), bool) else None
    updated_at = meta.get("updated_at") if isinstance(meta.get("updated_at"), (int, float)) and not isinstance(meta.get("updated_at"), bool) else None

    # ---- files：只写包内实际存在的文件 ----
    files = {}
    for rel_name, size_value in sizes.items():
        base = _basename_lower(rel_name)
        if base in IGNORED_MANIFEST_FILES:
            continue
        if base.startswith("info.json.bak"):
            continue
        if base.startswith("."):
            continue
        files[rel_name] = {"version": 1, "size": size_value}

    return {
        "schema": 1,
        "cid": None,
        "meta": {
            "name": name,
            "name_romanized": name_romanized,
            "author": author,
            "description": description,
            "safe_for_streamer": safe_for_streamer,
            "bpm": bpm,
            "bpm_min": bpm_min,
            "bpm_max": bpm_max,
            "scene": scene,
            "scene_egg": None,
            "background_video_opacity": background_video_opacity,
            "search_keywords": search_keywords,
            "maps": maps,
            "hide_mode": hide_mode,
            "hide_rating_override": hide_rating_override,
            "hide_message": hide_message,
            "cover_dominant_color": cover_dominant_color,
            "uploader": uploader,
            "created_at": created_at,
            "updated_at": updated_at,
        },
        "files": files,
    }


def _apply_euterpe_migration_quirks(manifest: dict) -> dict:
    """对齐 Euterpe App 实测的 CAM -> 离线迁移细节（就地修改并返回）。

    实测样本（good/reference_offline_lv9.json）：当 CAM 的 hideBmsDifficulty 为 "0"
    而某个难度的值为 "?"（非数值）时，App 迁移后 manifest 的 hide_rating_override
    会变成 "?"。这里按该实测结果修正，保证生成的 manifest.epk 与官方迁移一致。
    """
    if not isinstance(manifest, dict):
        return manifest
    meta = manifest.get("meta")
    if not isinstance(meta, dict):
        return manifest
    override = _text(meta.get("hide_rating_override")).strip()
    if override not in ("", "0", "0.0"):
        return manifest
    maps = meta.get("maps")
    if not isinstance(maps, dict):
        return manifest
    for slot in MAP_SLOTS:
        slot_meta = maps.get("map%d" % slot)
        if not isinstance(slot_meta, dict):
            continue
        rating = _rating_text(slot_meta.get("rating"))
        if rating and rating not in ("0", "0.0") and _parse_positive_number(rating) is None:
            meta["hide_rating_override"] = rating
            break
    return manifest


# ============================================================================
# CAM info.json 生成 / 校验 / 序列化（SPEC §2.2）
# ============================================================================


def build_cam_info(manifest: dict) -> dict:
    """把统一 manifest 转成合法的 CustomAlbums info.json（字段顺序固定）。"""
    manifest = manifest if isinstance(manifest, dict) else {}
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    maps = meta.get("maps") if isinstance(meta.get("maps"), dict) else {}

    def get_map(slot: int) -> dict:
        value = maps.get("map%d" % slot)
        return value if isinstance(value, dict) else {}

    # levelDesigner 取第一个存在难度槽位的第一位谱师
    level_designer = ""
    for slot in MAP_SLOTS:
        slot_meta = get_map(slot)
        charters = slot_meta.get("charters")
        if isinstance(charters, list):
            for value in charters:
                text = _text(value).strip()
                if text:
                    level_designer = text
                    break
        if level_designer:
            break
    if not level_designer:
        level_designer = "Unknown"

    name = _pick_text(meta.get("name"), default="Untitled")
    author = _pick_text(meta.get("author"), default="Unknown")
    scene = _text(meta.get("scene")).strip()
    if not SCENE_PATTERN.match(scene):
        scene = "scene_01"

    info = {}
    info["name"] = name
    info["author"] = author
    info["bpm"] = _format_bpm(meta.get("bpm"))
    info["scene"] = scene
    info["sceneEgg"] = "none"
    info["levelDesigner"] = level_designer

    for slot in MAP_SLOTS:
        slot_meta = get_map(slot)
        designer = level_designer
        charters = slot_meta.get("charters")
        if isinstance(charters, list):
            for value in charters:
                text = _text(value).strip()
                if text:
                    designer = text
                    break
        info["levelDesigner%d" % slot] = designer

    for slot in MAP_SLOTS:
        slot_meta = get_map(slot)
        rating = ""
        if slot_meta:
            rating = _rating_text(slot_meta.get("rating"))
        info["difficulty%d" % slot] = rating if rating else "0"

    mode = _text(meta.get("hide_mode")).strip().upper()
    info["hideBmsMode"] = mode if mode in HIDE_MODE_VALUES else "CLICK"
    info["hideBmsDifficulty"] = _pick_text(meta.get("hide_rating_override"), default="0")
    info["hideBmsMessage"] = _text(meta.get("hide_message"))
    info["unlockLevel"] = "0"
    keywords = meta.get("search_keywords")
    info["searchTags"] = [str(item) for item in keywords if isinstance(item, str)] if isinstance(keywords, list) else []
    info["battleUi"] = "default"
    return info


def _cam_errors_structure(info: dict, errors: list, warnings: list) -> None:
    """校验 CAM info 的字段存在性与类型（供 validate_cam_info 使用）。"""
    for key in CAM_TOP_KEYS:
        if key not in info:
            errors.append("缺少字段 %s" % key)
    string_keys = [key for key in CAM_TOP_KEYS if key != "searchTags"]
    for key in string_keys:
        if key in info and not isinstance(info.get(key), str):
            errors.append("字段 %s 应为字符串（当前为 %s）" % (key, type(info.get(key)).__name__))
    # 名称不能为空
    name = info.get("name")
    if isinstance(name, str) and not name.strip():
        errors.append("name 不能为空")
    # bpm 必须是正数
    if "bpm" in info:
        bpm_text = info.get("bpm")
        if isinstance(bpm_text, str):
            if _parse_positive_number(bpm_text) is None:
                errors.append("bpm 必须为正数（当前：%r）" % bpm_text)
        elif isinstance(bpm_text, (int, float)) and not isinstance(bpm_text, bool):
            errors.append("bpm 应为字符串（当前为数字）")
    # scene 必须合法
    scene = info.get("scene")
    if isinstance(scene, str) and not SCENE_PATTERN.match(scene.strip()):
        errors.append("scene 非法（必须是 scene_01..scene_12，当前：%r）" % scene)
    # 难度值
    for slot in MAP_SLOTS:
        key = "difficulty%d" % slot
        value = info.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text in ("", "0"):
            continue
        if text == "?":
            continue
        if _parse_positive_number(text) is None:
            warnings.append("%s 的值不是数字：%r" % (key, value))
    # 谱师字段
    for key in ("author", "levelDesigner") + tuple("levelDesigner%d" % slot for slot in MAP_SLOTS):
        value = info.get(key)
        if isinstance(value, str) and not value.strip():
            warnings.append("%s 为空" % key)
    # 隐藏模式
    mode = info.get("hideBmsMode")
    if isinstance(mode, str) and mode.strip().upper() not in HIDE_MODE_VALUES:
        warnings.append("hideBmsMode 不是 CLICK/PRESS/TOGGLE 之一：%r" % mode)
    # 搜索标签
    tags = info.get("searchTags")
    if "searchTags" in info:
        if not isinstance(tags, list):
            errors.append("searchTags 应为数组")
        else:
            for tag in tags:
                if not isinstance(tag, str):
                    warnings.append("searchTags 内存在非字符串元素")
                    break


def validate_cam_info(info: dict) -> tuple:
    """校验一份 CAM info.json；返回 (errors, warnings)。"""
    errors: list = []
    warnings: list = []
    try:
        if not isinstance(info, dict):
            return (["CAM info 必须是 JSON 对象"], [])
        _cam_errors_structure(info, errors, warnings)
    except Exception as exc:  # 校验自身绝不抛异常给 UI
        errors.append("校验失败：%s" % exc)
    return (errors, warnings)


def _serialize_cam_info(info: dict) -> bytes:
    """序列化 CAM info.json：UTF-8 无 BOM、indent=2、ensure_ascii=False、CRLF。"""
    text = json.dumps(info, ensure_ascii=False, indent=2)
    text = text.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n"
    return text.encode("utf-8")


# ============================================================================
# 扫描 / 损坏判定（SPEC §4 + R2 §8）
# ============================================================================


def _read_zip_package(path: str):
    """读取 .mdm(zip)：返回 (entries, sizes, bms_texts, info_entry, info_bytes, error)。"""
    entries: list = []
    sizes: dict = {}
    bms_texts: dict = {}
    info_entry = None
    info_bytes = None
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            for archive_info in archive.infolist():
                if archive_info.is_dir():
                    continue
                rel_name = _norm_pkg_name(archive_info.filename)
                if not rel_name:
                    continue
                entries.append(rel_name)
                sizes[rel_name] = archive_info.file_size
            for rel_name in entries:
                if _basename_lower(rel_name).endswith(".bms"):
                    try:
                        bms_texts[rel_name] = _decode_text_bytes(archive.read(rel_name))
                    except (OSError, zipfile.BadZipFile, RuntimeError):
                        bms_texts[rel_name] = ""
            info_entry = _find_entry(entries, "info.json")
            if info_entry is not None:
                info_bytes = archive.read(info_entry)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return ([], {}, {}, None, None, "无法读取 .mdm 压缩包：%s" % exc)
    return (entries, sizes, bms_texts, info_entry, info_bytes, None)


def _read_folder_package(path: str):
    """读取 CAM 文件夹：返回 (entries, sizes, bms_texts, info_entry, info_bytes, error)。"""
    entries: list = []
    sizes: dict = {}
    bms_texts: dict = {}
    info_entry = None
    info_bytes = None
    try:
        for root, dirs, files in os.walk(path):
            dirs.sort(key=str.casefold)
            for file_name in sorted(files, key=str.casefold):
                full_path = os.path.join(root, file_name)
                rel_name = os.path.relpath(full_path, path).replace("\\", "/")
                entries.append(rel_name)
                try:
                    sizes[rel_name] = os.path.getsize(full_path)
                except OSError:
                    sizes[rel_name] = 0
                if file_name.casefold().endswith(".bms"):
                    bms_texts[rel_name] = _read_text_file(full_path)
        info_entry = _find_entry(entries, "info.json")
        if info_entry is not None:
            with open(os.path.join(path, *info_entry.split("/")), "rb") as handle:
                info_bytes = handle.read()
    except OSError as exc:
        return ([], {}, {}, None, None, "无法读取文件夹：%s" % exc)
    return (entries, sizes, bms_texts, info_entry, info_bytes, None)


def _compare_manifest_files(raw_info: dict, sizes: dict) -> Optional[str]:
    """比对 info.json 里的 files 声明与实际包内文件；不一致时返回警告文案。"""
    if not isinstance(raw_info, dict):
        return None
    declared = raw_info.get("files")
    if not isinstance(declared, dict):
        return None
    actual = {}
    for rel_name, size_value in sizes.items():
        base = _basename_lower(rel_name)
        if base in IGNORED_MANIFEST_FILES:
            continue
        actual[base] = int(size_value)
    declared_sizes = {}
    for name, payload in declared.items():
        if not isinstance(payload, dict):
            continue
        base = _basename_lower(name)
        if base in IGNORED_MANIFEST_FILES:
            continue
        try:
            declared_sizes[base] = int(payload.get("size", -1))
        except (TypeError, ValueError):
            declared_sizes[base] = -1
    missing = sorted(set(declared_sizes) - set(actual))
    extra = sorted(set(actual) - set(declared_sizes))
    mismatch = sorted(
        name for name in set(declared_sizes) & set(actual) if declared_sizes[name] != actual[name]
    )
    if not (missing or extra or mismatch):
        return None
    parts = []
    if missing:
        parts.append("包内缺少 " + "、".join(missing))
    if extra:
        parts.append("多出 " + "、".join(extra))
    if mismatch:
        parts.append("大小不符 " + "、".join(mismatch))
    return "manifest.files 与包内文件不一致：" + "；".join(parts)


def _finish_item(
    item: ScanItem,
    raw_info,
    entries: list,
    sizes: dict,
    bms_texts: dict,
    info_present: bool,
    info_bytes,
    fallback_name: str,
    errors: list,
    warnings: list,
) -> ScanItem:
    """补齐 ScanItem 的 manifest / 校验 / payload（scan 与重扫共用）。"""
    raw = raw_info if isinstance(raw_info, dict) else {}
    bom = bool(info_bytes) and bytes(info_bytes).startswith(UTF8_BOM)

    manifest = to_manifest(raw, bms_texts, sizes, fallback_name)
    cam_info = build_cam_info(manifest)
    item.chart_name = manifest["meta"]["name"]
    item.author = manifest["meta"]["author"]

    if info_present and isinstance(raw_info, dict):
        # Euterpe manifest 结构
        if _is_euterpe_style(raw_info):
            if not any("Euterpe" in message for message in errors):
                errors.append(
                    "info.json 是 Euterpe manifest 结构（含 schema/meta/files），CustomAlbums 无法识别"
                )
        # 缺少 CAM 顶层字段
        missing = [key for key in CAM_TOP_KEYS if key not in raw_info]
        if missing:
            errors.append("缺少 CAM 顶层字段：" + "、".join(missing))
        # bpm 非正
        if "bpm" in raw_info:
            if _parse_positive_number(raw_info.get("bpm")) is None:
                errors.append("bpm 非正或非法：%r" % (raw_info.get("bpm"),))
        # scene 非法
        if "scene" in raw_info:
            scene_value = raw_info.get("scene")
            if not isinstance(scene_value, str) or not SCENE_PATTERN.match(scene_value.strip()):
                errors.append("scene 非法：%r" % (scene_value,))
        # 难度槽与包内 mapN.bms 是否一致
        for slot in MAP_SLOTS:
            has_bms = _find_entry(list(sizes.keys()), "map%d.bms" % slot) is not None
            value = raw_info.get("difficulty%d" % slot)
            if isinstance(value, bool):
                value = None
            if has_bms:
                if isinstance(value, (int, float)):
                    errors.append("difficulty%d 应为字符串（当前为数字）" % slot)
                elif _rating_text(value) in ("", "0", "0.0"):
                    errors.append("map%d.bms 存在但 difficulty%d 为空或 0" % (slot, slot))
            else:
                if _rating_text(value) not in ("", "0", "0.0"):
                    errors.append("difficulty%d 非 0 但包内没有 map%d.bms" % (slot, slot))
        # 谱师字段应为字符串
        for key in ("levelDesigner",) + tuple("levelDesigner%d" % slot for slot in MAP_SLOTS):
            if key in raw_info and not isinstance(raw_info.get(key), str):
                errors.append("%s 应为字符串" % key)
        # manifest.files 与包内文件的一致性（warning）
        mismatch = _compare_manifest_files(raw_info, sizes)
        if mismatch:
            warnings.append(mismatch)

    if info_present and info_bytes and bytes(info_bytes).startswith(UTF8_BOM):
        warnings.append("info.json 带 UTF-8 BOM（Euterpe 解析严格，可能无法识别）")

    # 音乐缺失（warning）
    if _find_entry(list(sizes.keys()), "music.ogg") is None:
        warnings.append("缺少 music.ogg（没有音乐的谱面可能无法载入）")

    # 方案1 对 map4/map5 的已知限制（warning）
    if _find_entry(list(sizes.keys()), "map4.bms") is not None or _find_entry(
        list(sizes.keys()), "map5.bms"
    ) is not None:
        warnings.append(MAP45_WARNING)

    # 去重后写入 item
    item.errors = []
    for message in errors:
        if message not in item.errors:
            item.errors.append(message)
    item.warnings = []
    for message in warnings:
        if message not in item.errors and message not in item.warnings:
            item.warnings.append(message)
    item.issues = list(item.errors) + list(item.warnings)
    item.needs_fix = bool(item.errors)

    item.payload = {
        "kind": item.kind,
        "source": os.path.abspath(item.path),
        "fallback_name": fallback_name,
        "info_present": bool(info_present),
        "info_entry": _find_entry(entries, "info.json") if info_present else None,
        "info_bytes": bytes(info_bytes) if info_bytes else None,
        "bom": bom,
        "entries": list(entries),
        "sizes": dict(sizes),
        "bms_texts": dict(bms_texts),
        "raw_info": raw_info if isinstance(raw_info, dict) else None,
        "manifest": manifest,
        "cam_info": cam_info,
    }
    return item


def _scan_mdm(path: str, progress=None) -> ScanItem:
    """扫描单个 .mdm 文件。"""
    item = ScanItem(path=os.path.abspath(path), kind="mdm")
    item.chart_name = os.path.splitext(os.path.basename(path))[0]
    _notify(progress, "扫描：%s" % os.path.basename(path))
    errors: list = []
    warnings: list = []

    entries, sizes, bms_texts, info_entry, info_bytes, error = _read_zip_package(path)
    if error:
        errors.append(error)
        return _finish_item(
            item, None, entries, sizes, bms_texts, False, None,
            item.chart_name, errors, warnings,
        )

    raw_info = None
    if info_entry is None:
        errors.append("缺少 info.json（可用 BMS 头补建）")
    else:
        try:
            raw_info = json.loads(bytes(info_bytes).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append("info.json JSON 解析失败：%s" % exc)
            raw_info = None
        if raw_info is not None and not isinstance(raw_info, dict):
            errors.append("info.json 不是 JSON 对象")
            raw_info = None

    return _finish_item(
        item, raw_info, entries, sizes, bms_texts,
        info_entry is not None, info_bytes, item.chart_name, errors, warnings,
    )


def _scan_folder(path: str, progress=None) -> ScanItem:
    """扫描单个 CAM 文件夹。"""
    item = ScanItem(path=os.path.abspath(path), kind="folder")
    item.chart_name = os.path.basename(os.path.normpath(path))
    _notify(progress, "扫描：%s" % item.chart_name)
    errors: list = []
    warnings: list = []

    entries, sizes, bms_texts, info_entry, info_bytes, error = _read_folder_package(path)
    if error:
        errors.append(error)
        return _finish_item(
            item, None, entries, sizes, bms_texts, False, None,
            item.chart_name, errors, warnings,
        )

    raw_info = None
    if info_entry is None:
        errors.append("缺少 info.json（可用 BMS 头补建）")
    else:
        try:
            raw_info = json.loads(bytes(info_bytes).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append("info.json JSON 解析失败：%s" % exc)
            raw_info = None
        if raw_info is not None and not isinstance(raw_info, dict):
            errors.append("info.json 不是 JSON 对象")
            raw_info = None

    return _finish_item(
        item, raw_info, entries, sizes, bms_texts,
        info_entry is not None, info_bytes, item.chart_name, errors, warnings,
    )


def _rebuild_payload(item: ScanItem) -> Optional[dict]:
    """当 ScanItem 没有 payload 时，重新扫描源文件补齐。"""
    try:
        if not item or not isinstance(item.path, str):
            return None
        if item.kind == "folder" and os.path.isdir(item.path):
            return _scan_folder(item.path).payload
        if os.path.isfile(item.path):
            return _scan_mdm(item.path).payload
    except Exception:
        return None
    return None


def scan(paths, recursive: bool = True, progress: Optional[Callable[[str], None]] = None) -> list:
    """扫描一批路径，返回所有 .mdm 与 CAM 文件夹候选项（不会抛异常）。"""
    results: list = []
    seen = set()

    def add(item: ScanItem) -> None:
        key = os.path.normcase(os.path.abspath(item.path))
        if key in seen:
            return
        seen.add(key)
        results.append(item)
        _notify(progress, "发现 %s：%s" % ("压缩包" if item.kind == "mdm" else "文件夹", item.chart_name))

    def walk_directory(directory: str) -> None:
        info_path = _dir_has_info(directory)
        if info_path is not None:
            try:
                add(_scan_folder(directory, progress))
            except Exception as exc:  # 单个异常不影响整体扫描
                broken = ScanItem(path=os.path.abspath(directory), kind="folder")
                broken.errors = ["扫描失败：%s" % exc]
                broken.issues = list(broken.errors)
                broken.needs_fix = True
                add(broken)
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk_directory(entry.path)
                elif entry.is_file() and entry.name.casefold().endswith(".mdm"):
                    add(_scan_mdm(entry.path, progress))
            except OSError:
                continue

    try:
        path_list = [paths] if isinstance(paths, (str, os.PathLike)) else list(paths or [])
    except TypeError:
        path_list = []

    for raw_path in path_list:
        try:
            path = os.path.abspath(os.fspath(raw_path))
        except (TypeError, ValueError):
            continue
        try:
            if os.path.isfile(path):
                if path.casefold().endswith(".mdm"):
                    add(_scan_mdm(path, progress))
                continue
            if not os.path.isdir(path):
                _notify(progress, "跳过不存在的路径：%s" % path)
                continue
            if not recursive:
                info_path = _dir_has_info(path)
                if info_path is not None:
                    add(_scan_folder(path, progress))
                for entry in sorted(os.scandir(path), key=lambda item: item.name.casefold()):
                    if entry.is_file() and entry.name.casefold().endswith(".mdm"):
                        add(_scan_mdm(entry.path, progress))
                continue
            walk_directory(path)
        except Exception as exc:  # 单路径异常不影响其它路径
            _notify(progress, "扫描路径失败：%s（%s）" % (path, exc))

    results.sort(key=lambda item: os.path.normcase(item.path))
    _notify(progress, "扫描完成：共 %d 项候选，其中 %d 项需要修复" % (
        len(results), sum(1 for item in results if item.needs_fix)))
    return results


def _dir_has_info(directory: str) -> Optional[str]:
    """返回目录下 info.json 的实际路径；没有返回 None。"""
    try:
        for name in os.listdir(directory):
            if name.casefold() == "info.json" and os.path.isfile(os.path.join(directory, name)):
                return os.path.join(directory, name)
    except OSError:
        return None
    return None


# ============================================================================
# 路径检测（SPEC §5）
# ============================================================================


def _candidate_game_dirs():
    """生成所有可能的 Muse Dash 游戏目录候选（Steam 库 + 常见路径）。"""
    candidates: list = []

    def add(path_value) -> None:
        if not path_value:
            return
        text = str(path_value).replace("\\", "/").strip().strip('"')
        if text:
            candidates.append(text)

    # 1) 环境变量
    for env_name in ("MUSE_DASH_DIR", "MUSEDASH_DIR", "STEAM_PATH", "SteamPath"):
        add(os.environ.get(env_name))

    # 2) Windows 注册表里的 Steam 安装目录（非 Windows / 无注册表时自动跳过）
    try:
        import winreg  # type: ignore

        registry_keys = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        )
        for hive, sub_key, value_name in registry_keys:
            try:
                with winreg.OpenKey(hive, sub_key) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                add(value)
            except OSError:
                continue
    except Exception:
        pass

    # 3) 常见盘符下的 Steam 根目录
    import string

    for letter in string.ascii_uppercase:
        drive = "%s:" % letter
        add(os.path.join(drive, "Steam"))
        add(os.path.join(drive, "steam"))
        add(os.path.join(drive, "SteamLibrary"))
        add(os.path.join(drive, "Games", "Steam"))
        add(os.path.join(drive, "Program Files (x86)", "Steam"))
        add(os.path.join(drive, "Program Files", "Steam"))
        add(os.path.join(drive, "Steam", "steamapps", "common", "Muse Dash"))
        add(os.path.join(drive, "Muse Dash"))
        add(os.path.join(drive, "Games", "Muse Dash"))

    # 4) Steam 库目录（libraryfolders.vdf）
    library_roots = []
    for steam_root in candidates:
        vdf_path = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        if not os.path.isfile(vdf_path):
            continue
        try:
            content = _read_text_file(vdf_path)
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
            library_roots.append(match.group(1).replace("\\\\", "\\"))
        for match in re.finditer(r'^\s*"\d+"\s+"([^"]+)"', content, re.MULTILINE):
            library_roots.append(match.group(1).replace("\\\\", "\\"))

    for library in library_roots:
        add(os.path.join(library, "steamapps", "common", "Muse Dash"))
        add(os.path.join(library, "steamapps", "common", "MuseDash"))
        add(os.path.join(library, "steamapps", "common", "Muse Dash_Data"))
    for steam_root in list(candidates):
        add(os.path.join(steam_root, "steamapps", "common", "Muse Dash"))
        add(os.path.join(steam_root, "steamapps", "common", "MuseDash"))

    # 去重（保持顺序）
    seen = set()
    unique = []
    for path in candidates:
        key = os.path.normcase(os.path.abspath(path)) if path else ""
        if key and key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _is_game_dir(path: str) -> bool:
    """判断一个目录是不是 Muse Dash 游戏安装目录。"""
    if not path or not os.path.isdir(path):
        return False
    if find_custom_albums(path) is not None:
        return True
    if os.path.isdir(os.path.join(path, "Euterpe_Charts")):
        return True
    try:
        for name in os.listdir(path):
            if name.casefold() in ("musedash.exe", "musedash_data", "muse dash_Data".casefold()):
                return True
    except OSError:
        return False
    return False


def detect_game_dir() -> Optional[str]:
    """从 Steam 库 + 常见路径自动寻找 Muse Dash 游戏目录；找不到返回 None。"""
    try:
        for candidate in _candidate_game_dirs():
            try:
                if _is_game_dir(candidate):
                    return os.path.abspath(candidate)
            except OSError:
                continue
    except Exception:
        return None
    return None


def find_custom_albums(game_dir: str) -> Optional[str]:
    """在游戏目录里找 Custom_Albums（兼容多种大小写拼写）；找不到返回 None。"""
    if not game_dir or not os.path.isdir(game_dir):
        return None
    try:
        names = os.listdir(game_dir)
    except OSError:
        return None
    mapping = {}
    for name in names:
        mapping.setdefault(name.casefold(), name)
    for wanted in CAM_DIR_NAMES:
        actual = mapping.get(wanted)
        if actual is None:
            continue
        full_path = os.path.join(game_dir, actual)
        if os.path.isdir(full_path):
            return full_path
    return None


def default_offline_dir(game_dir: str) -> str:
    """返回 <游戏目录>/Euterpe_Charts/Offline。"""
    return os.path.join(str(game_dir or ""), "Euterpe_Charts", "Offline")


# ============================================================================
# 方案1（就地修 info.json）/ 方案1b（解压成 CAM 文件夹）/ 方案2（Euterpe）
# ============================================================================


def _infer_game_root(*paths) -> Optional[str]:
    """从给定路径向上寻找 Custom_Albums 的父目录，作为游戏根目录。"""
    for raw in paths:
        if not raw:
            continue
        try:
            current = os.path.abspath(os.fspath(raw))
        except (TypeError, ValueError):
            continue
        if not os.path.isdir(current):
            if os.path.isfile(current):
                current = os.path.dirname(current)
            else:
                continue
        while True:
            if os.path.basename(current).casefold() in CAM_DIR_NAMES:
                return os.path.dirname(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def _backup_root(source: str, game_dir: Optional[str], custom_albums_dir: Optional[str]) -> str:
    """确定备份根目录：必须在 Custom_Albums 之外（避免被 EU 当成谱面扫描）。"""
    candidates = []
    if game_dir:
        candidates.append(game_dir)
    if custom_albums_dir:
        candidates.append(os.path.dirname(os.path.abspath(custom_albums_dir)))
    base = os.path.abspath(source)
    if os.path.isfile(base):
        base = os.path.dirname(base)
    walk = base
    while True:
        if os.path.basename(walk).casefold() in CAM_DIR_NAMES:
            candidates.append(os.path.dirname(walk))
            break
        parent = os.path.dirname(walk)
        if parent == walk:
            break
        walk = parent
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    for candidate in candidates:
        if candidate:
            return os.path.abspath(candidate)
    # 兜底：源目录的上一级（若本身在 Custom_Albums 里则继续向上）
    fallback = base
    while os.path.basename(fallback).casefold() in CAM_DIR_NAMES:
        parent = os.path.dirname(fallback)
        if parent == fallback:
            break
        fallback = parent
    return fallback


def _backup_dir_for(item: ScanItem, payload: dict, game_dir, custom_albums_dir) -> str:
    """计算该谱面的备份目录 <根>/_ChartFixerBackup/<来源名>/。"""
    source = os.path.abspath(item.path)
    if item.kind == "mdm":
        source_name = os.path.splitext(os.path.basename(source))[0]
    else:
        source_name = os.path.basename(os.path.normpath(source))
    source_name = _safe_filename(source_name, default="chart")
    root = _backup_root(source, game_dir, custom_albums_dir)
    return os.path.join(root, "_ChartFixerBackup", source_name)


def _write_backup(backup_dir: str, item: ScanItem, source: str, info_bytes, note: str) -> Optional[str]:
    """把原始 info.json 备份到备份目录，并写入 record.json；返回备份文件路径。"""
    os.makedirs(backup_dir, exist_ok=True)
    backup_info_path = os.path.join(backup_dir, "info.json")
    written = False
    if info_bytes is not None:
        if not os.path.exists(backup_info_path):
            _write_bytes_atomic(backup_info_path, bytes(info_bytes))
            written = True
    record = {
        "source_path": os.path.abspath(source),
        "kind": item.kind,
        "chart_name": item.chart_name,
        "sha256": hashlib.sha256(bytes(info_bytes)).hexdigest() if info_bytes is not None else None,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "backup_file": backup_info_path if info_bytes is not None else None,
        "backup_written": written,
    }
    _write_bytes_atomic(
        os.path.join(backup_dir, "record.json"),
        json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return backup_info_path if info_bytes is not None else None


def _write_zip_entry(archive: zipfile.ZipFile, original: zipfile.ZipInfo, data: bytes) -> None:
    """按原条目的压缩方式/时间戳/属性写入新 zip。"""
    info = zipfile.ZipInfo(original.filename, original.date_time)
    info.compress_type = original.compress_type
    info.external_attr = original.external_attr
    info.internal_attr = getattr(original, "internal_attr", 0)
    info.create_system = getattr(original, "create_system", 0)
    info.comment = getattr(original, "comment", b"")
    try:
        info.extra = getattr(original, "extra", b"")
    except Exception:
        pass
    archive.writestr(info, data)


def _rewrite_mdm_info(source: str, info_bytes: bytes) -> None:
    """就地重写 .mdm：只替换 info.json 条目，其余条目字节/顺序/压缩方式全部保留。"""
    source = os.path.abspath(source)
    directory = os.path.dirname(source)
    fd, temp_path = tempfile.mkstemp(prefix=".chartfixer-", suffix=".tmp", dir=directory)
    os.close(fd)
    replaced = False
    try:
        with zipfile.ZipFile(source, "r") as archive_in:
            archive_comment = archive_in.comment
            with zipfile.ZipFile(temp_path, "w") as archive_out:
                archive_out.comment = archive_comment
                found_info = False
                for original in archive_in.infolist():
                    data = archive_in.read(original)
                    if not original.is_dir() and _basename_lower(original.filename) == "info.json":
                        _write_zip_entry(archive_out, original, info_bytes)
                        found_info = True
                    else:
                        archive_out.writestr(original, data)
                if not found_info:
                    now = time.localtime()[:6]
                    new_info = zipfile.ZipInfo("info.json", now)
                    new_info.compress_type = zipfile.ZIP_DEFLATED
                    new_info.external_attr = 0o644 << 16
                    archive_out.writestr(new_info, info_bytes)
        os.replace(temp_path, source)
        replaced = True
    finally:
        if not replaced:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _copy_package_file(source_kind: str, source: str, rel_name: str, target_path: str) -> None:
    """把包内单个文件复制到目标路径（mdm 从 zip 读，folder 从磁盘读）。"""
    if source_kind == "mdm":
        with zipfile.ZipFile(source, "r") as archive:
            with archive.open(rel_name) as reader, open(target_path, "wb") as writer:
                shutil.copyfileobj(reader, writer)
        return
    full_path = os.path.join(source, *rel_name.split("/"))
    if not os.path.isfile(full_path):
        return
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    shutil.copy2(full_path, target_path)


def _extract_package(item: ScanItem, payload: dict, target_root: str) -> None:
    """安全解压/复制整个包到 target_root（zip slip 防护 + 非法名清洗）。"""
    entries = list(payload.get("entries") or [])
    if item.kind == "mdm":
        with zipfile.ZipFile(item.path, "r") as archive:
            infos = {_norm_pkg_name(info.filename): info for info in archive.infolist()}
            for rel_name in entries:
                archive_info = infos.get(_norm_pkg_name(rel_name))
                if archive_info is None or _is_symlink_zipinfo(archive_info):
                    continue
                safe_rel = _safe_member_relpath(rel_name)
                if safe_rel is None:
                    continue
                target_path = os.path.join(target_root, *safe_rel.split("/"))
                if not _is_inside(target_root, target_path):
                    continue
                os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
                with archive.open(archive_info) as reader, open(target_path, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
        return
    for rel_name in entries:
        safe_rel = _safe_member_relpath(rel_name)
        if safe_rel is None:
            continue
        target_path = os.path.join(target_root, *safe_rel.split("/"))
        if not _is_inside(target_root, target_path):
            continue
        _copy_package_file("folder", item.path, rel_name, target_path)


def _unique_target(root: str, base_name: str) -> str:
    """在 root 下取一个不冲突的目标目录名（重名追加 " (2)"、" (3)"…）。"""
    target = os.path.join(root, base_name)
    index = 2
    while os.path.exists(target):
        target = os.path.join(root, "%s (%d)" % (base_name, index))
        index += 1
    return target


def _convert_cam_inplace(item: ScanItem, payload: dict, game_dir, custom_albums_dir, dry_run: bool) -> ActionResult:
    """方案1：就地修复 .mdm / 文件夹里的 info.json（默认方案）。"""
    source = os.path.abspath(item.path)
    cam_info = payload.get("cam_info") if isinstance(payload.get("cam_info"), dict) else {}
    canonical = _serialize_cam_info(cam_info)
    backup_dir = _backup_dir_for(item, payload, game_dir, custom_albums_dir)

    if not item.needs_fix:
        return ActionResult(item, False, source, "无需修复：info.json 已符合 CAM 规范")

    info_bytes = payload.get("info_bytes")
    if not payload.get("info_present"):
        note = "原包内没有 info.json，已用 BMS 头/包内容补建"
    else:
        note = "备份原 info.json"
    backup_info_path = os.path.join(backup_dir, "info.json")

    if item.kind == "mdm":
        if dry_run:
            action = "补建 info.json" if not payload.get("info_present") else "重写 info.json"
            return ActionResult(
                item,
                True,
                source,
                "预览：将%s（CAM 规范），原文件备份到 %s；不写入任何文件" % (action, backup_info_path),
            )
        if info_bytes is not None:
            _write_backup(backup_dir, item, source, info_bytes, note)
        else:
            _write_backup(backup_dir, item, source, None, note)
        _rewrite_mdm_info(source, canonical)
        tail = "；原 info.json 已备份到 %s" % backup_info_path if info_bytes is not None else "；已补建 info.json"
        return ActionResult(item, True, source, "已就地修复 .mdm 内的 info.json（CAM 规范）" + tail)

    if item.kind != "folder":
        return ActionResult(item, False, None, "不支持的谱面类型：%r" % item.kind)

    info_path = os.path.join(source, "info.json")
    if dry_run:
        action = "补建 info.json" if not payload.get("info_present") else "备份并重写 info.json"
        return ActionResult(
            item,
            True,
            source,
            "预览：将%s，原文件备份到 %s；不写入任何文件" % (action, backup_info_path),
        )
    _write_backup(backup_dir, item, source, info_bytes, note)
    _write_bytes_atomic(info_path, canonical)
    tail = "；原 info.json 已备份到 %s" % backup_info_path if info_bytes is not None else "；已补建 info.json"
    return ActionResult(item, True, source, "已修复文件夹内的 info.json（CAM 规范）" + tail)


def _convert_cam_folder(item: ScanItem, payload: dict, custom_albums_dir, dry_run: bool) -> ActionResult:
    """方案1b：把 .mdm 安全解压成 Custom_Albums/<谱面名>/ 文件夹。

    文件夹形态本身就是一个 CAM 文件夹，无需解压，直接按方案 1 修复其 info.json。
    """
    if item.kind != "mdm":
        result = _convert_cam_inplace(item, payload, None, custom_albums_dir, dry_run)
        if result.ok:
            result.message = "文件夹已是 CAM 形态，无需解压：" + result.message
        elif not item.needs_fix:
            result = ActionResult(
                item, True, os.path.abspath(item.path),
                "文件夹已是 CAM 形态且 info.json 合法，无需处理",
            )
        return result
    if not custom_albums_dir:
        return ActionResult(item, False, None, "未指定 Custom_Albums 目录，无法解压为 CAM 文件夹")
    fallback = payload.get("fallback_name") or os.path.splitext(os.path.basename(item.path))[0]
    base_name = _safe_filename(item.chart_name or fallback)
    target = os.path.join(custom_albums_dir, base_name)

    if os.path.exists(target):
        return ActionResult(item, False, target, "目标已存在，跳过（不覆盖）：%s" % target)

    cam_info = payload.get("cam_info") if isinstance(payload.get("cam_info"), dict) else {}
    if dry_run:
        return ActionResult(
            item, True, target,
            "预览：将安全解压到 %s 并写入合法 CAM info.json（不写入任何文件）" % target,
        )

    os.makedirs(custom_albums_dir, exist_ok=True)
    os.makedirs(target, exist_ok=False)
    try:
        _extract_package(item, payload, target)
        _write_bytes_atomic(os.path.join(target, "info.json"), _serialize_cam_info(cam_info))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    message = "已解压为 CAM 文件夹：%s" % target
    if payload.get("warnings"):
        message += "（%s）" % "；".join(str(value) for value in payload["warnings"])
    return ActionResult(item, True, target, message)


def _convert_euterpe(item: ScanItem, payload: dict, offline_dir, dry_run: bool) -> ActionResult:
    """方案2：转换为 Euterpe_Charts/Offline/<谱面名>/（含 manifest.epk）。"""
    if not offline_dir:
        return ActionResult(item, False, None, "未指定 Euterpe_Charts/Offline 目录，无法转换")
    fallback = payload.get("fallback_name") or os.path.splitext(os.path.basename(item.path))[0]
    base_name = _safe_filename(item.chart_name or fallback)
    target = _unique_target(offline_dir, base_name)

    if dry_run:
        return ActionResult(
            item, True, target,
            "预览：将生成 Euterpe 离线谱目录 %s（含 manifest.epk，不写入任何文件）" % target,
        )

    manifest = copy.deepcopy(payload.get("manifest") or {})
    if not isinstance(manifest, dict) or not manifest.get("meta"):
        return ActionResult(item, False, None, "缺少有效的 manifest 数据，无法转换")
    _apply_euterpe_migration_quirks(manifest)

    os.makedirs(target, exist_ok=False)
    try:
        copied_names = []
        for rel_name in payload.get("entries") or []:
            base = _basename_lower(rel_name)
            if base in IGNORED_MANIFEST_FILES:
                continue
            if base.startswith("info.json.bak") or base.startswith("."):
                continue
            safe_rel = _safe_member_relpath(rel_name)
            if safe_rel is None:
                continue
            target_path = os.path.join(target, *safe_rel.split("/"))
            if not _is_inside(target, target_path):
                continue
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
            _copy_package_file(item.kind, item.path, rel_name, target_path)
            copied_names.append(safe_rel)
        # manifest.files 以实际写出的文件为准（字节数与原包一致）
        actual_files = {}
        for name in copied_names:
            full_path = os.path.join(target, *name.split("/"))
            try:
                actual_files[name] = {"version": 1, "size": os.path.getsize(full_path)}
            except OSError:
                continue
        manifest["files"] = actual_files
        _write_bytes_atomic(os.path.join(target, "manifest.epk"), msgpack_encode(manifest))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise

    warnings = []
    if _find_entry(list((payload.get("sizes") or {}).keys()), "music.ogg") is None:
        warnings.append("警告：缺少 music.ogg，Euterpe 可能跳过该谱面")
    message = "已转换为 Euterpe 离线谱：%s" % target
    if warnings:
        message += "（%s）" % "；".join(warnings)
    return ActionResult(item, True, target, message)


def convert(
    items,
    plan,
    game_dir=None,
    custom_albums_dir=None,
    offline_dir=None,
    dry_run: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> list:
    """执行转换；plan ∈ {"cam","cam_folder","euterpe","both"}，绝不向 UI 抛异常。"""
    item_list = list(items or [])
    plan_key = str(plan or "").strip().lower()
    results: list = []

    if plan_key not in ("cam", "cam_folder", "euterpe", "both"):
        message = "未知方案：%r（可选 cam / cam_folder / euterpe / both）" % (plan,)
        for item in item_list:
            results.append(ActionResult(item, False, None, message))
        return results

    custom_albums = custom_albums_dir or (
        find_custom_albums(game_dir) if game_dir else None
    )
    offline = offline_dir or (default_offline_dir(game_dir) if game_dir else None)
    if plan_key in ("cam_folder", "euterpe"):
        if plan_key == "cam_folder" and not custom_albums:
            message = "找不到 Custom_Albums 目录，请先在 GUI 里指定游戏目录"
            return [ActionResult(item, False, None, message) for item in item_list]
        if plan_key == "euterpe" and not offline:
            message = "找不到 Euterpe_Charts/Offline 目录，请先在 GUI 里指定游戏目录"
            return [ActionResult(item, False, None, message) for item in item_list]

    total = len(item_list)
    for index, item in enumerate(item_list, 1):
        label = item.chart_name or os.path.basename(str(item.path))
        _notify(progress, "(%d/%d) 开始处理：%s" % (index, total, label))
        result = None
        try:
            payload = item.payload if isinstance(item.payload, dict) else _rebuild_payload(item)
            if not isinstance(payload, dict):
                result = ActionResult(item, False, None, "无法处理：缺少扫描数据，且无法重新读取源文件")
            elif plan_key == "cam":
                result = _convert_cam_inplace(item, payload, game_dir, custom_albums, dry_run)
            elif plan_key == "cam_folder":
                result = _convert_cam_folder(item, payload, custom_albums, dry_run)
            elif plan_key == "euterpe":
                result = _convert_euterpe(item, payload, offline, dry_run)
            else:  # both = cam + euterpe
                cam_result = _convert_cam_inplace(item, payload, game_dir, custom_albums, dry_run)
                euterpe_result = _convert_euterpe(item, payload, offline, dry_run)
                ok = cam_result.ok and euterpe_result.ok
                targets = [value for value in (cam_result.target, euterpe_result.target) if value]
                message = "CAM：%s；Euterpe：%s" % (cam_result.message, euterpe_result.message)
                result = ActionResult(item, ok, targets[0] if targets else None, message)
        except Exception as exc:  # 任何异常都转成失败结果，不抛给 UI
            result = ActionResult(item, False, None, "转换失败：%s" % exc)
        results.append(result)
        _notify(progress, "(%d/%d) %s → %s" % (index, total, label, result.message))
    _notify(progress, "转换完成：成功 %d，跳过/失败 %d" % (
        sum(1 for result in results if result.ok),
        sum(1 for result in results if not result.ok),
    ))
    return results


# ============================================================================
# MessagePack 编解码（标准库实现，仅用于 manifest.epk；SPEC §3）
# ============================================================================


def msgpack_encode(obj) -> bytes:
    """把 Python 对象编码成 MessagePack 字节串。"""
    buffer = bytearray()
    _msgpack_pack(obj, buffer)
    return bytes(buffer)


def _msgpack_pack(obj, out: bytearray) -> None:
    """递归编码单个对象。"""
    if obj is None:
        out.append(0xC0)
        return
    if obj is True:
        out.append(0xC3)
        return
    if obj is False:
        out.append(0xC2)
        return
    if isinstance(obj, int):
        if obj >= 0:
            if obj <= 0x7F:
                out.append(obj)
            elif obj <= 0xFF:
                out.append(0xCC)
                out += struct.pack(">B", obj)
            elif obj <= 0xFFFF:
                out.append(0xCD)
                out += struct.pack(">H", obj)
            elif obj <= 0xFFFFFFFF:
                out.append(0xCE)
                out += struct.pack(">I", obj)
            elif obj <= 0xFFFFFFFFFFFFFFFF:
                out.append(0xCF)
                out += struct.pack(">Q", obj)
            else:
                raise OverflowError("整数超出 uint64 范围：%r" % obj)
        else:
            if obj >= -32:
                out.append(obj & 0xFF)
            elif obj >= -128:
                out.append(0xD0)
                out += struct.pack(">b", obj)
            elif obj >= -32768:
                out.append(0xD1)
                out += struct.pack(">h", obj)
            elif obj >= -2147483648:
                out.append(0xD2)
                out += struct.pack(">i", obj)
            elif obj >= -9223372036854775808:
                out.append(0xD3)
                out += struct.pack(">q", obj)
            else:
                raise OverflowError("整数超出 int64 范围：%r" % obj)
        return
    if isinstance(obj, float):
        out.append(0xCB)
        out += struct.pack(">d", obj)
        return
    if isinstance(obj, str):
        data = obj.encode("utf-8")
        length = len(data)
        if length <= 31:
            out.append(0xA0 | length)
        elif length <= 0xFF:
            out.append(0xD9)
            out += struct.pack(">B", length)
        elif length <= 0xFFFF:
            out.append(0xDA)
            out += struct.pack(">H", length)
        elif length <= 0xFFFFFFFF:
            out.append(0xDB)
            out += struct.pack(">I", length)
        else:
            raise ValueError("字符串过长，超出 str32 范围")
        out += data
        return
    if isinstance(obj, (bytes, bytearray)):
        data = bytes(obj)
        length = len(data)
        if length <= 0xFF:
            out.append(0xC4)
            out += struct.pack(">B", length)
        elif length <= 0xFFFF:
            out.append(0xC5)
            out += struct.pack(">H", length)
        elif length <= 0xFFFFFFFF:
            out.append(0xC6)
            out += struct.pack(">I", length)
        else:
            raise ValueError("字节串过长，超出 bin32 范围")
        out += data
        return
    if isinstance(obj, dict):
        length = len(obj)
        if length <= 15:
            out.append(0x80 | length)
        elif length <= 0xFFFF:
            out.append(0xDE)
            out += struct.pack(">H", length)
        elif length <= 0xFFFFFFFF:
            out.append(0xDF)
            out += struct.pack(">I", length)
        else:
            raise ValueError("映射过大，超出 map32 范围")
        for key, value in obj.items():
            _msgpack_pack(key, out)
            _msgpack_pack(value, out)
        return
    if isinstance(obj, (list, tuple)):
        length = len(obj)
        if length <= 15:
            out.append(0x90 | length)
        elif length <= 0xFFFF:
            out.append(0xDC)
            out += struct.pack(">H", length)
        elif length <= 0xFFFFFFFF:
            out.append(0xDD)
            out += struct.pack(">I", length)
        else:
            raise ValueError("数组过大，超出 array32 范围")
        for value in obj:
            _msgpack_pack(value, out)
        return
    raise TypeError("无法编码的类型：%s" % type(obj).__name__)


def msgpack_decode(data):
    """把 MessagePack 字节串解码成 Python 对象（严格模式，拒绝多余尾部字节）。"""
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise TypeError("msgpack_decode 只接受 bytes/bytearray")
    value, offset = _msgpack_unpack(data, 0)
    if offset != len(data):
        raise ValueError("msgpack 数据末尾有 %d 字节多余内容" % (len(data) - offset))
    return value


def _msgpack_take(data: bytes, offset: int, length: int) -> bytes:
    """从 offset 取 length 字节；不足时抛 ValueError。"""
    end = offset + length
    if end > len(data):
        raise ValueError("msgpack 数据不完整（需要 %d 字节，实际剩余 %d）" % (
            length, len(data) - offset))
    return data[offset:end]


def _msgpack_unpack(data: bytes, offset: int):
    """从 offset 解码一个对象，返回 (对象, 新偏移)。"""
    if offset >= len(data):
        raise ValueError("msgpack 数据不完整")
    tag = data[offset]
    offset += 1

    # positive fixint / negative fixint
    if tag <= 0x7F:
        return tag, offset
    if tag >= 0xE0:
        return tag - 0x100, offset

    # fixmap / fixarray / fixstr
    if 0x80 <= tag <= 0x8F:
        return _msgpack_unpack_map(data, offset, tag & 0x0F)
    if 0x90 <= tag <= 0x9F:
        return _msgpack_unpack_array(data, offset, tag & 0x0F)
    if 0xA0 <= tag <= 0xBF:
        length = tag & 0x1F
        raw = _msgpack_take(data, offset, length)
        return raw.decode("utf-8"), offset + length

    if tag == 0xC0:
        return None, offset
    if tag == 0xC2:
        return False, offset
    if tag == 0xC3:
        return True, offset
    if tag == 0xC4:
        length = _msgpack_take(data, offset, 1)[0]
        offset += 1
        return _msgpack_take(data, offset, length), offset + length
    if tag == 0xC5:
        length = struct.unpack(">H", _msgpack_take(data, offset, 2))[0]
        offset += 2
        return _msgpack_take(data, offset, length), offset + length
    if tag == 0xC6:
        length = struct.unpack(">I", _msgpack_take(data, offset, 4))[0]
        offset += 4
        return _msgpack_take(data, offset, length), offset + length
    if tag == 0xCA:
        return struct.unpack(">f", _msgpack_take(data, offset, 4))[0], offset + 4
    if tag == 0xCB:
        return struct.unpack(">d", _msgpack_take(data, offset, 8))[0], offset + 8
    if tag == 0xCC:
        return _msgpack_take(data, offset, 1)[0], offset + 1
    if tag == 0xCD:
        return struct.unpack(">H", _msgpack_take(data, offset, 2))[0], offset + 2
    if tag == 0xCE:
        return struct.unpack(">I", _msgpack_take(data, offset, 4))[0], offset + 4
    if tag == 0xCF:
        return struct.unpack(">Q", _msgpack_take(data, offset, 8))[0], offset + 8
    if tag == 0xD0:
        return struct.unpack(">b", _msgpack_take(data, offset, 1))[0], offset + 1
    if tag == 0xD1:
        return struct.unpack(">h", _msgpack_take(data, offset, 2))[0], offset + 2
    if tag == 0xD2:
        return struct.unpack(">i", _msgpack_take(data, offset, 4))[0], offset + 4
    if tag == 0xD3:
        return struct.unpack(">q", _msgpack_take(data, offset, 8))[0], offset + 8
    if tag == 0xD9:
        length = _msgpack_take(data, offset, 1)[0]
        offset += 1
        raw = _msgpack_take(data, offset, length)
        return raw.decode("utf-8"), offset + length
    if tag == 0xDA:
        length = struct.unpack(">H", _msgpack_take(data, offset, 2))[0]
        offset += 2
        raw = _msgpack_take(data, offset, length)
        return raw.decode("utf-8"), offset + length
    if tag == 0xDB:
        length = struct.unpack(">I", _msgpack_take(data, offset, 4))[0]
        offset += 4
        raw = _msgpack_take(data, offset, length)
        return raw.decode("utf-8"), offset + length
    if tag == 0xDC:
        length = struct.unpack(">H", _msgpack_take(data, offset, 2))[0]
        return _msgpack_unpack_array(data, offset + 2, length)
    if tag == 0xDD:
        length = struct.unpack(">I", _msgpack_take(data, offset, 4))[0]
        return _msgpack_unpack_array(data, offset + 4, length)
    if tag == 0xDE:
        length = struct.unpack(">H", _msgpack_take(data, offset, 2))[0]
        return _msgpack_unpack_map(data, offset + 2, length)
    if tag == 0xDF:
        length = struct.unpack(">I", _msgpack_take(data, offset, 4))[0]
        return _msgpack_unpack_map(data, offset + 4, length)
    raise ValueError("不支持的 msgpack 类型标记：0x%02x（偏移 %d）" % (tag, offset - 1))


def _msgpack_unpack_array(data: bytes, offset: int, length: int):
    """解出 length 个元素的数组。"""
    values = []
    for _ in range(length):
        value, offset = _msgpack_unpack(data, offset)
        values.append(value)
    return values, offset


def _msgpack_unpack_map(data: bytes, offset: int, length: int):
    """解出 length 个键值对的映射。"""
    mapping = {}
    for _ in range(length):
        key, offset = _msgpack_unpack(data, offset)
        value, offset = _msgpack_unpack(data, offset)
        try:
            mapping[key] = value
        except TypeError:
            raise ValueError("msgpack 映射的键不可哈希：%r" % (key,))
    return mapping, offset