# -*- coding: utf-8 -*-
"""MuseDashChartFixer 核心逻辑单元测试（严格按 SPEC.md §7 与修订 R2）。

约定：
- 所有会写盘的测试都只操作 tests/out/core_work/ 下的 fixtures 副本；
- tests/fixtures 原目录等任何外部目录只读；
- 测试结束后自动校验 tests/fixtures 未被修改。
"""

import copy
import hashlib
import json
import os
import re
import shutil
import sys
import unittest
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
FIXTURES = os.path.join(BASE_DIR, "fixtures")
OUT_ROOT = os.path.join(BASE_DIR, "out")
WORK = os.path.join(OUT_ROOT, "core_work")

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import core  # noqa: E402  （必须在 sys.path 调整之后导入）

DAMAGED_MDM_NAMES = (
    "[Lv 10]MEGAMIX 2.mdm",
    "[lv 10]Cherry Pop.mdm",
    "[lv 10]Infinity Heaven.mdm",
    "[lv 10]memorys.mdm",
)
GOOD_MDM_NAMES = (
    "【LV.8.2】告白・告別.mdm",
    "【LV.9,？】はやくにげなきゃ（得赶快逃才行）.mdm",
)
REFERENCE_NAME = "reference_offline_lv9.json"
FOLDER_SAMPLE_NAME = "folder-mode sample"


# ============================================================================
# 通用辅助
# ============================================================================


def sha256_file(path):
    """计算文件 sha256。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8-sig"))


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def copy_or_link(src, dst):
    """大文件用硬链接（避免每次测试复制几十 MB），小文件真复制（可能被测试修改）。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        if os.path.getsize(src) > 65536:
            os.link(src, dst)
            return
    except OSError:
        pass
    shutil.copy2(src, dst)


def mirror_dir(src_dir, dst_dir):
    """把目录镜像到测试工作区（大文件链接、小文件复制）。"""
    os.makedirs(dst_dir, exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        dirs.sort(key=str.casefold)
        rel = os.path.relpath(root, src_dir)
        target_root = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in sorted(files, key=str.casefold):
            copy_or_link(os.path.join(root, name), os.path.join(target_root, name))


def make_zip(path, entries):
    """用 {条目名: bytes} 生成一个 zip。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def snapshot_tree(root):
    """记录目录下所有文件的 sha256，用于确认没有被写入。"""
    result = {}
    for current, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            full_path = os.path.join(current, name)
            result[os.path.relpath(full_path, root)] = sha256_file(full_path)
    return result


def semantic_diff(expected, actual, path=""):
    """语义比较：递归忽略 dict 键顺序；数值按值比较；返回差异描述列表。"""
    differences = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in set(expected) | set(actual):
            if key not in expected:
                differences.append("多出键 %s.%s" % (path, key))
            elif key not in actual:
                differences.append("缺少键 %s.%s" % (path, key))
            else:
                differences.extend(semantic_diff(expected[key], actual[key], "%s.%s" % (path, key)))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            differences.append("列表长度不同 %s：%d vs %d" % (path, len(expected), len(actual)))
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences.extend(semantic_diff(left, right, "%s[%d]" % (path, index)))
        return differences

    def is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    if is_number(expected) and is_number(actual):
        if expected != actual:
            differences.append("数值不同 %s：%r vs %r" % (path, expected, actual))
        return differences
    if expected != actual:
        differences.append("值不同 %s：%r vs %r" % (path, expected, actual))
    return differences


def sample_cam_info(name="测试谱面", bpm="150", scene="scene_02"):
    """构造一份最小可用的合法 CAM info.json。"""
    return {
        "name": name,
        "author": "测试谱师",
        "bpm": bpm,
        "scene": scene,
        "sceneEgg": "none",
        "levelDesigner": "测试谱师",
        "levelDesigner1": "测试谱师",
        "levelDesigner2": "测试谱师",
        "levelDesigner3": "测试谱师",
        "levelDesigner4": "测试谱师",
        "levelDesigner5": "测试谱师",
        "difficulty1": "0",
        "difficulty2": "7",
        "difficulty3": "0",
        "difficulty4": "0",
        "difficulty5": "0",
        "hideBmsMode": "CLICK",
        "hideBmsDifficulty": "0",
        "hideBmsMessage": "",
        "unlockLevel": "0",
        "searchTags": [],
        "battleUi": "default",
    }


def cam_zip_entries(cam_info, with_music=True):
    """构造一个最小合法 CAM .mdm 的条目字典。"""
    entries = {
        "cover.png": b"\x89PNG\r\n\x1a\n" + b"cover" * 20,
        "demo.ogg": b"OggS" + b"demo" * 20,
        "map2.bms": b"#PLAYER 2\r\n#GENRE scene_02\r\n#TITLE test\r\n#BPM 155\r\n#PLAYLEVEL 7\r\n",
    }
    if with_music:
        entries["music.ogg"] = b"OggS" + b"music" * 100
    entries["info.json"] = json.dumps(cam_info, ensure_ascii=False).encode("utf-8")
    return entries


# ============================================================================
# 测试用例
# ============================================================================


class CoreTestCase(unittest.TestCase):
    """所有测试的公共环境：准备一个假的游戏目录与 fixtures 副本。"""

    def setUp(self):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK, exist_ok=True)
        self.game = os.path.join(WORK, "game")
        self.custom_albums = os.path.join(self.game, "Custom_Albums")
        self.offline = os.path.join(self.game, "Euterpe_Charts", "Offline")
        os.makedirs(self.custom_albums, exist_ok=True)
        os.makedirs(self.offline, exist_ok=True)

        # 4 个损坏的 .mdm + 文件夹形态样本
        damaged_src = os.path.join(FIXTURES, "Custom_Albums")
        for name in DAMAGED_MDM_NAMES:
            copy_or_link(os.path.join(damaged_src, name), os.path.join(self.custom_albums, name))
        mirror_dir(
            os.path.join(damaged_src, FOLDER_SAMPLE_NAME),
            os.path.join(self.custom_albums, FOLDER_SAMPLE_NAME),
        )
        # 合法样本与官方迁移参考 manifest
        self.good_dir = os.path.join(WORK, "good")
        mirror_dir(os.path.join(FIXTURES, "good"), self.good_dir)
        # 完整 fixtures（用于全量扫描测试）
        self.all_fixtures = os.path.join(WORK, "all_fixtures")
        mirror_dir(FIXTURES, self.all_fixtures)

    # ---- 便捷方法 ----
    def damaged_path(self, name):
        return os.path.join(self.custom_albums, name)

    def good_path(self, name):
        return os.path.join(self.good_dir, name)

    def scan_one(self, path, **kwargs):
        items = core.scan([path], **kwargs)
        self.assertEqual(len(items), 1, "扫描结果数量不对：%r" % (items,))
        return items[0]

    def convert_one(self, item, plan, dry_run=False, game_dir=None, custom_albums=None, offline=None, progress=None):
        results = core.convert(
            [item],
            plan,
            game_dir=self.game if game_dir is None else game_dir,
            custom_albums_dir=self.custom_albums if custom_albums is None else custom_albums,
            offline_dir=self.offline if offline is None else offline,
            dry_run=dry_run,
            progress=progress,
        )
        self.assertEqual(len(results), 1)
        return results[0]


# ----------------------------------------------------------------------------
# 1. MessagePack 编解码
# ----------------------------------------------------------------------------


class MsgpackTests(CoreTestCase):
    def test_roundtrip_common_types(self):
        value = {
            "schema": 1,
            "cid": None,
            "meta": {
                "name": "中文名称❤",
                "bpm": 161,
                "safe": False,
                "flag": True,
                "score": 94.1,
                "maps": {"map2": {"rating": "8.2", "charters": ["巯_Q1uP7us"], "predicted_rating": None}},
            },
            "files": {"cover.png": {"version": 1, "size": 312397}},
        }
        encoded = core.msgpack_encode(value)
        self.assertIsInstance(encoded, bytes)
        self.assertEqual(core.msgpack_decode(encoded), value)

    def test_integer_boundaries(self):
        cases = [
            0, 127, 128, 255, 256, 65535, 65536, 2 ** 32 - 1, 2 ** 32, 2 ** 63,
            -1, -32, -33, -128, -129, -32768, -32769, -2 ** 31, -2 ** 31 - 1,
        ]
        for number in cases:
            with self.subTest(number=number):
                self.assertEqual(core.msgpack_decode(core.msgpack_encode(number)), number)

    def test_string_and_container_boundaries(self):
        short = "x" * 31
        medium = "中" * 40
        long_text = "y" * 300
        value = {
            "short": short,
            "medium": medium,
            "long": long_text,
            "empty": "",
        }
        self.assertEqual(core.msgpack_decode(core.msgpack_encode(value)), value)

        big_list = list(range(20))
        self.assertEqual(core.msgpack_decode(core.msgpack_encode(big_list)), big_list)

        big_map = {"k%d" % index: index for index in range(20)}
        self.assertEqual(core.msgpack_decode(core.msgpack_encode(big_map)), big_map)

    def test_encoder_uses_expected_tags(self):
        self.assertEqual(core.msgpack_encode(None), b"\xc0")
        self.assertEqual(core.msgpack_encode(True), b"\xc3")
        self.assertEqual(core.msgpack_encode(False), b"\xc2")
        self.assertEqual(core.msgpack_encode(0), b"\x00")
        self.assertEqual(core.msgpack_encode(127), b"\x7f")
        self.assertEqual(core.msgpack_encode(128), b"\xcc\x80")
        self.assertEqual(core.msgpack_encode(-1), b"\xff")
        self.assertEqual(core.msgpack_encode(-32), b"\xe0")
        self.assertEqual(core.msgpack_encode(-33), b"\xd0\xdf")
        self.assertEqual(core.msgpack_encode(1.5)[0:1], b"\xcb")
        self.assertEqual(core.msgpack_encode("abcd")[0:1], b"\xa4")
        self.assertEqual(core.msgpack_encode([1, 2])[0:1], b"\x92")
        self.assertEqual(core.msgpack_encode({"a": 1})[0:1], b"\x81")

    def test_decode_rejects_invalid_data(self):
        with self.assertRaises(ValueError):
            core.msgpack_decode(b"\xc1")  # 保留标记
        with self.assertRaises(ValueError):
            core.msgpack_decode(b"\xa5ab")  # 字符串被截断
        with self.assertRaises(ValueError):
            core.msgpack_decode(core.msgpack_encode(1) + b"\x00")  # 多余尾部字节
        with self.assertRaises(TypeError):
            core.msgpack_decode("not-bytes")

    def test_float_bpm_roundtrip_keeps_float(self):
        manifest = {
            "meta": {"bpm": 94.1, "name": "x"},
            "files": {"cover.png": {"version": 1, "size": 1}},
        }
        decoded = core.msgpack_decode(core.msgpack_encode(manifest))
        self.assertIsInstance(decoded["meta"]["bpm"], float)
        self.assertAlmostEqual(decoded["meta"]["bpm"], 94.1)


# ----------------------------------------------------------------------------
# 2. 扫描与损坏判定
# ----------------------------------------------------------------------------


class ScanTests(CoreTestCase):
    def test_scan_four_damaged_samples(self):
        paths = [self.damaged_path(name) for name in DAMAGED_MDM_NAMES]
        items = core.scan(paths)
        self.assertEqual(len(items), 4)
        for item in items:
            with self.subTest(item=item.path):
                self.assertEqual(item.kind, "mdm")
                self.assertTrue(item.needs_fix)
                self.assertTrue(item.chart_name)
                self.assertTrue(item.author)
                self.assertIsInstance(item.payload, dict)
                joined = "\n".join(item.issues)
                self.assertIn("BOM", joined)
                self.assertIn("缺少 CAM 顶层字段", joined)
                self.assertIn("Euterpe", joined)
        # MEGAMIX 的 bpm 必须能从 BMS 兜底为 160（不是 0）
        megamix = next(item for item in items if "MEGAMIX" in item.chart_name)
        self.assertEqual(megamix.payload["manifest"]["meta"]["bpm"], 160)
        self.assertTrue(any("bpm" in message for message in megamix.errors))
        # 每个损坏样本重建出的 CAM info 都应能通过校验
        for item in items:
            errors, _warnings = core.validate_cam_info(item.payload["cam_info"])
            self.assertEqual(errors, [], "%s 重建的 CAM info 不合法：%r" % (item.path, errors))

    def test_scan_valid_cam_has_no_errors(self):
        item = self.scan_one(self.good_path(GOOD_MDM_NAMES[0]))
        self.assertFalse(item.needs_fix)
        self.assertEqual(item.errors, [])
        self.assertEqual(item.kind, "mdm")

    def test_scan_map4_warning(self):
        item = self.scan_one(self.good_path(GOOD_MDM_NAMES[1]))
        self.assertFalse(item.needs_fix)
        joined = "\n".join(item.warnings)
        self.assertIn("map4/map5", joined)
        self.assertIn("前三个难度", joined)
        self.assertIn("方案 2", joined)

    def test_scan_full_fixtures_tree(self):
        items = core.scan([self.all_fixtures], recursive=True)
        kinds = sorted((item.kind, item.needs_fix) for item in items)
        self.assertEqual(len(items), 7)
        self.assertIn(("folder", True), kinds)
        self.assertIn(("mdm", False), kinds)
        folder_item = next(item for item in items if item.kind == "folder")
        self.assertTrue(folder_item.needs_fix)
        self.assertIn("缺少 CAM 顶层字段", "\n".join(folder_item.issues))
        self.assertTrue(folder_item.chart_name)

    def test_scan_recursive_flag(self):
        nested = os.path.join(WORK, "nested", "deep")
        os.makedirs(nested, exist_ok=True)
        copy_or_link(
            self.damaged_path(DAMAGED_MDM_NAMES[0]),
            os.path.join(nested, "nested chart.mdm"),
        )
        self.assertEqual(core.scan([os.path.join(WORK, "nested")], recursive=False), [])
        found = core.scan([os.path.join(WORK, "nested")], recursive=True)
        self.assertEqual(len(found), 1)
        # 直接扫描 .mdm 文件不受 recursive 影响
        self.assertEqual(len(core.scan([os.path.join(nested, "nested chart.mdm")], recursive=False)), 1)

    def test_scan_folder_kind(self):
        folder = os.path.join(self.custom_albums, FOLDER_SAMPLE_NAME)
        item = self.scan_one(folder)
        self.assertEqual(item.kind, "folder")
        self.assertTrue(item.needs_fix)
        self.assertEqual(item.chart_name, "Memories")
        self.assertTrue(item.payload["bom"])
        self.assertNotIn("缺少 music.ogg", "\n".join(item.warnings))  # 该样本有 music.ogg

    def test_scan_missing_info_and_music(self):
        # 缺 info.json
        no_info_zip = make_zip(os.path.join(WORK, "synthetic", "no_info.mdm"), {
            "cover.png": b"png",
            "demo.ogg": b"ogg",
            "map2.bms": b"#BPM 155\r\n#GENRE scene_02\r\n#PLAYLEVEL 7\r\n",
            "music.ogg": b"music",
        })
        item = self.scan_one(no_info_zip)
        self.assertTrue(item.needs_fix)
        self.assertIn("缺少 info.json", "\n".join(item.issues))
        self.assertEqual(item.payload["manifest"]["meta"]["bpm"], 155)
        self.assertEqual(item.payload["manifest"]["meta"]["scene"], "scene_02")
        self.assertEqual(item.payload["manifest"]["meta"]["maps"]["map2"]["rating"], "7")

        # 缺 music.ogg（warning）
        missing_music_zip = make_zip(
            os.path.join(WORK, "synthetic", "no_music.mdm"),
            cam_zip_entries(sample_cam_info(), with_music=False),
        )
        item = self.scan_one(missing_music_zip)
        self.assertFalse(item.needs_fix)
        self.assertIn("music.ogg", "\n".join(item.warnings))

    def test_scan_invalid_values_are_reported(self):
        bad = sample_cam_info()
        bad["bpm"] = "0"
        bad["scene"] = "scene_99"
        bad["difficulty2"] = 0  # map2.bms 存在但难度为 0
        bad["difficulty3"] = "5"  # 有难度但包内没有 map3.bms
        path = make_zip(os.path.join(WORK, "synthetic", "bad_values.mdm"), cam_zip_entries(bad))
        item = self.scan_one(path)
        self.assertTrue(item.needs_fix)
        joined = "\n".join(item.issues)
        self.assertIn("bpm 非正", joined)
        self.assertIn("scene 非法", joined)
        self.assertIn("difficulty2", joined)
        self.assertIn("difficulty3", joined)

    def test_scan_broken_zip_and_missing_path(self):
        broken = os.path.join(WORK, "synthetic", "broken.mdm")
        write_bytes(broken, b"this is not a zip")
        item = self.scan_one(broken)
        self.assertTrue(item.needs_fix)
        self.assertTrue(item.errors)
        self.assertEqual(core.scan([os.path.join(WORK, "不存在")]), [])

    def test_scan_reports_progress(self):
        messages = []
        core.scan([self.damaged_path(DAMAGED_MDM_NAMES[0])], progress=messages.append)
        self.assertTrue(messages)
        self.assertTrue(any("扫描" in message for message in messages))


# ----------------------------------------------------------------------------
# 3. manifest / CAM info 生成与校验
# ----------------------------------------------------------------------------


class ManifestTests(CoreTestCase):
    def test_bms_fallback_rules(self):
        raw = {
            "name": "X",
            "author": "Y",
            "bpm": "0",
            "scene": "bad_scene",
            "difficulty2": "5",
            "levelDesigner": "Z",
        }
        bms = {"map2.bms": "#BPM 160\n#GENRE scene_04\n#PLAYLEVEL 8.5\n"}
        sizes = {"cover.png": 10, "demo.ogg": 20, "map2.bms": 30, "music.ogg": 40}
        manifest = core.to_manifest(raw, bms, sizes)
        self.assertEqual(manifest["meta"]["bpm"], 160)
        self.assertEqual(manifest["meta"]["scene"], "scene_04")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["rating"], "5")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["charters"], ["Z"])
        self.assertEqual(sorted(manifest["files"]), ["cover.png", "demo.ogg", "map2.bms", "music.ogg"])
        self.assertEqual(manifest["files"]["music.ogg"], {"version": 1, "size": 40})
        self.assertEqual(manifest["schema"], 1)
        self.assertIsNone(manifest["cid"])

        # 没有任何兜底信息时使用默认值
        empty = core.to_manifest({}, {}, {})
        self.assertEqual(empty["meta"]["bpm"], 120)
        self.assertEqual(empty["meta"]["scene"], "scene_01")
        self.assertEqual(empty["meta"]["name"], "Untitled")
        self.assertEqual(empty["meta"]["author"], "Unknown")
        self.assertEqual(empty["meta"]["maps"], {})

        # 小数 bpm 保持 float
        decimal = core.to_manifest({"bpm": "94.1"}, {}, {})
        self.assertIsInstance(decimal["meta"]["bpm"], float)
        self.assertAlmostEqual(decimal["meta"]["bpm"], 94.1)

    def test_euterpe_style_input_priority(self):
        raw = {
            "schema": 1,
            "cid": 1015,
            "meta": {
                "name": "N",
                "author": "A",
                "bpm": 0,
                "scene": "scene_05",
                "maps": {"map2": {"rating": "8.2", "charters": ["谱师"]}},
                "hide_mode": "press",
                "hide_rating_override": "?",
                "hide_message": "hi",
                "search_keywords": ["kw"],
            },
            "files": {"map2.bms": {"version": 1, "size": 5}},
        }
        bms = {"map2.bms": "#BPM 999\n#GENRE scene_09\n"}
        manifest = core.to_manifest(raw, bms, {"map2.bms": 5})
        self.assertEqual(manifest["meta"]["bpm"], 999)  # meta.bpm=0 非法 -> BMS
        self.assertEqual(manifest["meta"]["scene"], "scene_05")  # 合法 meta.scene 优先
        self.assertEqual(manifest["meta"]["maps"]["map2"]["rating"], "8.2")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["charters"], ["谱师"])
        self.assertEqual(manifest["meta"]["hide_mode"], "PRESS")
        self.assertEqual(manifest["meta"]["hide_rating_override"], "?")
        self.assertEqual(manifest["meta"]["hide_message"], "hi")
        self.assertEqual(manifest["meta"]["scene_egg"], None)
        self.assertEqual(manifest["meta"]["search_keywords"], ["kw"])

    def test_only_existing_map_slots(self):
        sizes = {"map2.bms": 10, "map4.bms": 20, "music.ogg": 30}
        manifest = core.to_manifest({}, {}, sizes)
        self.assertEqual(sorted(manifest["meta"]["maps"]), ["map2", "map4"])
        self.assertEqual(manifest["meta"]["maps"]["map2"]["rating"], "1")

    def test_build_cam_info_field_order_and_values(self):
        manifest = core.to_manifest(
            {"name": "名", "author": "作者", "bpm": "94.1", "scene": "scene_03",
             "difficulty2": "9", "levelDesigner2": "谱师A"},
            {"map2.bms": "#PLAYLEVEL 9\n"},
            {"map2.bms": 10, "music.ogg": 20},
        )
        manifest["meta"]["hide_rating_override"] = "?"
        info = core.build_cam_info(manifest)
        self.assertEqual(list(info.keys()), list(core.CAM_TOP_KEYS))
        self.assertEqual(info["name"], "名")
        self.assertEqual(info["author"], "作者")
        self.assertEqual(info["bpm"], "94.1")
        self.assertEqual(info["scene"], "scene_03")
        self.assertEqual(info["sceneEgg"], "none")
        self.assertEqual(info["difficulty2"], "9")
        self.assertEqual(info["difficulty1"], "0")
        self.assertEqual(info["difficulty4"], "0")
        self.assertEqual(info["levelDesigner2"], "谱师A")
        self.assertEqual(info["levelDesigner1"], "谱师A")
        self.assertEqual(info["hideBmsDifficulty"], "?")
        self.assertEqual(info["unlockLevel"], "0")
        self.assertEqual(info["battleUi"], "default")
        self.assertEqual(core.validate_cam_info(info)[0], [])

    def test_cam_serialization(self):
        info = sample_cam_info()
        data = core._serialize_cam_info(info)
        self.assertFalse(data.startswith(core.UTF8_BOM))
        self.assertIn(b"\r\n", data)
        self.assertTrue(data.endswith(b"\r\n"))
        self.assertIn("测试谱面".encode("utf-8"), data)  # ensure_ascii=False
        self.assertEqual(json.loads(data.decode("utf-8")), info)

    def test_validate_cam_info_rejects_bad_data(self):
        self.assertTrue(core.validate_cam_info("not a dict")[0])
        info = sample_cam_info()
        self.assertEqual(core.validate_cam_info(info)[0], [])

        broken = copy.deepcopy(info)
        del broken["battleUi"]
        broken["bpm"] = "0"
        broken["scene"] = "scene_13"
        broken["difficulty2"] = 9
        broken["searchTags"] = "not-a-list"
        errors, _warnings = core.validate_cam_info(broken)
        joined = "\n".join(errors)
        self.assertIn("缺少字段 battleUi", joined)
        self.assertIn("bpm 必须为正数", joined)
        self.assertIn("scene 非法", joined)
        self.assertIn("difficulty2", joined)
        self.assertIn("searchTags", joined)


# ----------------------------------------------------------------------------
# 4. 方案1：就地修复 .mdm 内的 info.json（SPEC R2 §8）
# ----------------------------------------------------------------------------


class CamInplaceTests(CoreTestCase):
    def test_cam_fix_megamix(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[0])
        with zipfile.ZipFile(path) as archive:
            before_entries = [info.filename for info in archive.infolist()]
            before_meta = {
                info.filename: (info.date_time, info.compress_type, hashlib.sha256(archive.read(info)).hexdigest())
                for info in archive.infolist()
                if info.filename.casefold() != "info.json"
            }
        before_sha = sha256_file(path)
        item = self.scan_one(path)
        result = self.convert_one(item, "cam")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(os.path.normcase(result.target), os.path.normcase(path))
        self.assertNotEqual(sha256_file(path), before_sha)

        with zipfile.ZipFile(path) as archive:
            after_entries = [info.filename for info in archive.infolist()]
            self.assertEqual(after_entries, before_entries)  # 条目顺序不变
            after_meta = {
                info.filename: (info.date_time, info.compress_type, hashlib.sha256(archive.read(info)).hexdigest())
                for info in archive.infolist()
                if info.filename.casefold() != "info.json"
            }
            self.assertEqual(after_meta, before_meta)  # 其它条目字节/时间戳/压缩方式不变
            raw_info = archive.read("info.json")
        self.assertFalse(raw_info.startswith(core.UTF8_BOM))
        self.assertIn(b"\r\n", raw_info)
        info = json.loads(raw_info.decode("utf-8"))
        self.assertEqual(info["bpm"], "160")  # 必证：MEGAMIX 用 BMS 兜底为 160 而不是 0
        self.assertEqual(info["scene"], "scene_03")
        self.assertEqual(info["difficulty2"], "10")
        errors, _warnings = core.validate_cam_info(info)
        self.assertEqual(errors, [])

        # 备份目录必须在 Custom_Albums 之外
        backup_dir = os.path.join(self.game, "_ChartFixerBackup", "[Lv 10]MEGAMIX 2")
        self.assertTrue(os.path.isdir(backup_dir))
        self.assertFalse(os.path.exists(os.path.join(self.custom_albums, "_ChartFixerBackup")))
        backup_info = read_bytes(os.path.join(backup_dir, "info.json"))
        self.assertTrue(backup_info.startswith(core.UTF8_BOM))
        record = read_json(os.path.join(backup_dir, "record.json"))
        self.assertEqual(record["sha256"], hashlib.sha256(backup_info).hexdigest())
        self.assertEqual(os.path.normcase(record["source_path"]), os.path.normcase(path))
        self.assertTrue(record["time"])

        # 修复后重新扫描：不再是 needs_fix
        rescan = self.scan_one(path)
        self.assertFalse(rescan.needs_fix)
        self.assertEqual(rescan.errors, [])

    def test_cam_fix_is_idempotent(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[2])  # Infinity Heaven
        item = self.scan_one(path)
        self.assertTrue(self.convert_one(item, "cam").ok)
        first_sha = sha256_file(path)
        backup_info = read_bytes(
            os.path.join(self.game, "_ChartFixerBackup", "[lv 10]Infinity Heaven", "info.json")
        )
        # 用同一份过期 item 再修一次：内容不变、备份不被覆盖
        self.assertTrue(self.convert_one(item, "cam").ok)
        self.assertEqual(sha256_file(path), first_sha)
        backup_info_again = read_bytes(
            os.path.join(self.game, "_ChartFixerBackup", "[lv 10]Infinity Heaven", "info.json")
        )
        self.assertEqual(backup_info_again, backup_info)

    def test_cam_fix_dry_run_writes_nothing(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[1])
        before_sha = sha256_file(path)
        before_tree = snapshot_tree(self.game)
        item = self.scan_one(path)
        result = self.convert_one(item, "cam", dry_run=True)
        self.assertTrue(result.ok, result.message)
        self.assertIn("预览", result.message)
        self.assertEqual(sha256_file(path), before_sha)
        self.assertEqual(snapshot_tree(self.game), before_tree)

    def test_cam_fix_missing_info_rebuild(self):
        path = make_zip(os.path.join(WORK, "synthetic", "no_info_fix.mdm"), {
            "cover.png": b"png",
            "demo.ogg": b"ogg",
            "map2.bms": b"#BPM 155\r\n#GENRE scene_02\r\n#PLAYLEVEL 7\r\n",
            "music.ogg": b"music",
        })
        item = self.scan_one(path)
        self.assertTrue(item.needs_fix)
        self.assertIn("缺少 info.json", "\n".join(item.issues))
        result = self.convert_one(item, "cam")
        self.assertTrue(result.ok, result.message)
        self.assertIn("补建", result.message)
        with zipfile.ZipFile(path) as archive:
            info = json.loads(archive.read("info.json").decode("utf-8"))
        self.assertEqual(info["bpm"], "155")
        self.assertEqual(info["scene"], "scene_02")
        self.assertEqual(info["difficulty2"], "7")
        self.assertEqual(core.validate_cam_info(info)[0], [])
        # 原本没有 info.json，备份目录只需要 record.json
        backup_dir = os.path.join(self.game, "_ChartFixerBackup", "no_info_fix")
        self.assertTrue(os.path.isdir(backup_dir))
        self.assertFalse(os.path.exists(os.path.join(backup_dir, "info.json")))
        record = read_json(os.path.join(backup_dir, "record.json"))
        self.assertIsNone(record["sha256"])

    def test_cam_fix_folder_kind(self):
        folder = os.path.join(self.custom_albums, FOLDER_SAMPLE_NAME)
        before_other = {}
        for name in os.listdir(folder):
            if name.casefold() != "info.json":
                before_other[name] = sha256_file(os.path.join(folder, name))
        original_info = read_bytes(os.path.join(folder, "info.json"))

        item = self.scan_one(folder)
        self.assertEqual(item.kind, "folder")
        self.assertTrue(item.needs_fix)
        result = self.convert_one(item, "cam")
        self.assertTrue(result.ok, result.message)

        new_info_bytes = read_bytes(os.path.join(folder, "info.json"))
        self.assertFalse(new_info_bytes.startswith(core.UTF8_BOM))
        info = json.loads(new_info_bytes.decode("utf-8"))
        self.assertEqual(info["bpm"], "125")
        self.assertEqual(core.validate_cam_info(info)[0], [])
        # 其它文件不动
        for name, digest in before_other.items():
            self.assertEqual(sha256_file(os.path.join(folder, name)), digest)
        # 备份在 Custom_Albums 之外，内容为原始（带 BOM）info.json
        backup_path = os.path.join(self.game, "_ChartFixerBackup", FOLDER_SAMPLE_NAME, "info.json")
        self.assertEqual(read_bytes(backup_path), original_info)
        self.assertTrue(read_bytes(backup_path).startswith(core.UTF8_BOM))
        self.assertFalse(os.path.exists(os.path.join(self.custom_albums, "_ChartFixerBackup")))

        # 再次扫描不再需要修复，再次转换会跳过
        rescan = self.scan_one(folder)
        self.assertFalse(rescan.needs_fix)
        skipped = self.convert_one(rescan, "cam")
        self.assertFalse(skipped.ok)
        self.assertIn("无需修复", skipped.message)

    def test_cam_fix_does_not_touch_source_for_valid_item(self):
        path = self.good_path(GOOD_MDM_NAMES[0])
        item = self.scan_one(path)
        self.assertFalse(item.needs_fix)
        before_sha = sha256_file(path)
        result = self.convert_one(item, "cam")
        self.assertFalse(result.ok)
        self.assertIn("无需修复", result.message)
        self.assertEqual(sha256_file(path), before_sha)


# ----------------------------------------------------------------------------
# 5. 方案1b：解压为 CAM 文件夹
# ----------------------------------------------------------------------------


class CamFolderTests(CoreTestCase):
    def test_cam_folder_megamix(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[0])
        source_sha = sha256_file(path)
        item = self.scan_one(path)
        result = self.convert_one(item, "cam_folder")
        self.assertTrue(result.ok, result.message)
        target = result.target
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(sorted(os.listdir(target)),
                         sorted(["cover.png", "demo.ogg", "map2.bms", "music.ogg", "info.json"]))
        info = read_json(os.path.join(target, "info.json"))
        self.assertEqual(info["bpm"], "160")
        self.assertEqual(core.validate_cam_info(info)[0], [])
        self.assertEqual(sha256_file(path), source_sha)  # 源 .mdm 不变

        # 重复执行跳过，不覆盖已存在目标
        sentinel = os.path.join(target, "sentinel.txt")
        write_bytes(sentinel, b"keep")
        second = self.convert_one(self.scan_one(path), "cam_folder")
        self.assertFalse(second.ok)
        self.assertIn("已存在", second.message)
        self.assertTrue(os.path.isfile(sentinel))
        self.assertEqual(sha256_file(path), source_sha)

    def test_cam_folder_dry_run(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[3])
        item = self.scan_one(path)
        before_tree = snapshot_tree(self.game)
        result = self.convert_one(item, "cam_folder", dry_run=True)
        self.assertTrue(result.ok)
        self.assertIn("预览", result.message)
        self.assertFalse(os.path.exists(result.target))
        self.assertEqual(snapshot_tree(self.game), before_tree)

    def test_cam_folder_on_folder_kind_fixes_in_place(self):
        """文件夹本身已是 CAM 形态：cam_folder 等价于就地修复其 info.json。"""
        folder = os.path.join(self.custom_albums, FOLDER_SAMPLE_NAME)
        item = self.scan_one(folder)
        self.assertTrue(item.needs_fix)
        result = self.convert_one(item, "cam_folder")
        self.assertTrue(result.ok, result.message)
        self.assertIn("CAM 形态", result.message)
        info = read_json(os.path.join(folder, "info.json"))
        self.assertEqual(core.validate_cam_info(info)[0], [])
        # 合法文件夹再执行返回“无需处理”，仍算成功
        valid_item = self.scan_one(folder)
        self.assertFalse(valid_item.needs_fix)
        again = self.convert_one(valid_item, "cam_folder")
        self.assertTrue(again.ok)
        self.assertIn("无需处理", again.message)


# ----------------------------------------------------------------------------
# 6. 方案2：Euterpe 离线谱
# ----------------------------------------------------------------------------


class EuterpeTests(CoreTestCase):
    def test_euterpe_conversion_structure(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[1])  # Cherry Pop
        source_sha = sha256_file(path)
        with zipfile.ZipFile(path) as archive:
            package_sizes = {
                name: archive.getinfo(name).file_size
                for name in archive.namelist()
                if name.casefold() != "info.json"
            }
        item = self.scan_one(path)
        result = self.convert_one(item, "euterpe")
        self.assertTrue(result.ok, result.message)
        target = result.target
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(sorted(os.listdir(target)),
                         sorted(list(package_sizes) + ["manifest.epk"]))

        manifest = core.msgpack_decode(read_bytes(os.path.join(target, "manifest.epk")))
        self.assertEqual(manifest["schema"], 1)
        self.assertIsNone(manifest["cid"])
        meta = manifest["meta"]
        self.assertGreater(meta["bpm"], 0)
        self.assertRegex(meta["scene"], r"^scene_(0[1-9]|1[0-2])$")
        self.assertIsNone(meta["scene_egg"])
        self.assertEqual(meta["hide_mode"], "CLICK")
        self.assertEqual(sorted(meta["maps"]),
                         sorted("map%d" % slot for slot in range(1, 6)
                                if "map%d.bms" % slot in [name.casefold() for name in package_sizes]))
        for name, entry in manifest["files"].items():
            self.assertEqual(entry["version"], 1)
            self.assertEqual(entry["size"], os.path.getsize(os.path.join(target, name)))
            self.assertEqual(entry["size"], package_sizes[name])
        self.assertNotIn("manifest.epk", manifest["files"])
        self.assertEqual(sha256_file(path), source_sha)  # 源 .mdm 不变

        # 重名时追加 " (2)"，不覆盖第一次的结果
        first_manifest_bytes = read_bytes(os.path.join(target, "manifest.epk"))
        second = self.convert_one(self.scan_one(path), "euterpe")
        self.assertTrue(second.ok)
        self.assertNotEqual(os.path.normcase(second.target), os.path.normcase(target))
        self.assertIn("(2)", os.path.basename(second.target))
        self.assertEqual(read_bytes(os.path.join(target, "manifest.epk")), first_manifest_bytes)

    def test_euterpe_reference_regression(self):
        """回归：合法 CAM .mdm 转换结果必须与 Euterpe App 官方迁移 manifest 语义一致。"""
        path = self.good_path(GOOD_MDM_NAMES[1])
        reference = read_json(os.path.join(self.good_dir, REFERENCE_NAME))
        source_sha = sha256_file(path)
        item = self.scan_one(path)
        self.assertFalse(item.needs_fix)
        result = self.convert_one(item, "euterpe")
        self.assertTrue(result.ok, result.message)
        manifest = core.msgpack_decode(read_bytes(os.path.join(result.target, "manifest.epk")))
        differences = semantic_diff(reference, manifest)
        self.assertEqual(differences, [], "与官方迁移 manifest 不一致：\n" + "\n".join(differences))
        # 明确校验关键字段
        self.assertEqual(manifest["meta"]["hide_rating_override"], "?")
        self.assertEqual(sorted(manifest["meta"]["maps"]), ["map2", "map4"])
        self.assertEqual(manifest["meta"]["maps"]["map4"]["rating"], "?")
        self.assertIsNone(manifest["meta"]["scene_egg"])
        self.assertEqual(list(manifest["files"]),  # 与包内条目顺序一致
                         ["cover.png", "demo.ogg", "map2.bms", "map4.bms", "map4.json", "music.ogg"])
        self.assertEqual(sha256_file(path), source_sha)

    def test_euterpe_good_cam_isomorphic(self):
        path = self.good_path(GOOD_MDM_NAMES[0])
        item = self.scan_one(path)
        self.assertFalse(item.needs_fix)
        source_sha = sha256_file(path)
        result = self.convert_one(item, "euterpe")
        self.assertTrue(result.ok, result.message)
        manifest = core.msgpack_decode(read_bytes(os.path.join(result.target, "manifest.epk")))
        self.assertEqual(list(manifest.keys()), ["schema", "cid", "meta", "files"])
        self.assertEqual(manifest["schema"], 1)
        self.assertIsNone(manifest["cid"])
        self.assertEqual(manifest["meta"]["bpm"], 161)
        self.assertIsInstance(manifest["meta"]["bpm"], int)
        self.assertEqual(manifest["meta"]["scene"], "scene_05")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["rating"], "8.2")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["charters"], ["巯_Q1uP7us"])
        self.assertEqual(manifest["meta"]["hide_mode"], "CLICK")
        self.assertEqual(manifest["meta"]["hide_rating_override"], "0")
        self.assertIsNone(manifest["meta"]["scene_egg"])
        # 与真实离线样本同构：meta 键集合一致
        expected_meta_keys = {
            "name", "name_romanized", "author", "description", "safe_for_streamer", "bpm",
            "bpm_min", "bpm_max", "scene", "scene_egg", "background_video_opacity",
            "search_keywords", "maps", "hide_mode", "hide_rating_override", "hide_message",
            "cover_dominant_color", "uploader", "created_at", "updated_at",
        }
        self.assertEqual(set(manifest["meta"].keys()), expected_meta_keys)
        for name, entry in manifest["files"].items():
            self.assertEqual(entry["size"], os.path.getsize(os.path.join(result.target, name)))
        self.assertEqual(sha256_file(path), source_sha)

    def test_euterpe_missing_music_warning(self):
        path = make_zip(
            os.path.join(WORK, "synthetic", "no_music_euterpe.mdm"),
            cam_zip_entries(sample_cam_info(), with_music=False),
        )
        item = self.scan_one(path)
        self.assertIn("music.ogg", "\n".join(item.warnings))
        result = self.convert_one(item, "euterpe")
        self.assertTrue(result.ok, result.message)  # 仍生成
        self.assertIn("警告", result.message)
        manifest = core.msgpack_decode(read_bytes(os.path.join(result.target, "manifest.epk")))
        self.assertNotIn("music.ogg", manifest["files"])

    def test_euterpe_from_folder_kind(self):
        folder = os.path.join(self.custom_albums, FOLDER_SAMPLE_NAME)
        item = self.scan_one(folder)
        result = self.convert_one(item, "euterpe")
        self.assertTrue(result.ok, result.message)
        manifest = core.msgpack_decode(read_bytes(os.path.join(result.target, "manifest.epk")))
        self.assertEqual(manifest["meta"]["name"], "Memories")
        self.assertEqual(manifest["meta"]["bpm"], 125)
        self.assertEqual(manifest["meta"]["scene"], "scene_05")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["rating"], "10")
        self.assertEqual(manifest["meta"]["maps"]["map2"]["charters"], ["鱼"])
        self.assertEqual(sorted(manifest["files"]), ["cover.png", "demo.ogg", "map2.bms", "music.ogg"])
        for name, entry in manifest["files"].items():
            self.assertEqual(entry["size"], os.path.getsize(os.path.join(result.target, name)))
            self.assertEqual(entry["size"], os.path.getsize(os.path.join(folder, name)))

    def test_euterpe_dry_run_writes_nothing(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[0])
        item = self.scan_one(path)
        before_tree = snapshot_tree(self.game)
        before_sha = sha256_file(path)
        result = self.convert_one(item, "euterpe", dry_run=True)
        self.assertTrue(result.ok)
        self.assertIn("预览", result.message)
        self.assertFalse(os.path.exists(result.target))
        self.assertEqual(os.listdir(self.offline), [])
        self.assertEqual(snapshot_tree(self.game), before_tree)
        self.assertEqual(sha256_file(path), before_sha)


# ----------------------------------------------------------------------------
# 7. 组合方案 / 错误处理 / 安全
# ----------------------------------------------------------------------------


class ConvertBehaviorTests(CoreTestCase):
    def test_plan_both(self):
        path = self.damaged_path(DAMAGED_MDM_NAMES[0])
        item = self.scan_one(path)
        result = self.convert_one(item, "both")
        self.assertTrue(result.ok, result.message)
        self.assertIn("CAM", result.message)
        self.assertIn("Euterpe", result.message)
        # CAM 就地修复：源 .mdm 已变
        with zipfile.ZipFile(path) as archive:
            info = json.loads(archive.read("info.json").decode("utf-8"))
        self.assertEqual(info["bpm"], "160")
        # Euterpe 目标也存在
        euterpe_dirs = os.listdir(self.offline)
        self.assertEqual(len(euterpe_dirs), 1)
        manifest = core.msgpack_decode(read_bytes(os.path.join(self.offline, euterpe_dirs[0], "manifest.epk")))
        self.assertEqual(manifest["meta"]["bpm"], 160)

    def test_unknown_plan_returns_failed_actions(self):
        item = self.scan_one(self.damaged_path(DAMAGED_MDM_NAMES[0]))
        results = core.convert([item], "不存在的方案", game_dir=self.game)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("未知方案", results[0].message)
        self.assertEqual(core.convert([item], None, game_dir=self.game)[0].ok, False)

    def test_convert_handles_bad_item_without_raising(self):
        broken = os.path.join(WORK, "synthetic", "broken2.mdm")
        write_bytes(broken, b"not a zip")
        item = core.ScanItem(path=broken, kind="mdm")
        item.payload = {"kind": "mdm", "source": broken, "entries": [], "sizes": {},
                        "manifest": core.to_manifest({}, {}, {}), "cam_info": core.build_cam_info({}),
                        "info_present": False, "info_bytes": None, "bms_texts": {}, "raw_info": None}
        for plan in ("cam", "cam_folder", "euterpe", "both"):
            results = core.convert([item], plan, game_dir=self.game,
                                   custom_albums_dir=self.custom_albums, offline_dir=self.offline)
            self.assertEqual(len(results), 1)
            self.assertIsInstance(results[0].ok, bool)
            self.assertIsInstance(results[0].message, str)

        # 完全空 item（没有 payload 也无法重读）也不能抛异常
        empty_item = core.ScanItem(path=os.path.join(WORK, "nowhere.mdm"), kind="mdm")
        result = core.convert([empty_item], "cam", game_dir=self.game)[0]
        self.assertFalse(result.ok)
        self.assertIn("无法处理", result.message)

    def test_convert_missing_directories(self):
        item = self.scan_one(self.damaged_path(DAMAGED_MDM_NAMES[0]))
        result = core.convert([item], "euterpe", game_dir=None, custom_albums_dir=None, offline_dir=None)[0]
        self.assertFalse(result.ok)
        self.assertIn("Euterpe", result.message)
        result = core.convert([item], "cam_folder", game_dir=None, custom_albums_dir=None, offline_dir=None)[0]
        self.assertFalse(result.ok)
        self.assertIn("Custom_Albums", result.message)

    def test_plan_cam_uses_inferred_backup_root(self):
        """不传 game_dir 时，从 Custom_Albums 位置反推备份根目录。"""
        path = self.damaged_path(DAMAGED_MDM_NAMES[3])
        item = self.scan_one(path)
        results = core.convert([item], "cam", game_dir=None,
                               custom_albums_dir=self.custom_albums, offline_dir=None)
        self.assertTrue(results[0].ok, results[0].message)
        self.assertTrue(os.path.isdir(os.path.join(self.game, "_ChartFixerBackup")))
        self.assertFalse(os.path.exists(os.path.join(self.custom_albums, "_ChartFixerBackup")))

    def test_source_mdms_unchanged_for_non_inplace_plans(self):
        paths = [self.damaged_path(name) for name in DAMAGED_MDM_NAMES]
        hashes = {path: sha256_file(path) for path in paths}
        items = core.scan(paths)
        core.convert(items, "euterpe", game_dir=self.game,
                     custom_albums_dir=self.custom_albums, offline_dir=self.offline)
        core.convert(items, "cam_folder", game_dir=self.game,
                     custom_albums_dir=self.custom_albums, offline_dir=self.offline)
        core.convert(items, "both", game_dir=self.game,
                     custom_albums_dir=self.custom_albums, offline_dir=self.offline, dry_run=True)
        for path, digest in hashes.items():
            self.assertEqual(sha256_file(path), digest, "%s 被意外修改" % path)

    def test_convert_reports_progress(self):
        item = self.scan_one(self.damaged_path(DAMAGED_MDM_NAMES[0]))
        messages = []
        self.convert_one(item, "euterpe", progress=messages.append)
        self.assertTrue(messages)
        self.assertTrue(any("处理" in message for message in messages))
        self.assertTrue(any("转换完成" in message for message in messages))

    def test_progress_callback_exception_is_swallowed(self):
        item = self.scan_one(self.damaged_path(DAMAGED_MDM_NAMES[0]))

        def broken_progress(_message):
            raise RuntimeError("GUI 回调炸了")

        result = self.convert_one(item, "euterpe", progress=broken_progress)
        self.assertTrue(result.ok, result.message)


class SafetyTests(CoreTestCase):
    def test_safe_filename_rules(self):
        self.assertEqual(core._safe_filename("CON"), "_CON")
        self.assertEqual(core._safe_filename("con.txt"), "_con.txt")
        self.assertEqual(core._safe_filename("a<b>c:d\"e/f\\g|h?i*j"), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(core._safe_filename("name. . "), "name")
        self.assertEqual(core._safe_filename(""), "Untitled")
        self.assertEqual(core._safe_filename("  "), "Untitled")
        self.assertEqual(core._safe_filename(".."), "Untitled")
        self.assertLessEqual(len(core._safe_filename("长" * 500)), 120)

    def test_safe_member_relpath_rules(self):
        self.assertIsNone(core._safe_member_relpath("../evil.txt"))
        self.assertIsNone(core._safe_member_relpath(".."))
        self.assertEqual(core._safe_member_relpath("/abs/x.txt"), "abs/x.txt")
        self.assertEqual(core._safe_member_relpath("dir\\CON.txt"), "dir/_CON.txt")
        self.assertEqual(core._safe_member_relpath("a/./b.txt"), "a/b.txt")

    def test_zip_slip_protection(self):
        malicious = make_zip(os.path.join(WORK, "synthetic", "malicious.mdm"), {
            "../evil.txt": b"evil",
            "..\\evil2.txt": b"evil2",
            "/abs_evil.txt": b"abs",
            "CON.txt": b"reserved",
            "nested/../../escape.txt": b"escape",
            "cover.png": b"png",
            "demo.ogg": b"ogg",
            "music.ogg": b"music",
            "map2.bms": b"#BPM 120\r\n#GENRE scene_01\r\n#PLAYLEVEL 3\r\n",
            "info.json": b"{broken json",
        })
        item = self.scan_one(malicious)
        self.assertTrue(item.needs_fix)
        result = self.convert_one(item, "cam_folder")
        self.assertTrue(result.ok, result.message)
        target = result.target
        self.assertTrue(os.path.isdir(target))
        # 合法条目被解压（文件名按规则清洗）
        self.assertTrue(os.path.isfile(os.path.join(target, "abs_evil.txt")))
        self.assertTrue(os.path.isfile(os.path.join(target, "CON.txt")) is False)
        self.assertTrue(os.path.isfile(os.path.join(target, "_CON.txt")))
        # 任何 .mdm 之外/目标之外的文件都不允许出现
        outside_candidates = [
            os.path.join(WORK, "evil.txt"),
            os.path.join(self.custom_albums, "evil.txt"),
            os.path.join(self.game, "evil.txt"),
            os.path.join(self.custom_albums, "escape.txt"),
            os.path.join(WORK, "escape.txt"),
        ]
        for outside in outside_candidates:
            self.assertFalse(os.path.exists(outside), "zip slip 产生了目标外文件：%s" % outside)
        self.assertFalse(os.path.exists(os.path.join(target, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(target, "evil2.txt")))
        # info.json 仍被写成合法 CAM
        info = read_json(os.path.join(target, "info.json"))
        self.assertEqual(core.validate_cam_info(info)[0], [])

    def test_scan_does_not_write_sources(self):
        paths = [self.damaged_path(name) for name in DAMAGED_MDM_NAMES]
        paths.append(self.good_path(GOOD_MDM_NAMES[0]))
        before = {path: sha256_file(path) for path in paths}
        core.scan(paths)
        for path, digest in before.items():
            self.assertEqual(sha256_file(path), digest)


# ----------------------------------------------------------------------------
# 8. 路径检测
# ----------------------------------------------------------------------------


class PathDetectionTests(CoreTestCase):
    def test_find_custom_albums_and_default_offline_dir(self):
        self.assertTrue(core.find_custom_albums(self.game))
        self.assertIsNone(core.find_custom_albums(os.path.join(WORK, "不存在")))
        self.assertIsNone(core.find_custom_albums(None))
        expected = os.path.join(self.game, "Euterpe_Charts", "Offline")
        self.assertEqual(os.path.normcase(core.default_offline_dir(self.game)),
                         os.path.normcase(expected))

        # 兼容 CustomAlbums / custom_albums 等拼写
        for index, spelling in enumerate(("CustomAlbums", "custom_albums", "custom albums")):
            fake = os.path.join(WORK, "fake_game_%d" % index)
            os.makedirs(os.path.join(fake, spelling), exist_ok=True)
            found = core.find_custom_albums(fake)
            self.assertIsNotNone(found)
            self.assertEqual(os.path.basename(found), spelling)

    def test_detect_game_dir_scans_candidates(self):
        fake = os.path.join(WORK, "fake_game")
        os.makedirs(os.path.join(fake, "Custom_Albums"), exist_ok=True)
        self.assertTrue(core._is_game_dir(fake))
        self.assertFalse(core._is_game_dir(os.path.join(WORK, "missing_dir")))

        original = core._candidate_game_dirs
        try:
            core._candidate_game_dirs = lambda: [os.path.join(WORK, "not_here"), fake]
            detected = core.detect_game_dir()
        finally:
            core._candidate_game_dirs = original
        self.assertIsNotNone(detected)
        self.assertEqual(os.path.normcase(detected), os.path.normcase(fake))

        # 本机真实环境：返回 None 或一个真实存在且像游戏的目录
        real = core.detect_game_dir()
        self.assertTrue(real is None or os.path.isdir(real))

    def test_is_game_dir_with_musedash_exe(self):
        fake = os.path.join(WORK, "fake_game2")
        os.makedirs(fake, exist_ok=True)
        write_bytes(os.path.join(fake, "MuseDash.exe"), b"MZ")
        self.assertTrue(core._is_game_dir(fake))


# ----------------------------------------------------------------------------
# 9. 外部只读约束：fixtures 原目录不允许被测试修改
# ----------------------------------------------------------------------------


_FIXTURE_SNAPSHOT = {}


def setUpModule():
    global _FIXTURE_SNAPSHOT
    _FIXTURE_SNAPSHOT = snapshot_tree(FIXTURES)


def tearDownModule():
    current = snapshot_tree(FIXTURES)
    if current != _FIXTURE_SNAPSHOT:
        changed = sorted(
            set(current) | set(_FIXTURE_SNAPSHOT)
        )
        detail = "\n".join(
            "%s: %s -> %s" % (name, _FIXTURE_SNAPSHOT.get(name), current.get(name))
            for name in changed
            if _FIXTURE_SNAPSHOT.get(name) != current.get(name)
        )
        raise AssertionError("tests/fixtures 被修改了！\n" + detail)
    # 清理本次测试的工作区（tests/out 下仅保留其他任务可能使用的文件）
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)