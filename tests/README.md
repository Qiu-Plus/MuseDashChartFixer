# tests 说明

本目录的单元测试依赖 `tests/fixtures/` 下的谱面样本（**未随仓库提供**，因为样本是从社区谱面包中提取的、
体积较大且涉及原谱版权）。测试与仓库内代码本身可以直接阅读/运行模块级用例；涉及 fixtures 的用例会跳过或
报缺文件。

## 期望的 fixtures 目录结构

```
tests/fixtures/
├── Custom_Albums/
│   ├── [Lv 10]MEGAMIX 2.mdm           # 喵斯兔转坏的 .mdm（info.json 为 Euterpe manifest 风格 + BOM + bpm=0）
│   ├── [lv 10]Cherry Pop.mdm
│   ├── [lv 10]Infinity Heaven.mdm
│   ├── [lv 10]memorys.mdm
│   └── folder-mode sample/            # 解压形态的损坏谱面文件夹
│       ├── info.json
│       ├── map2.bms
│       ├── cover.png
│       ├── demo.ogg
│       └── music.ogg
└── good/
    ├── 【LV.9,？】はやくにげなきゃ（得赶快逃才行）.mdm   # 合法 CAM 包
    └── reference_offline_lv9.json                        # 该包经 Euterpe 官方迁移后的 manifest（对照用）
```

没有样本时，可以用任意合法的 CustomAlbums .mdm 复制成上述文件名做基本冒烟；需要精确复现断言时请使用
同结构样本（关键点：`[Lv 10]MEGAMIX 2.mdm` 的 info.json 中 bpm 为 "0"、BMS 里 `#BPM 160`）。

## 运行

```bat
python -m unittest discover -s tests -v
python verify.py        :: 独立端到端验收（同样依赖 fixtures）
```