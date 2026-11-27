# MuseDashChartFixer 内部规格（SPEC）

## 0. 背景（已确证的事实）
- 喵斯兔（MuseDashTOOL，英文缩写 MDT，仓库 https://github.com/Suzimo506/MuseDashTOOL）会把 Euterpe 的谱面
  （Euterpe_Charts/Online、Offline 里的谱）转成 .mdm（本质是 zip），放到游戏的 Custom_Albums 里，
  但生成的 info.json 是**Euterpe manifest 风格的 JSON**（带 schema/cid/meta/files），且带 UTF-8 BOM、
  缺少 CustomAlbums 需要的顶层字段、bpm 可能为 0，因此：
  - Euterpe v0（基于 CustomAlbums 的老模组）还能勉强识别；
  - Euterpe v1（自研谱面系统，从游戏目录 Euterpe_Charts 读取 Online/Offline）无法读取。
- Euterpe v1.1.x 原生核心仍然支持 **Custom_Albums 下的“旧版 CAM 谱面文件夹”**（文件夹里有 info.json 即
  载入为离线谱面），但**不读取 .mdm 压缩包**（见 Euterpe 更新日志字符串：
  “Custom_Albums 里含 info.json 的旧版 CAM 谱面文件夹也会载入为离线谱面”；“.mdm 谱面不会载入”）。
- 因此两条修复路线：
  1. **方案 1（CAM 修复）**：把每个损坏的 .mdm 解压成 Custom_Albums/<谱面名>/ 文件夹，并把 info.json
     重写成**合法的 CustomAlbums info.json**（标准 CAM 格式）。Euterpe v1 会把它当作离线谱面载入。
     源 .mdm 文件保持原位不动。
  2. **方案 2（Euterpe 转换）**：把每个损坏的 .mdm 转成 Euterpe_Charts/Offline/<谱面名>/ 文件夹：
     cover.png / demo.ogg / mapN.bms / music.ogg / manifest.epk（MessagePack）。源文件同样不动。
  3. 方案 3：GUI 里让用户勾选方案 1 或 2（或两者都做）。

## 1. 已确证的格式样本
### 1.1 损坏的 info.json（喵斯兔产物，示例 MEGAMIX）
- 文件以 UTF-8 BOM (EF BB BF) 开头，JSON 结构为：
  { "schema": 1, "cid": null, "meta": { name,name_romanized,author,description,safe_for_streamer,
    bpm,bpm_min,bpm_max,scene,scene_egg,background_video_opacity,search_keywords,
    maps: { "map2": { "rating": "10", "charters": ["鱼"], "predicted_rating": null } },
    hide_mode,hide_rating_override,hide_message,cover_dominant_color,uploader,created_at,updated_at },
    "files": { "cover.png": {"version":1,"size":...}, "demo.ogg":..., "map2.bms":..., "music.ogg":... },
    "name": "...", "author": "...", "bpm": "0", "difficulty2": "10",
    "levelDesigner2": "...", "charter": "...", "levelDesigner": "..." }
- 已证实的问题：BOM；bpm 可能为 0；顶层缺 scene/sceneEgg/difficulty1/3/4/5/levelDesigner1/3/4/5/
  hideBmsMode/hideBmsDifficulty/hideBmsMessage/unlockLevel/searchTags/battleUi；
  info.json 只有 Euterpe manifest 结构。

### 1.2 合法的 CustomAlbums info.json（已知可用，来自 MDT 制谱器/社区 .mdm）
{
  "name": "...", "author": "...", "bpm": "172", "scene": "scene_03", "sceneEgg": "none",
  "levelDesigner": "AP的是给", "levelDesigner1".."levelDesigner5": 同一名字,
  "difficulty1": "0", "difficulty2": "9", "difficulty3": "0", "difficulty4": "?", "difficulty5": "0",
  "hideBmsMode": "CLICK", "hideBmsDifficulty": "0", "hideBmsMessage": "", "unlockLevel": "0",
  "searchTags": [], "battleUi": "default" }
- 无 BOM；所有难度、谱师都是**字符串**；scene 必须是 scene_01..scene_12；
  bpm 是十进制字符串；difficultyN 为 "0" 表示该槽位没有；值必须与包内 mapN.bms 是否存在一致。

### 1.3 Euterpe manifest.epk（MessagePack；真实离线谱样本解码后）
{ schema:1, cid:null, meta:{ name, name_romanized:"", author, description:null,
  safe_for_streamer:false, bpm:161, bpm_min:null, bpm_max:null,
  scene:"scene_05", scene_egg:null, background_video_opacity:null, search_keywords:[],
  maps:{ map2:{ rating:"8.2", charters:["巯_Q1uP7us"], predicted_rating:null } },
  hide_mode:"CLICK", hide_rating_override:"0", hide_message:"",
  cover_dominant_color:null, uploader:null, created_at:null, updated_at:null },
  files:{ "cover.png":{"version":1,"size":312397}, "demo.ogg":..., "map2.bms":..., "music.ogg":... } }
- 离线谱的 files 里**没有** manifest.epk 自身条目（线上谱才有，version 3）。
- 真实样本证明从 CAM → Euterpe 的 App 迁移映射：
  name→meta.name，author→meta.author，bpm→meta.bpm(number)，scene→meta.scene，
  levelDesignerN→meta.maps.mapN.charters=[designer]，difficultyN→mapN.rating(字符串)，
  hideBmsMode→hide_mode，hideBmsDifficulty→hide_rating_override，hideBmsMessage→hide_message，
  sceneEgg→meta.scene_egg(null)（App 会丢弃 sceneEgg，属于已知信息损失）。
  files 用实际文件名+真实字节数，version=1。

### 1.4 BMS 兜底信息
mapN.bms 是文本（可能带 BOM），头部行形如：
  #BPM 160 / #GENRE scene_03 / #PLAYLEVEL 9 / #TITLE ... / #ARTIST ...
- bpm 缺失/非法时用 #BPM 兜底；scene 无效时用 #GENRE 兜底（若形如 scene_XX）；
  难度缺失时用 #PLAYLEVEL 兜底（可为小数如 8.2）。

## 2. 转换规则
### 2.1 统一中间模型 manifest
to_manifest(raw_info: dict, bms_texts: dict[str,str], file_sizes: dict[str,int]) -> dict
- 若 raw_info 有 dict 型 meta/schema：以它为基础，补齐/修正所有字段；
- 否则按 CAM 字段构造；
- 修正规则：
  * name: meta.name 或顶层 name 或（最后）文件名；空则 "Untitled"
  * author: 同上，空则 "Unknown"
  * bpm: 取 meta.bpm 或顶层 bpm，转正数；非法→BMS #BPM→120。整数输出 int，小数输出 float
  * scene: 取 meta.scene / 顶层 scene；必须匹配 ^scene_(0[1-9]|1[0-2])$，否则 BMS #GENRE，再否则 "scene_01"
  * maps: 只保留包内存在 mapN.bms 的槽位；rating 取 meta.maps.mapN.rating / 顶层 difficultyN /
    BMS #PLAYLEVEL / "1"（保持字符串）；charters 取 meta.maps.mapN.charters 里非空字符串，
    否则 [levelDesignerN / levelDesigner / charter / "Unknown"]；predicted_rating:null
  * hide_mode: meta.hide_mode 或顶层 hideBmsMode；只接受 CLICK/PRESS/TOGGLE（规范化为大写），否则 "CLICK"
  * hide_rating_override: 顶层 hideBmsDifficulty 或 meta.hide_rating_override；统一转字符串，默认 "0"
  * hide_message: 顶层 hideBmsMessage 或 meta.hide_message；字符串，默认 ""
  * search_keywords: 顶层 searchTags 或 meta.search_keywords；缺省 []
  * 其余 meta 字段按 1.3 格式补齐（name_romanized "", description null, safe_for_streamer false,
    bpm_min/bpm_max/background_video_opacity/cover_dominant_color/uploader/created_at/updated_at = null）
  * schema=1, cid=null（离线）
- files: 对包内实际存在的每个文件（cover.png/cover.webp/demo.ogg/music.ogg/mapN.bms/mapN.json/
  mapN.talk/video*）写 {"version":1,"size":<真实字节数>}；不含 manifest.epk。

### 2.2 CAM info.json 生成
build_cam_info(manifest: dict) -> dict（字段顺序固定）
name, author, bpm(str), scene, sceneEgg("none"), levelDesigner,
levelDesigner1..5, difficulty1..5, hideBmsMode, hideBmsDifficulty, hideBmsMessage,
unlockLevel("0"), searchTags, battleUi("default")
- difficultyN：若 manifest.maps 有 mapN → 该 rating；否则 "0"
- levelDesignerN：mapN.charters[0] 若有，否则 levelDesigner
- bpm 字符串：int → "160"；float → 去掉多余 0（如 94.1）
- 序列化：UTF-8 无 BOM、indent=2、ensure_ascii=False、换行 CRLF（与已知可用样本一致）。

### 2.3 方案 1 执行（CAM 修复）
输入 .mdm：
  - 目标文件夹 = <Custom_Albums>/<mdm 去扩展名>（非法字符替换 _，去尾部空格和点，避开 Windows 保留名）
  - 目标已存在 → 跳过并在结果里注明（不覆盖）
  - 安全解压全部文件到目标文件夹（zip slip 防护）
  - 覆盖写入合法 info.json
  - 源 .mdm 保持原样
输入文件夹（已经是解压形态）：
  - 若 info.json 损坏：把原 info.json 备份为 info.json.bak.<时间戳>，写入合法 info.json
  - 其他文件不动
所有写操作前支持 dry_run=True 仅报告。

### 2.4 方案 2 执行（Euterpe 转换）
- 目标 = <Euterpe_Charts>/Offline/<谱面名>；重名时追加 " (2)"、" (3)"…
- 写入 cover.png/demo.ogg/music.ogg/mapN.bms 以及包里存在的 mapN.json/mapN.talk 等附属文件
- 写入 manifest.epk = msgpack(manifest)（自研编码器，见 §3）
- 源 .mdm/源文件夹保持原样；不写 manifest.epk 的 files 条目
- 缺 music.ogg 时仍生成但结果标记警告

## 3. MessagePack 编码器（stdlib 实现）
encode(obj) -> bytes：dict(fixmap/map16)、list(fixarray/array16)、str(utf-8; fixstr/str8/str16/str32)、
int(正数 positive fixint/uint8/16/32/64，负数 int8/16/32/64)、float64(0xcb)、None(0xc0)、bool(0xc2/0xc3)。
解码器（用于自检）同规格实现。只用于 manifest.epk。

## 4. 检测与“损坏”判定
scan 时对每个候选项产出 ScanItem：
- path、kind("mdm"/"folder")、chart_name、author、issues[]
- issues 至少覆盖：JSON 解析失败；带 BOM；Euterpe manifest 结构（schema/meta/files）；
  缺少 CAM 顶层字段；bpm 非正；scene 非法；难度槽与包内 bms 不一致；
  缺 info.json；缺 music.ogg（warning）；manifest.files 与包内文件不符（warning）
- valid = 无“严重”issues（BOM 单独算轻微，但如果同时缺字段则算损坏；实现上：只要 needs_fix 为真就列出）

## 5. API 契约（core.py，GUI/CLI 只用这些）
@dataclass ScanItem: path:str; kind:str; chart_name:str; author:str; issues:list[str];
    errors:list[str]; warnings:list[str]; needs_fix:bool; payload:dict|None
@dataclass ActionResult: item:ScanItem; ok:bool; target:str|None; message:str
def detect_game_dir() -> str|None            # 从 Steam 库 + 常见路径找 Muse Dash
def find_custom_albums(game_dir:str) -> str|None   # Custom_Albums/custom_albums/CustomAlbums
def scan(paths:list[str], recursive:bool=True, progress:Callable[[str],None]|None=None) -> list[ScanItem]
def validate_cam_info(info:dict) -> tuple[list[str], list[str]]  # (errors, warnings)
def convert(items:list[ScanItem], plan:str, game_dir:str|None, custom_albums_dir:str|None,
            offline_dir:str|None, dry_run:bool=False,
            progress:Callable[[str],None]|None=None) -> list[ActionResult]
    # plan ∈ {"cam","euterpe","both"}
def default_offline_dir(game_dir:str) -> str  # <game>/Euterpe_Charts/Offline
def msgpack_encode(obj) / msgpack_decode(data)（内部/测试用，可导出）
所有函数不得写源文件（除 §2.3 明确允许的 info.json 覆盖 + .bak）。

## 6. GUI 要求（gui.py，tkinter/ttk，纯中文）
- 标题：Muse Dash 谱面急救箱（喵斯兔损坏谱面修复）
- 顶部：游戏目录输入框 + [自动检测] + [浏览…]；自动定位 Custom_Albums 并显示
- [扫描] 按钮 → Treeview 列出：谱面名 | 作者 | 问题摘要 | 状态
- 方案选择（单选）：① 修复为 CAM 文件夹（推荐：EU v1 直接识别） ② 转换为 Euterpe 离线谱
  ③ 两者都做
- 复选框：[仅预览，不写入文件]
- [开始转换] → 后台线程执行，进度条+日志文本框；完成后弹窗统计（成功/跳过/失败）
- [打开 Custom_Albums] [打开 Euterpe_Charts/Offline] [保存日志]
- 所有长操作不能卡界面（threading，UI 更新用 after()）

## 7. 测试（tests/test_core.py，unittest，stdlib）
- 用 tests/fixtures 的样本副本（禁止改动 关于转换谱面的问题 原目录）
- 扫描 4 个损坏样本 → 4 个 needs_fix，issues 含 BOM/缺字段
- 方案1：生成文件夹 + 合法 CAM info.json；MEGAMIX 的 bpm 必须从 BMS 兜底为 160（不是 0）；
  再用 validate_cam_info 校验全部通过
- 方案2：生成 Euterpe 文件夹；manifest.epk 可被 msgpack_decode 解回；schema=1、cid=null；
  maps 的键与包内 mapN.bms 一致；files 的 size 与磁盘文件一致；meta.scene 合法、bpm>0
- 回归：用 tests/fixtures/good 里合法的 .mdm 走方案2，结构与真实 Euterpe 离线样本同构
- 幂等/安全：重复执行不覆盖已存在目标；源 .mdm 哈希不变


## 8. 修订 R2（重要，实现以本节为准）
已从 Euterpe 1.1.2 内置更新日志（Euterpe.Hot.dll 内嵌字符串）确认当前行为：
- 旧版 CAM 谱面（.mdm）会在游戏启动时自动导入「Euterpe 离线」曲包，可以直接游玩；
- Custom_Albums 里含 info.json 的 CAM 谱面“文件夹”也会载入为离线谱面；
- .mdm 只导入前三个难度；没有音乐(music.ogg)的谱面会被跳过；
- 当前版本代码数据根目录常量里仍含 Custom_Albums，info.json 解析校验很严格
  （BOM、bpm 非正、scene 非 scene_01..12、难度槽与包内 mapN.bms 不一致等都会失败）。

因此方案 1 的**默认形态改为“就地修复 .mdm 里的 info.json”**（不改变 .mdm 位置和文件名）：
1) 打开 .mdm（zip），读出原始 info.json 字节，保存到备份目录（见下）；
2) 用规范 CAM info.json 覆盖包内 info.json 条目，其余条目字节与顺序保持不变，
   压缩方式沿用原条目，先写临时文件再 os.replace 原子替换；
3) 若源是“文件夹”形态：备份原 info.json 后直接覆盖写入；
4) 额外提供 plan="cam_folder"（方案 1b）：把 .mdm 安全解压成
   Custom_Albums/<谱面名>/ 文件夹并写入规范 info.json（用于 .mdm 导入不生效时的退路）。
备份目录固定为 <游戏目录>/_ChartFixerBackup/<来源名>/info.json（附带 record.json：
原路径、sha256、时间）。备份目录在 Custom_Albums 之外，避免被 EU 当成谱面扫描。
若 .mdm 内根本没有 info.json：用 BMS 头/包内容重建后写入（记为“补建 info.json”）。
若包内含 map4.bms/map5.bms：在 ScanItem.warnings 里提示“方案 1 的 .mdm 导入可能只保留前三个难度，
更推荐方案 2”，但仍允许执行。

## 9. convert 的 plan 取值（R2 最终）
plan ∈ {"cam", "cam_folder", "euterpe", "both"}：
- "cam"：就地修 .mdm / 文件夹 info.json（默认）
- "cam_folder"：解压为 CAM 文件夹
- "euterpe"：方案 2（Euterpe_Charts/Offline/<名>/ + manifest.epk）
- "both"：cam + euterpe（GUI 需提示可能重复；默认不选）
GUI 方案单选建议 4 项：方案1 修复 .mdm（推荐）/ 方案1b 解压为 CAM 文件夹 / 方案2 Euterpe 离线谱 / 两者都做。
