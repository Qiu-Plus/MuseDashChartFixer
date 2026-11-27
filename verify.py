"""独立验收脚本：对 core.py 做端到端黑盒验收。

用法: python verify.py [--keep]
覆盖：扫描、方案1（就地修 .mdm）、方案1b（解压 CAM 文件夹）、方案2（Euterpe 原生谱）、
官方迁移 reference 逐字段比对、源文件安全（方案1 只改 info.json；方案2 完全不改源）。
"""
import hashlib
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILS = []
PASSES = []

CAM_KEYS = ["name", "author", "bpm", "scene", "sceneEgg", "levelDesigner",
            "levelDesigner1", "levelDesigner2", "levelDesigner3", "levelDesigner4", "levelDesigner5",
            "difficulty1", "difficulty2", "difficulty3", "difficulty4", "difficulty5",
            "hideBmsMode", "hideBmsDifficulty", "hideBmsMessage", "unlockLevel",
            "searchTags", "battleUi"]


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("PASS", name)
    else:
        FAILS.append((name, detail))
        print("FAIL", name, detail)


def json_norm(v):
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, (int, float)):
        return ("num", round(float(v), 9))
    if isinstance(v, str):
        return ("str", v)
    if v is None:
        return ("null",)
    if isinstance(v, list):
        return ("list", [json_norm(x) for x in v])
    if isinstance(v, dict):
        return ("dict", sorted((k, json_norm(x)) for k, x in v.items()))
    return ("other", repr(v))


def diff_json(a, b, path="$"):
    if json_norm(a) == json_norm(b):
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: 仅 reference 有 {b[k]!r}")
            elif k not in b:
                out.append(f"{path}.{k}: 仅产物有 {a[k]!r}")
            else:
                out.extend(diff_json(a[k], b[k], f"{path}.{k}"))
        return out
    return [f"{path}: 产物={a!r} reference={b!r}"]


def sha256(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def zip_snapshot(p):
    with zipfile.ZipFile(p) as z:
        return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}


def scene_ok(s):
    return isinstance(s, str) and s.startswith("scene_") and s[6:].isdigit() and 1 <= int(s[6:]) <= 12


def main():
    keep = "--keep" in sys.argv
    import core

    work = os.path.join(HERE, ".verify_tmp")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    alb = os.path.join(work, "Custom_Albums")
    shutil.copytree(os.path.join(HERE, "tests", "fixtures", "Custom_Albums"), alb)
    good = os.path.join(work, "good")
    shutil.copytree(os.path.join(HERE, "tests", "fixtures", "good"), good)
    offline = os.path.join(work, "Euterpe_Charts", "Offline")
    os.makedirs(offline, exist_ok=True)

    # ---------- 扫描 ----------
    items = core.scan([alb])
    check("扫描损坏谱面 >= 4", len(items) >= 4, f"实际 {len(items)}")
    check("全部 needs_fix", all(i.needs_fix for i in items),
          str([(i.chart_name, i.errors, i.issues) for i in items if not i.needs_fix]))
    check("检测到 BOM 问题", any("BOM" in "".join(i.issues) for i in items))

    mdms = [os.path.join(dp, f) for dp, _, fs in os.walk(alb) for f in fs if f.lower().endswith(".mdm")]
    before = {p: sha256(p) for p in mdms}
    snap_before = {p: zip_snapshot(p) for p in mdms}

    # ---------- 方案1：就地修 .mdm ----------
    res = core.convert(items, "cam", game_dir=work, custom_albums_dir=alb, offline_dir=offline)
    check("方案1 全部成功", all(r.ok for r in res),
          str([(r.item.chart_name, r.message) for r in res if not r.ok]))
    changed = [p for p in mdms if sha256(p) != before[p]]
    check("方案1 改写了 .mdm", len(changed) >= 4, f"改写了 {len(changed)} 个")
    # 除 info.json 外条目不变
    same_other = True
    detail = ""
    for p in mdms:
        snap_after = zip_snapshot(p)
        old = snap_before[p]
        for k, v in old.items():
            if k == "info.json":
                continue
            if snap_after.get(k) != v:
                same_other = False
                detail = f"{p}:{k}"
    check("方案1 仅 info.json 变化", same_other, detail)
    check("方案1 备份已生成", os.path.isdir(os.path.join(work, "_ChartFixerBackup")))

    # MEGAMIX 修复效果
    megamix_mdm = [p for p in mdms if "MEGAMIX" in p][0]
    with zipfile.ZipFile(megamix_mdm) as z:
        raw = z.read("info.json")
        info = json.loads(raw.decode("utf-8-sig"))
    check("方案1 info.json 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
    check("方案1 MEGAMIX bpm 兜底为 160", abs(float(info["bpm"]) - 160) < 1e-6, str(info.get("bpm")))
    check("方案1 CAM 字段齐全", not [k for k in CAM_KEYS if k not in info], str([k for k in CAM_KEYS if k not in info]))
    check("方案1 MEGAMIX difficulty2=10", info.get("difficulty2") == "10", str(info.get("difficulty2")))
    check("方案1 MEGAMIX scene 合法", scene_ok(info.get("scene")), str(info.get("scene")))
    errs, _w = core.validate_cam_info(info)
    check("方案1 产物通过 validate_cam_info", not errs, str(errs))
    # 所有修复后的 .mdm 都合法
    all_cam_ok = True
    for p in mdms:
        with zipfile.ZipFile(p) as z:
            d = json.loads(z.read("info.json").decode("utf-8-sig"))
        e, _ = core.validate_cam_info(d)
        if e:
            all_cam_ok = False
            print("  非法:", os.path.basename(p), e)
    check("方案1 全部 .mdm info 合法", all_cam_ok)
    mid = {p: sha256(p) for p in mdms}

    # ---------- 方案1b：解压为 CAM 文件夹 ----------
    res1b = core.convert(items, "cam_folder", game_dir=work, custom_albums_dir=alb, offline_dir=offline)
    check("方案1b 全部成功", all(r.ok for r in res1b),
          str([(r.item.chart_name, r.message) for r in res1b if not r.ok]))
    ok_folders = 0
    for r in res1b:
        if r.ok and r.target and os.path.isdir(r.target):
            ip = os.path.join(r.target, "info.json")
            if os.path.isfile(ip):
                try:
                    e, _ = core.validate_cam_info(json.load(open(ip, encoding="utf-8")))
                    if not e:
                        ok_folders += 1
                except Exception as exc:
                    print("  解析失败", ip, exc)
    check("方案1b 生成合法 CAM 文件夹 >= 4", ok_folders >= 4, f"合法 {ok_folders}")

    # ---------- 方案2：Euterpe 离线谱 ----------
    res2 = core.convert(items, "euterpe", game_dir=work, custom_albums_dir=alb, offline_dir=offline)
    check("方案2 全部成功", all(r.ok for r in res2),
          str([(r.item.chart_name, r.message) for r in res2 if not r.ok]))
    check("方案2 未修改源 .mdm", all(sha256(p) == mid[p] for p in mdms))
    for r in res2:
        if not r.ok or not r.target:
            continue
        tag = r.item.chart_name[:18]
        epk = os.path.join(r.target, "manifest.epk")
        if not os.path.isfile(epk):
            check(f"方案2[{tag}] manifest.epk 存在", False, r.target)
            continue
        m = core.msgpack_decode(open(epk, "rb").read())
        check(f"方案2[{tag}] 顶层结构", m.get("schema") == 1 and m.get("cid") is None
              and isinstance(m.get("meta"), dict) and isinstance(m.get("files"), dict), str(m)[:160])
        meta = m.get("meta", {})
        check(f"方案2[{tag}] bpm>0", float(meta.get("bpm") or 0) > 0, str(meta.get("bpm")))
        check(f"方案2[{tag}] scene 合法", scene_ok(meta.get("scene")), str(meta.get("scene")))
        bad = [k for k, v in m["files"].items()
               if not os.path.isfile(os.path.join(r.target, k)) or os.path.getsize(os.path.join(r.target, k)) != v.get("size")]
        check(f"方案2[{tag}] files 尺寸一致", not bad, str(bad))
        maps = set(meta.get("maps", {}))
        bms = {os.path.splitext(k)[0] for k in m["files"] if k.lower().endswith(".bms")}
        check(f"方案2[{tag}] maps 与 bms 一致", maps == bms or maps <= bms, f"maps={maps} bms={bms}")

    # ---------- 官方迁移 reference 比对 ----------
    good_dir = good
    good_items = core.scan([good_dir])
    check("扫描官方合法样本 >= 2", len(good_items) >= 2, str(len(good_items)))
    lv9 = [i for i in good_items if "はやくにげなきゃ" in i.chart_name or "はやくにげなきゃ" in i.path]
    check("找到 LV9 官方样本", bool(lv9), str([i.chart_name for i in good_items]))
    if lv9:
        core.convert(lv9[:1], "euterpe", game_dir=work, custom_albums_dir=None, offline_dir=offline)
        ref = json.load(open(os.path.join(good_dir, "reference_offline_lv9.json"), encoding="utf-8"))
        targets = [d for d in os.listdir(offline) if os.path.isdir(os.path.join(offline, d))]
        # 找到与 reference files 匹配的那个新目录
        matched = None
        for d in targets:
            ep = os.path.join(offline, d, "manifest.epk")
            if not os.path.isfile(ep):
                continue
            m = core.msgpack_decode(open(ep, "rb").read())
            if set(m.get("files", {})) == set(ref.get("files", {})):
                matched = m
                break
        check("方案2 官方样本产物可定位", matched is not None)
        if matched is not None:
            diffs = diff_json(matched, ref)
            check("与官方迁移 reference 完全一致", not diffs, "\n  ".join(diffs[:20]))

    print("\n===== %d passed, %d failed =====" % (len(PASSES), len(FAILS)))
    for n, d in FAILS:
        print("FAILED:", n, d)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())