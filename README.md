# Muse Dash 谱面急救箱（MuseDashChartFixer）

修复被**喵斯兔（MuseDashTOOL / MDT）**转坏的 Euterpe 自制谱面，让 Euterpe v1 重新能玩。

- 目标文件：Custom_Albums 里的 `.mdm`（本质是 zip），或已经解压的旧版 CAM 谱面文件夹
- 典型症状：info.json 变成 Euterpe manifest 结构、带 UTF-8 BOM、bpm=0、缺少 CAM 字段；
  老版 Euterpe（基于 CustomAlbums）勉强能读，新版 Euterpe v1 读不了，谱面消失/不能玩
- 提供两种修复方案（界面里二选一）：
  1. **方案 1：就地修复 .mdm 里的 info.json（推荐，默认）**
     源 .mdm 位置/文件名不变；只把包内 info.json 改成合法的 CustomAlbums 格式；
     改动前自动备份原 info.json。Euterpe 启动时会把 Custom_Albums 里的旧版 CAM 谱面
     识别为「Euterpe 离线」谱面。
  2. **方案 2：转换为 Euterpe 原生离线谱**
     生成 `Euterpe_Charts/Offline/<谱面名>/` 文件夹（cover/demo/map/music/manifest.epk），
     完全使用 Euterpe 自身格式；不改动源 .mdm。适合 Euterpe 版本较新、或方案 1 不生效时。

> 原理依据：Euterpe 1.1.x 会把 Custom_Albums 里的旧版 CAM 谱面（.mdm 以及含 info.json 的文件夹）
> 识别为「Euterpe 离线」谱面；但它的解析很严格（BOM、bpm、scene、难度槽位等），喵斯兔生成的
> info.json 不合法，所以被跳过。详见 [docs/技术说明.md](docs/技术说明.md)。

## 使用方法（图形界面）

0. 解压后**双击「启动（双击我）.bat」**（推荐，启动文件夹版）；也可以直接双击 `portable\MuseDashChartFixer.exe`。
   若双击没反应：先等 10~20 秒，或运行「启动诊断.bat」；Windows 提示「已保护你的电脑」时点「更多信息 → 仍要运行」。
2. 选择游戏目录（例如 `D:\steam\steamapps\common\Muse Dash`），程序会自动定位 `Custom_Albums`
3. 点「扫描」，列表会显示每个损坏谱面、作者与问题摘要
4. 选择方案（推荐先用方案 1；若谱面仍不显示，再改用方案 2）
5. 可勾选「仅预览，不写入文件」先看结果
6. **先关闭游戏**，点「开始转换」，完成后重启游戏

转换完成后：
- 方案 1：直接重启游戏，Euterpe 会在启动时把修复后的 .mdm 导入「Euterpe 离线」曲包
- 方案 2：Euterpe 支持热重载，通常不用重启即可在离线谱面里看到（没有的话重启一次）

## 安全性

- 方案 2 只新增文件，**绝不修改/删除**源 .mdm
- 方案 1 只改写 .mdm 里的 info.json；改写前会把原始 info.json 备份到
  `<游戏目录>\_ChartFixerBackup\<谱面名>\info.json`（附带 record.json 记录原路径、哈希、时间）
- 重复运行是安全的：已合法/已转换的谱面会被跳过
- 程序不联网、不上传任何数据

## 备份与恢复

- 备份目录：`<游戏目录>\_ChartFixerBackup\`
- 需要还原时：把备份里的 `info.json` 重新写回对应 .mdm 包（用任意 zip 工具替换包内 info.json 即可），
  或直接用原始文件覆盖 .mdm
- 建议转换前先把整个 `Custom_Albums` 复制一份（1000 多张谱时也建议保留原始 .mdm 备份）

## 命令行模式（可选）

`dist\MuseDashChartFixerCLI.exe`（或 `python main.py --cli ...`）：

```bat
MuseDashChartFixerCLI.exe --cli --game-dir "D:\steam\steamapps\common\Muse Dash" --plan cam
MuseDashChartFixerCLI.exe --cli --game-dir "D:\steam\steamapps\common\Muse Dash" --plan cam --dry-run
MuseDashChartFixerCLI.exe --cli --scan "D:\...\Custom_Albums" --plan euterpe --json
```

## 从源码运行 / 自行打包

```bat
python main.py                            :: 图形界面
python -m unittest discover -s tests -v   :: 运行 52 项单元测试
python verify.py                          :: 独立集成验收（含与官方迁移结果逐字段比对）
build_exe.bat                             :: 打包单文件版 + 命令行版 + 文件夹版（dist_portable）
```

依赖：Python 3.10+（仅标准库，tkinter 随 Python 安装）。打包需 `pip install pyinstaller`。

## 常见问题

**Q：转换后游戏里还是看不到？**
A：确认使用 Euterpe v1（新版自制谱系统，建议最新 1.1.x）；确认游戏目录正确；方案 1 需要重启游戏；
   若谱面含 map4.bms/map5.bms，Euterpe 对 .mdm 只导入前三个难度，建议改用方案 2。

**Q：为什么会丢信息？**
A：喵斯兔把 Euterpe 谱面转成 .mdm 时只写入了部分字段（sceneEgg、部分难度、部分谱师信息等会丢），
   本工具能恢复大部分（名称、作者、BPM、场景、难度、谱师、隐藏谱设置）；原始就丢掉的字段无法凭空恢复。

**Q：双击没反应/启动不了？**
A：单文件版首次启动要自解压，可能慢 5~20 秒；被杀毒软件拦截时请加信任；推荐直接用「启动（双击我）.bat」
   或 `portable` 文件夹版。仍不行就运行「启动诊断.bat」并把输出发回。

**Q：会不会影响正版游戏/封号？**
A：工具只处理本地自制谱文件，不修改游戏本体、不联网、不碰排行/存档。

**Q：1000 多张谱要处理多久？**
A：扫描只需读 info.json，通常几秒到几十秒；方案 1 需要重写每个 .mdm 包（复制文件），
   耗时取决于磁盘速度；方案 2 需要复制音频/封面，耗时与谱面总体积相关。