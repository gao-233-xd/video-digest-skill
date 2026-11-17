# video-digest-skill — B站视频速览工具

[English](README.en.md) | 中文

把一个 B 站链接变成一份「可速读的资料包」：**带时间戳的文字转写 + 关键画面截图 + AI 总结**。
不再需要从头到尾看视频。支持分P视频、b23.tv 短链、官方AI字幕优先 + 本地 Whisper 兜底。

> 配合 ZCode / Claude 等 AI Agent 使用体验最佳：直接在对话里丢链接，Agent 会自动跑脚本、
> 读产物、把视频内容总结成 `summary.md` 给你（Skill 定义见 [SKILL.md](SKILL.md)）。
> 纯手动命令行使用也完全没问题。

## 效果预览

处理完一个视频，你会得到：

```
notes/
└── 标题-UP主-20260604/
    ├── info.json         # 标题/UP主/简介/分P清单
    ├── cover.jpg         # 封面
    ├── transcript.md     # 带 [mm:ss] 时间戳的转写全文（主力阅读文件）
    ├── transcript.srt    # 同内容字幕文件（导入播放器可对照回看）
    ├── summary.md        # AI 深度总结（配合 Agent 生成）
    └── frames/           # 关键帧截图，文件名即时间点: 00-05-32.jpg
```

## 快速开始

```bash
# 0. 安装依赖（需要系统已装 ffmpeg）
pip install -r requirements.txt
cp config.example.ini config.ini   # 编辑 config.ini，填入你的 SESSDATA（强烈建议）

# 1. 只看视频信息和分P清单（不下载）
python video_digest_skill.py info https://www.bilibili.com/video/BVxxxxxx

# 2. 处理单P视频（转写 + 截帧一次完成）
python video_digest_skill.py run https://www.bilibili.com/video/BVxxxxxx

# 3. 分P视频：先 info 看清单，再指定处理哪几P
python video_digest_skill.py run https://www.bilibili.com/video/BVxxxxxx --pages 1,3-5
python video_digest_skill.py run https://www.bilibili.com/video/BVxxxxxx --pages all

# 4. URL 带 ?p=2 时只处理第2P
python video_digest_skill.py run "https://www.bilibili.com/video/BVxxxxxx?p=2"
```

支持输入：完整URL（可带 `?p=N`）、`b23.tv` 短链、裸 BV 号、av 号。

## 工作原理（一图流）

```
链接 ──► 解析BV号(b23.tv自动跟随跳转) ──► 拉取视频信息/分P清单
                                              │
                                    每个分P走转写流水线:
                                              │
              ┌───────────────────────────────┴──────────────────────────┐
              │ 视频 >10分钟?                                             │
              │   ├─ 是 → 抓B站官方AI/CC字幕(需SESSDATA)                  │
              │   │    ├─ 时长覆盖校验(防换源残留字幕)                     │
              │   │    ├─ 内容抽检: 3个30s窗口Whisper快转比对相似度        │
              │   │    └─ 可信 → 直接用 (秒级完成!)                        │
              │   └─ 否/不可信 → 下载音频 → faster-whisper 本地转写        │
              │         降档链: medium/GPU → small/GPU → small/CPU         │
              │         VAD人声过滤 → 无语音时自动关VAD重试                │
              └───────────────────────────────┬──────────────────────────┘
                                              │
                                    ffmpeg 场景检测截帧
                                      ├─ 感知哈希(pHash)去重
                                      ├─ 相邻2秒内去重
                                      └─ 静止画面按间隔兜底补帧
```

技术细节见 [docs/转写流程详解.md](docs/转写流程详解.md)。

## 为什么配置 SESSDATA 收益最大

| | 不配置 | 配置 |
|---|---|---|
| 官方AI字幕 | 拿不到 | 命中则秒级完成 |
| 本地转写 | 全部视频走Whisper（慢） | 只有无字幕的视频走 |
| 截图画质 | 最高480P（B站未登录限制） | 最高1080P |

获取方法见 `config.example.ini` 内注释。**SESSDATA 只存在本地 config.ini，不参与任何上传**。

## 配合 AI Agent 使用（推荐）

本仓库同时是一份 Agent Skill（[SKILL.md](SKILL.md)）。在 ZCode 中把它加入 skills 目录后，
对话里直接丢链接即可，Agent 会：

1. `info` 先看清单（多P会先跟你确认）
2. `run` 后台跑转写+截帧（含并行纪律、静默期判断等实战经验）
3. 通读 transcript.md + 关键帧
4. 写出 1500~5000 字结构化深度总结 `summary.md`
5. 按你的要求归档到资料目录

## 转写速度参考（RTX 3050 显卡）

| 模型 | 1小时视频大约耗时 |
|---|---|
| 有官方AI字幕 | 秒级 |
| medium (GPU) | 10~15 分钟 |
| small (GPU) | 5~8 分钟 |
| small (CPU兜底) | 40~60 分钟 |

## 常用选项

```bash
--no-frames          # 只转写，不截帧（播客/纯talk类视频推荐）
--no-transcript      # 只截帧，不转写
--model small        # 临时用小模型（更快）
--force              # 忽略已有结果全部重新生成（默认增量：已处理的P自动跳过）
--scene-threshold    # 场景检测阈值 0~1，越小截帧越多
```

## 常见问题

- **首次运行很慢**：在下载 Whisper 模型文件（medium 约1.5GB），只发生一次。已默认走国内镜像 hf-mirror.com（并禁用了镜像站不支持的 Xet 协议）。
- **提示 CUDA 相关错误**：自动降级到下一档配置继续跑，只是慢；也可 `--model small` 减轻显存压力。
- **macOS / Linux**：Windows + NVIDIA 显卡体验最佳；macOS/Linux 可用 CPU 转写，`requirements.txt` 会自动跳过 CUDA 库包。需要系统已安装 ffmpeg 并在 PATH 中。
- **"官方字幕弃用"提示**：B站换源后可能残留旧视频的AI字幕，工具用时长校验+内容抽检自动识别并弃用，属正常保护。
- **付费/充电专属视频**：无法处理，会明确报错。
- **不要并行跑两个转写**：小显存显卡（≤4GB）同时跑两个 Whisper 进程会触发 WDDM 显存溢出，两个任务互相拖慢数倍。串行处理。

## 免责声明

- 仅供个人学习研究，请尊重UP主版权，勿传播转写内容。
- 请遵守B站用户协议，合理控制请求频率（工具已内置各种保守超时与单请求串行逻辑）。
- SESSDATA 等凭据等同于登录态，请勿分享自己的 config.ini。

## License

MIT
