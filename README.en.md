# bbdigest — Bilibili Video Digest

Turn a Bilibili link into a "fast-reading resource pack": **timestamped transcript + keyframe screenshots + AI summary**.
No need to watch the whole video. Supports multi-part videos, b23.tv short links, and prefers official AI subtitles with a local Whisper fallback.

> Works best with AI agents (ZCode / Claude, etc.): drop a link in the chat and the agent runs the script,
> reads the output, and writes a deep `summary.md` for you (skill definition in [SKILL.md](SKILL.md)).
> Plain command-line usage works fine too.

[中文说明](README.md) | English

## What you get

After processing a video:

```
notes/
└── Title-Uploader-20260604/
    ├── info.json         # title / uploader / description / page list
    ├── cover.jpg         # cover image
    ├── transcript.md     # full transcript with [mm:ss] timestamps (main reading file)
    ├── transcript.srt    # same content as subtitles (import into a player to follow along)
    ├── summary.md        # deep AI summary (generated with an agent)
    └── frames/           # keyframe screenshots, filename = timestamp: 00-05-32.jpg
```

## Quick start

```bash
# 0. Install dependencies (ffmpeg must already be on PATH)
pip install -r requirements.txt
cp config.example.ini config.ini   # edit config.ini and fill in SESSDATA (strongly recommended)

# 1. Inspect video info and the page list (no download)
python bbdigest.py info https://www.bilibili.com/video/BVxxxxxx

# 2. Process a single-part video (transcript + keyframes in one go)
python bbdigest.py run https://www.bilibili.com/video/BVxxxxxx

# 3. Multi-part video: run info first, then choose which parts
python bbdigest.py run https://www.bilibili.com/video/BVxxxxxx --pages 1,3-5
python bbdigest.py run https://www.bilibili.com/video/BVxxxxxx --pages all

# 4. A URL with ?p=2 processes only part 2
python bbdigest.py run "https://www.bilibili.com/video/BVxxxxxx?p=2"
```

Supported inputs: full URLs (optionally with `?p=N`), `b23.tv` short links, bare BV IDs, and av IDs.

## How it works

```
Link ──► Parse BV ID (b23.tv redirect followed) ──► Fetch video info / page list
                                              │
                                    For each part:
                                              │
              ┌───────────────────────────────┴──────────────────────────┐
              │ Video longer than 10 min?                                │
              │   ├─ Yes → Fetch official AI/CC subtitles (SESSDATA)     │
              │   │    ├─ Duration coverage check (reject stale subs)    │
              │   │    ├─ Spot check: 3x30s windows vs Whisper (sim.)    │
              │   │    └─ Trusted → use directly (done in seconds!)      │
              │   └─ No / untrusted → Download audio → faster-whisper    │
              │         Fallback: medium/GPU → small/GPU → small/CPU     │
              │         VAD filter → auto-retry without VAD if silent    │
              └───────────────────────────────┬──────────────────────────┘
                                              │
                                    ffmpeg scene detection
                                      ├─ Perceptual hash (pHash) dedupe
                                      ├─ Drop frames within 2s of each other
                                      └─ Fallback interval for static scenes
```

Technical deep-dive (Chinese): [docs/转写流程详解.md](docs/转写流程详解.md).

## Why SESSDATA is the highest-value setting

| | Not configured | Configured |
|---|---|---|
| Official AI subtitles | Unavailable | Instant when available |
| Local transcription | Every video goes through Whisper (slow) | Only videos without subtitles do |
| Screenshot quality | Up to 480P (logged-out limit) | Up to 1080P |

See the comments in `config.example.ini` for how to obtain it. **SESSDATA is stored only in your local config.ini and is never uploaded.**

## Using it with an AI agent (recommended)

This repository is also an Agent Skill ([SKILL.md](SKILL.md)). Add the repository to your agent's skills
directory and drop a link in the chat — the agent will:

1. Run `info` first to inspect the page list (confirms scope for multi-part videos)
2. Run `run` in the background (with concurrency discipline and silent-phase handling)
3. Read `transcript.md` and keyframes
4. Write a structured 1500–5000 character deep `summary.md`
5. Archive the result wherever you ask

## Speed reference (RTX 3050)

| Model | Approx. time per 1-hour video |
|---|---|
| Official AI subtitles | Seconds |
| medium (GPU) | 10–15 min |
| small (GPU) | 5–8 min |
| small (CPU fallback) | 40–60 min |

## Common options

```bash
--no-frames          # transcript only, no keyframes (recommended for podcasts / talking-head videos)
--no-transcript      # keyframes only, no transcript
--model small        # temporarily use a smaller, faster model
--force              # regenerate everything, ignoring existing results (incremental by default)
--scene-threshold    # scene-detection threshold 0–1; lower = more frames
```

## FAQ

- **First run is slow**: it downloads the Whisper model (medium is ~1.5 GB) once. The HuggingFace mirror `hf-mirror.com` is enabled by default (with the Xet protocol disabled, which the mirror does not support).
- **CUDA-related errors**: the tool automatically falls back to the next configuration; it only gets slower. You can also use `--model small` to reduce VRAM pressure.
- **"Official subtitles rejected" message**: Bilibili may keep stale AI subtitles from an older source after an uploader swaps the video. Duration and content checks detect and discard them automatically — this is normal protection.
- **Paid / exclusive videos**: unsupported; the tool reports an explicit error.
- **Do not run two transcriptions in parallel**: on small-VRAM GPUs (≤4 GB), two Whisper processes trigger WDDM VRAM overflow and slow each other down several-fold. Process them serially.

## Platform notes

- Best experience: **Windows + NVIDIA GPU**.
- macOS / Linux: CPU transcription works. `requirements.txt` automatically skips the CUDA dynamic-library packages on macOS.
- `ffmpeg` must be installed and available on your `PATH`.
- On Windows, the CUDA libraries are provided by the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels and registered at runtime.

## Disclaimer

- For personal study and research only. Please respect creators' copyright and do not redistribute transcripts.
- Follow Bilibili's terms of service and keep request rates reasonable (the tool already uses conservative timeouts and serial requests).
- SESSDATA is equivalent to a login credential — never share your `config.ini`.

## License

MIT
