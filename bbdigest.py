#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bbdigest — B站视频速览工具

用法:
  python bbdigest.py info <链接|BV号>
      只拉取视频信息和分P清单，不下载
  python bbdigest.py run <链接|BV号> [--pages all|1,3-5] [选项]
      完整处理: 语音转文字 + 关键帧截图，输出到 notes/ 目录

链接支持: 完整URL(可带 ?p=N)、b23.tv 短链、裸 BV号、av号
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Windows 控制台中文输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# HuggingFace 镜像（国内可直连）；若环境中已设置则优先用环境的。
# 镜像站不支持 Xet 下载协议，必须禁用，否则新版 huggingface_hub 会报错
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "_cache"
NOTES_DIR = BASE_DIR / "notes"
CONFIG_FILE = BASE_DIR / "config.ini"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = {
        "sessdata": "",
        "buvid3": "",
        "language": "auto",
        "model": "medium",
        "scene_threshold": 0.3,
        "fallback_interval": 240,
    }
    if CONFIG_FILE.exists():
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(CONFIG_FILE, encoding="utf-8-sig")
        if cp.has_section("bilibili"):
            cfg["sessdata"] = cp.get("bilibili", "SESSDATA", fallback="").strip()
            cfg["buvid3"] = cp.get("bilibili", "buvid3", fallback="").strip()
        if cp.has_section("whisper"):
            cfg["language"] = cp.get("whisper", "language", fallback="auto").strip()
            cfg["model"] = cp.get("whisper", "model", fallback="medium").strip()
        if cp.has_section("frames"):
            cfg["scene_threshold"] = cp.getfloat("frames", "scene_threshold", fallback=0.3)
            cfg["fallback_interval"] = cp.getint("frames", "fallback_interval", fallback=240)
    return cfg


# ---------------------------------------------------------------- 链接解析

def parse_input(s: str) -> tuple[str, int | None]:
    """返回 (bvid 或 'av数字', 分P提示或None)"""
    s = s.strip().strip('"').strip("'")
    if "b23.tv" in s:
        import requests
        r = requests.get(s, headers={"User-Agent": UA}, allow_redirects=True, timeout=20)
        s = r.url
        log(f"  短链解析为: {s}")
    m = re.search(r"[Bb][Vv][0-9A-Za-z]{10}", s)
    if m:
        vid = "BV" + m.group(0)[2:]
        page = None
        mp = re.search(r"[?&]p=(\d+)", s)
        if mp:
            page = int(mp.group(1))
        return vid, page
    m = re.search(r"av(\d+)", s, re.IGNORECASE)
    if m:
        page = None
        mp = re.search(r"[?&]p=(\d+)", s)
        if mp:
            page = int(mp.group(1))
        return "av" + m.group(1), page
    raise SystemExit(f"无法从输入中识别视频ID: {s}")


# ---------------------------------------------------------------- B站 API

def bili_cookies(cfg: dict) -> dict:
    c = {}
    if cfg.get("sessdata"):
        c["SESSDATA"] = cfg["sessdata"]
    if cfg.get("buvid3"):
        c["buvid3"] = cfg["buvid3"]
    return c


def api_get(url: str, params: dict, cookies: dict) -> dict:
    import requests
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    r = requests.get(url, params=params, headers=headers, cookies=cookies, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_view(vid: str, cfg: dict) -> dict:
    params = {"bvid": vid} if vid.upper().startswith("BV") else {"aid": vid[2:]}
    data = api_get("https://api.bilibili.com/x/web-interface/view", params, bili_cookies(cfg))
    if data.get("code") != 0:
        raise SystemExit(f"获取视频信息失败: [{data.get('code')}] {data.get('message')}")
    v = data["data"]
    rights = v.get("rights") or {}
    if rights.get("is_upower_exclusive"):
        raise SystemExit("这是充电专属视频，无法直接下载处理。")
    return v


def fetch_subtitle(vid: str, cid: int, cfg: dict, duration: float = 0) -> tuple[list | None, str]:
    """尝试拿官方 AI/CC 字幕，返回 ([(from, to, text)], 说明) 或 (None, 拒因)"""
    if not cfg.get("sessdata"):
        return None, ""
    try:
        params = {"cid": cid}
        if vid.upper().startswith("BV"):
            params["bvid"] = vid
        else:
            params["aid"] = vid[2:]
        data = api_get("https://api.bilibili.com/x/player/v2", params, bili_cookies(cfg))
        subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    except Exception:
        return None, ""
    if not subs:
        return None, ""
    # AI 字幕优先
    subs = sorted(subs, key=lambda s: 0 if str(s.get("lan", "")).lower().startswith("ai") else 1)
    sub = subs[0]
    surl = sub.get("subtitle_url") or ""
    if surl.startswith("//"):
        surl = "https:" + surl
    if not surl:
        return None, ""
    try:
        import requests
        r = requests.get(surl, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                         timeout=25)
        body = r.json().get("body") or []
    except Exception:
        return None, ""
    segs = [(float(x.get("from", 0)), float(x.get("to", 0)), str(x.get("content", "")).strip())
            for x in body]
    segs = [s for s in segs if s[2]]
    if not segs:
        return None, ""
    if duration:
        # UP主换视频源后，B站可能残留旧内容的AI字幕，用时间覆盖范围校验
        last_end = segs[-1][1]
        if last_end > duration * 1.15 + 5:
            return None, (f"字幕时间戳({int(last_end)}s)超出视频时长({int(duration)}s)，"
                          "是换源残留的过期字幕")
        if last_end < duration * 0.4:
            return None, f"字幕仅覆盖到{int(last_end)}s/{int(duration)}s，疑似残缺"
    return segs, f"B站字幕({sub.get('lan', '')}, {len(segs)}条)"


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s.lower())


def _spot_check_subtitle(audio_path: Path, segs: list, duration: float) -> tuple[bool, str]:
    """从音频抽3个窗口用whisper快转，与字幕同时间段文本比对相似度。
    返回 (是否可信, 说明)。换源残留字幕时长可能与本视频相近，时长校验拦不住，只能内容比对。"""
    import subprocess as sp
    from difflib import SequenceMatcher

    windows = []
    for f in (0.2, 0.5, 0.8):
        t = duration * f
        if t + 30 < duration:
            windows.append(t)
    if not windows:
        return True, "无采样点"

    tmpdir = CACHE_DIR / "_spotcheck"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp_wav = tmpdir / "clip.wav"

    def whisper_small():
        from faster_whisper import WhisperModel
        return WhisperModel("small", device="cuda", compute_type="int8_float16")

    model = None
    ratios = []
    try:
        for t in windows:
            r = sp.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-ss", f"{t:.1f}", "-t", "30", "-i", str(audio_path),
                        "-ar", "16000", "-ac", "1", str(tmp_wav)],
                       capture_output=True, text=True)
            if r.returncode != 0 or not tmp_wav.exists():
                continue
            if model is None:
                _register_nvidia_dlls()
                # 优先复用/预加载与正式转写相同的模型（缓存共享，不浪费显存）
                try:
                    model = _get_cached_model("medium", "cuda", "int8_float16")
                except Exception:
                    model = None
                if model is None:
                    try:
                        model = _get_cached_model("small", "cpu", "int8")
                    except Exception:
                        return True, "whisper不可用,跳过抽检"
            segs_w, _ = model.transcribe(str(tmp_wav), vad_filter=True)
            heard = _norm_text("".join(s.text for s in segs_w))
            expect = _norm_text("".join(txt for a, b, txt in segs if b > t and a < t + 30))
            if not heard or not expect:
                continue  # 该窗口可能无人声/字幕无覆盖，不计入
            ratios.append(round(SequenceMatcher(None, heard, expect).ratio(), 3))
        tmp_wav.unlink(missing_ok=True)
    except Exception as e:
        return True, f"抽检异常({type(e).__name__}),保守放行"

    if len(ratios) < 2:
        return True, f"有效采样不足({len(ratios)})"
    best = max(ratios)
    ok = best >= 0.15
    return ok, f"相似度{ratios},阈值0.15"


# ---------------------------------------------------------------- yt-dlp 下载

def _write_cookiefile(cfg: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / "cookies.txt"
    buvid = cfg.get("buvid3") or uuid.uuid4().hex.upper()
    lines = ["# Netscape HTTP Cookie File"]
    if cfg.get("sessdata"):
        lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\t{cfg['sessdata']}")
    lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\tbuvid3\t{buvid}")
    p.write_text("\n".join(lines) + "\n", encoding="ascii")
    return p


def _ydl_base_opts(cfg: dict) -> dict:
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
        "concurrent_fragment_downloads": 4,
        "ffmpeg_location": FFMPEG,
    }
    if cfg.get("sessdata"):
        opts["cookiefile"] = str(_write_cookiefile(cfg))
    return opts


def _ydl_download(opts: dict, url: str, outtmpl: str) -> Path:
    import yt_dlp
    opts = dict(opts)
    opts["outtmpl"] = {"default": outtmpl}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise SystemExit("yt-dlp 未返回任何媒体流（视频可能收费/地区限制/已失效）")
            info = entries[0]
        fp = None
        rd = info.get("requested_downloads") or []
        if rd:
            fp = rd[0].get("filepath")
        fp = fp or ydl.prepare_filename(info)
    return Path(fp)


def download_audio(bvid: str, page: int, cfg: dict) -> Path:
    out = CACHE_DIR / f"{bvid}_p{page}_audio.%(ext)s"
    opts = _ydl_base_opts(cfg)
    opts["format"] = "bestaudio/best"
    url = f"https://www.bilibili.com/video/{bvid}?p={page}"
    return _ydl_download(opts, url, str(out))


def download_video(bvid: str, page: int, cfg: dict) -> Path:
    out = CACHE_DIR / f"{bvid}_p{page}_video.%(ext)s"
    opts = _ydl_base_opts(cfg)
    opts["format"] = (
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=1080]+bestaudio/"
        "best[height<=1080]/best"
    )
    opts["merge_output_format"] = "mp4"
    url = f"https://www.bilibili.com/video/{bvid}?p={page}"
    return _ydl_download(opts, url, str(out))


# ---------------------------------------------------------------- 转写 (faster-whisper)

_DLLS_REGISTERED = False


def _register_nvidia_dlls() -> None:
    """注册 pip 安装的 cuBLAS/cuDNN DLL 目录（Windows GPU 必需）。
    必须幂等：批量任务反复调用时若重复追加 PATH，会撑爆Windows的32KB环境变量上限"""
    global _DLLS_REGISTERED
    if _DLLS_REGISTERED:
        return
    if os.name == "nt":
        import site
        roots = list(site.getsitepackages())
        try:
            roots.append(site.getusersitepackages())
        except Exception:
            pass
        for root in roots:
            nvidia = Path(root) / "nvidia"
            if not nvidia.is_dir():
                continue
            for dll_dir in nvidia.glob("*/bin"):
                try:
                    os.add_dll_directory(str(dll_dir))
                except Exception:
                    pass
                os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
    _DLLS_REGISTERED = True


_MODEL_CACHE: dict = {}


def _get_cached_model(name: str, device: str, compute: str):
    """进程内缓存 Whisper 模型，批量任务避免反复加载"""
    key = (name, device, compute)
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        _MODEL_CACHE[key] = WhisperModel(name, device=device, compute_type=compute)
    return _MODEL_CACHE[key]


def transcribe_audio(audio_path: Path, cfg: dict) -> tuple[list, str]:
    """返回 ([(start, end, text)], 方法说明)"""
    _register_nvidia_dlls()
    lang = None if cfg["language"] in ("", "auto") else cfg["language"]
    attempts = [
        (cfg["model"], "cuda", "int8_float16"),
        ("small", "cuda", "int8_float16"),
        ("small", "cpu", "int8"),
    ]
    seen, plan = set(), []
    for a in attempts:
        if a not in seen:
            seen.add(a)
            plan.append(a)

    last_err = None
    for model_name, device, compute in plan:
        try:
            log(f"  加载 Whisper 模型 {model_name} ({device}/{compute})，首次会下载模型文件...")
            model = _get_cached_model(model_name, device, compute)
            segments, info = model.transcribe(
                str(audio_path), language=lang, vad_filter=True,
                vad_parameters=dict(threshold=0.35, min_silence_duration_ms=700,
                                    speech_pad_ms=400))
            segs = [(s.start, s.end, s.text.strip()) for s in segments]
            segs = [x for x in segs if x[2]]
            if not segs:
                # VAD 对纯音乐/歌声类音频可能过度过滤，关闭后重试一次
                log("  VAD 未检出语音，关闭 VAD 重试...")
                segments, info = model.transcribe(str(audio_path), language=lang,
                                                  vad_filter=False)
                segs = [(s.start, s.end, s.text.strip()) for s in segments]
                segs = [x for x in segs if x[2]]
            det = info.language or "?"
            return segs, f"faster-whisper({model_name}, {device}, 检测语言={det})"
        except Exception as e:  # 显存不足等 → 降档重试
            last_err = e
            log(f"  [!] {model_name}/{device} 失败: {type(e).__name__}: {e}")
            log("      尝试下一档配置...")
    raise SystemExit(f"所有转写配置均失败，最后错误: {last_err}")


# ---------------------------------------------------------------- 关键帧

def _fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _frame_name(sec: float) -> str:
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}-{m:02d}-{s:02d}"


def _phash(path: Path):
    import imagehash
    from PIL import Image
    with Image.open(path) as im:
        return imagehash.phash(im)


def _capture_at(video: Path, t: float, out: Path) -> bool:
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{max(t, 0):.2f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", "-y", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return out.exists()


def extract_frames(video: Path, frames_dir: Path, threshold: float,
                   fallback_interval: int, duration: float) -> list[Path]:
    """场景检测截帧 + 感知哈希去重 + 长静止画面兜底，返回最终帧文件列表（按时间排序）"""
    raw = frames_dir / "_raw"
    raw.mkdir(parents=True, exist_ok=True)
    for old in raw.glob("f_*.jpg"):
        old.unlink()

    # 1) ffmpeg 场景检测
    log(f"  ffmpeg 场景检测 (阈值 {threshold}) ...")
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-i", str(video),
           "-vf", f"select='gt(scene,{threshold})',showinfo,scale=1280:-2",
           "-fps_mode", "vfr", "-q:v", "2", "-an", str(raw / "f_%05d.jpg")]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    times = sorted({round(float(t), 3) for t in re.findall(r"pts_time:([\d.]+)", proc.stderr)})

    # 2) 去重：相邻 2 秒内或感知哈希接近的丢弃
    kept: list[tuple[float, Path]] = []
    hashes: list = []
    for i, t in enumerate(times):
        p = raw / f"f_{i + 1:05d}.jpg"
        if not p.exists():
            continue
        if kept and t - kept[-1][0] < 2.0:
            p.unlink()
            continue
        if hashes:
            h = _phash(p)
            if min(h - last for last in hashes[-3:]) <= 8:
                p.unlink()
                continue
            hashes.append(h)
        else:
            hashes.append(_phash(p))
        kept.append((t, p))

    # 3) 兜底：开头一张 + 相邻帧间隔超过 fallback_interval 时补一张
    inserts: list[float] = []
    if kept:
        if kept[0][0] > 5:
            inserts.append(0.5)
        for (ta, _), (tb, _) in zip(kept, kept[1:]):
            n_extra = int((tb - ta) // fallback_interval)
            for j in range(1, n_extra + 1):
                inserts.append(ta + j * fallback_interval)
    else:
        log("  无场景切换（画面基本静止），使用固定间隔")
        inserts = [0.5]
        t = fallback_interval
        while t < duration - 5:
            inserts.append(t)
            t += fallback_interval
        if duration > 10:
            inserts.append(max(duration - 2, inserts[-1] + 5 if inserts else 0.5))

    n = 0
    for t in inserts:
        n += 1
        p = raw / f"extra_{n:04d}.jpg"
        if _capture_at(video, t, p):
            kept.append((t, p))
    kept.sort(key=lambda x: x[0])

    # 4) 归位到 frames/ 目录，按时间命名
    final: list[Path] = []
    for t, src in kept:
        name = _frame_name(t) + ".jpg"
        dst = frames_dir / name
        k = 1
        while dst.exists():
            dst = frames_dir / f"{_frame_name(t)}-{k}.jpg"
            k += 1
        shutil.move(str(src), str(dst))
        final.append(dst)
    shutil.rmtree(raw, ignore_errors=True)
    return final


# ---------------------------------------------------------------- 产物写入

def sanitize(name: str, limit: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name).strip(" .")
    return (s[:limit].strip(" .")) or "untitled"


def video_folder_name(view: dict) -> str:
    """notes/ 下的视频目录名: 标题-UP主-发布日期(YYYYMMDD)"""
    title = sanitize(view.get("title") or "untitled", 60)
    owner = sanitize((view.get("owner") or {}).get("name") or "unknown", 30)
    pub = view.get("pubdate") or 0
    date = time.strftime("%Y%m%d", time.localtime(pub)) if pub else "nodate"
    return sanitize(f"{title}-{owner}-{date}", 80)


def resolve_vid_dir(view: dict) -> Path:
    """定位视频目录: notes/ 根目录优先；若已被整理进子目录（如 鞋类评测/），在一级子目录中查找"""
    name = video_folder_name(view)
    direct = NOTES_DIR / name
    if direct.exists():
        return direct
    if NOTES_DIR.is_dir():
        for sub in NOTES_DIR.iterdir():
            if sub.is_dir():
                candidate = sub / name
                if candidate.is_dir():
                    return candidate
    return direct


def write_srt(path: Path, segs: list) -> None:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, m, s = ms // 3600000, ms % 3600000 // 60000, ms % 60000 // 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms % 1000:03d}"

    lines = []
    for i, (a, b, text) in enumerate(segs, 1):
        lines += [str(i), f"{ts(a)} --> {ts(max(b, a + 0.2))}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _join_seg_text(acc: str, text: str) -> str:
    """中英文混排友好拼接: 前后都是拉丁字符时补空格"""
    if not acc:
        return text
    if re.search(r"[A-Za-z0-9,.;:!?']$", acc) and re.match(r"[A-Za-z0-9(\[' ]", text):
        return acc + " " + text
    return acc + text


def write_transcript_md(path: Path, meta: dict, segs: list, method: str) -> None:
    lines = [
        f"# {meta['ptitle']}",
        "",
        f"- 视频: [{meta['title']}]({meta['url']})",
        f"- UP主: {meta['owner']}",
        f"- 本P时长: {_fmt_ts(meta['duration'])}",
        f"- 转写方式: {method}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 正文",
        "",
    ]
    if not segs:
        lines.append("（本P未检测到语音内容——可能是纯音乐/无人声视频）")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    joined = ""
    buf_start = segs[0][0]
    last_end = 0.0
    for start, end, text in segs:
        if joined and (start - last_end > 2.0 or start - buf_start > 30):
            lines.append(f"[{_fmt_ts(buf_start)}] " + joined)
            lines.append("")
            joined = ""
            buf_start = start
        joined = _join_seg_text(joined, text)
        last_end = end
    if joined:
        lines.append(f"[{_fmt_ts(buf_start)}] " + joined)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_info_json(vid_dir: Path, view: dict) -> None:
    data = {
        "bvid": view.get("bvid"),
        "aid": view.get("aid"),
        "title": view.get("title"),
        "owner": (view.get("owner") or {}).get("name"),
        "desc": view.get("desc"),
        "duration": view.get("duration"),
        "pages": [
            {"page": p.get("page"), "part": p.get("part"),
             "duration": p.get("duration"), "cid": p.get("cid")}
            for p in (view.get("pages") or [])
        ],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    (vid_dir / "info.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_cover(view: dict, vid_dir: Path) -> None:
    pic = view.get("pic") or ""
    if not pic:
        return
    if pic.startswith("//"):
        pic = "https:" + pic
    try:
        import requests
        r = requests.get(pic, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                         timeout=30)
        if r.ok and r.headers.get("Content-Type", "").startswith("image"):
            (vid_dir / "cover.jpg").write_bytes(r.content)
    except Exception:
        pass


# ---------------------------------------------------------------- 命令

def page_done_marks(vid_dir: Path, n: int) -> list[str]:
    """检测某个分P是否已产出（兼容单P扁平布局和多P子目录布局）"""
    marks = []
    if (vid_dir / "transcript.md").exists() or list(vid_dir.glob(f"P{n:02d}_*/transcript.md")):
        marks.append("已转写")
    fdir_ok = (vid_dir / "frames").is_dir() and any((vid_dir / "frames").glob("*.jpg"))
    if fdir_ok or list(vid_dir.glob(f"P{n:02d}_*/frames/*.jpg")):
        marks.append("已截帧")
    return marks


def fetch_season_info(view: dict) -> dict | None:
    """B站"合集"是多个独立视频的集合，不是分P；返回合集元信息供提示用"""
    sid = view.get("season_id")
    mid = (view.get("owner") or {}).get("mid")
    if not sid or not mid:
        return None
    try:
        import requests
        r = requests.get(
            "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
            params={"mid": mid, "season_id": sid, "page_num": 1, "page_size": 1},
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}, timeout=20)
        d = r.json()
        if d.get("code") != 0:
            return None
        meta = d["data"]["meta"]
        return {"name": meta.get("name"), "total": meta.get("total")}
    except Exception:
        return None


def notice_season(view: dict) -> None:
    season = fetch_season_info(view)
    if season:
        log(f"\n[提示] 该视频属于UP主合集《{season['name']}》（共{season['total']}个独立视频）。")
        log("       合集≠分P，本工具只处理当前链接的视频；如需合集里其他视频，请单独发链接。")


def print_pages(view: dict, vid_dir: Path | None = None) -> None:
    pages = view.get("pages") or []
    total = view.get("duration") or sum(p.get("duration") or 0 for p in pages)
    log(f"\n标题: {view.get('title')}")
    log(f"UP主: {(view.get('owner') or {}).get('name')}  |  总时长: {_fmt_ts(total)}  |  分P数: {len(pages)}")
    desc = (view.get("desc") or "").strip()
    if desc:
        log(f"简介: {desc[:200]}{'...' if len(desc) > 200 else ''}")
    log("")
    for p in pages:
        n = p.get("page")
        done = ""
        if vid_dir:
            marks = page_done_marks(vid_dir, n)
            if marks:
                done = "  ✓" + "/".join(marks)
        log(f"  P{n:<3d} {_fmt_ts(p.get('duration') or 0):>8s}  {p.get('part')}{done}")
    log("")


def process_page(view: dict, pinfo: dict, cfg: dict, args,
                 vid_dir: Path, single_layout: bool) -> None:
    bvid = view["bvid"]
    n = pinfo["page"]
    if single_layout:
        pdir = vid_dir
    else:
        pdir = vid_dir / f"P{n:02d}_{sanitize(pinfo['part'])}"
    pdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": view.get("title", ""),
        "ptitle": pinfo.get("part") or f"P{n}",
        "owner": (view.get("owner") or {}).get("name", ""),
        "url": f"https://www.bilibili.com/video/{bvid}?p={n}",
        "duration": pinfo.get("duration") or 0,
    }
    log(f"\n=== P{n}: {meta['ptitle']} (时长 {_fmt_ts(meta['duration'])}) ===")

    # ---- 转写 ----
    if not args.no_transcript:
        tfile = pdir / "transcript.md"
        if tfile.exists() and not args.force:
            log("  转写已存在，跳过 (--force 可重新生成)")
        else:
            segs, method = None, ""
            audio = None
            if meta["duration"] > 600:
                log("  获取官方字幕...")
                segs, method = fetch_subtitle(bvid, pinfo["cid"], cfg, float(meta["duration"]))
                if segs:
                    log(f"  命中 {method}，下载音频做内容抽检...")
                    audio = download_audio(bvid, n, cfg)
                    ok, detail = _spot_check_subtitle(audio, segs, float(meta["duration"]))
                    if ok:
                        log(f"  字幕抽检通过 ({detail})")
                    else:
                        log(f"  [!] 字幕抽检不符 ({detail})，判定为换源残留字幕")
                        segs = None
            else:
                # 短视频本地转写很快；换源残留字幕时长可能与本视频相近，时长校验拦不住
                log("  短视频(≤10分钟)直接本地转写，规避换源残留字幕...")
            if not segs:
                if audio is None:
                    if method:
                        log(f"  [!] 弃用字幕: {method}")
                    log("  下载音频进行本地转写...")
                    audio = download_audio(bvid, n, cfg)
                log(f"  音频就绪: {audio.name} ({audio.stat().st_size / 1e6:.1f} MB)")
                segs, method = transcribe_audio(audio, cfg)
                log(f"  转写完成: {len(segs)} 段 ({method})")
            write_transcript_md(tfile, meta, segs, method)
            write_srt(pdir / "transcript.srt", segs)
            log(f"  已写入 {tfile.relative_to(BASE_DIR)}")

    # ---- 关键帧 ----
    if not args.no_frames:
        fdir = pdir / "frames"
        if fdir.exists() and any(fdir.glob("*.jpg")) and not args.force:
            log("  关键帧已存在，跳过 (--force 可重新生成)")
        else:
            log("  下载视频用于截帧...")
            video = download_video(bvid, n, cfg)
            log(f"  视频就绪: {video.name} ({video.stat().st_size / 1e6:.1f} MB)")
            frames = extract_frames(
                video, fdir,
                cfg["scene_threshold"] if args.scene_threshold is None else args.scene_threshold,
                cfg["fallback_interval"] if args.fallback_interval is None else args.fallback_interval,
                float(meta["duration"]),
            )
            log(f"  保留关键帧 {len(frames)} 张 → {fdir.relative_to(BASE_DIR)}")


def parse_pages(spec: str, total: int) -> list[int]:
    if spec.strip().lower() == "all":
        return list(range(1, total + 1))
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.update(range(int(a), int(b) + 1))
        else:
            wanted.add(int(part))
    sel = sorted(p for p in wanted if 1 <= p <= total)
    if not sel:
        raise SystemExit(f"--pages 无有效分P (视频共 {total}P)")
    dropped = wanted - set(sel)
    if dropped:
        log(f"  [!] 超出范围的分P已忽略: {sorted(dropped)}")
    return sel


def cmd_info(args) -> None:
    cfg = load_config()
    vid, _ = parse_input(args.url)
    view = fetch_view(vid, cfg)
    vid_dir = resolve_vid_dir(view)
    vid_dir.mkdir(parents=True, exist_ok=True)
    write_info_json(vid_dir, view)
    save_cover(view, vid_dir)
    print_pages(view, vid_dir)
    notice_season(view)


def cmd_run(args) -> None:
    cfg = load_config()
    if args.model:
        cfg["model"] = args.model
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    vid, page_hint = parse_input(args.url)
    view = fetch_view(vid, cfg)
    bvid = view["bvid"]
    vid_dir = resolve_vid_dir(view)
    vid_dir.mkdir(parents=True, exist_ok=True)
    write_info_json(vid_dir, view)
    save_cover(view, vid_dir)

    pages = view.get("pages") or []
    total = len(pages)
    log(f"标题: {view.get('title')}  (共 {total}P)")

    if total > 1 and not args.pages and not page_hint:
        print_pages(view, vid_dir)
        log(f"该视频有 {total} 个分P。请指定要处理的分P，例如:")
        log(f"  python bbdigest.py run {args.url} --pages 1,3-5")
        log(f"  python bbdigest.py run {args.url} --pages all")
        return

    if args.pages:
        sel = parse_pages(args.pages, total)
    elif page_hint and total > 1:
        if not (1 <= page_hint <= total):
            raise SystemExit(f"URL 中的 p={page_hint} 超出范围 (共 {total}P)")
        sel = [page_hint]
    else:
        sel = list(range(1, total + 1))

    log(f"待处理分P: {sel}")
    single_layout = total <= 1
    for n in sel:
        pinfo = pages[n - 1] if total > 1 else (pages[0] if pages else
                                                {"page": 1, "part": view.get("title", "P1"),
                                                 "cid": view.get("cid"), "duration": view.get("duration")})
        process_page(view, pinfo, cfg, args, vid_dir, single_layout)

    log("\n全部完成。产物目录:")
    if single_layout:
        log(f"  {vid_dir}")
    else:
        for n in sel:
            for d in sorted(vid_dir.glob(f"P{n:02d}_*")):
                log(f"  {d}")
    log("\n提示: 把链接和产物发回给 ZCode 对话，可以继续生成 summary.md 总结。")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="bbdigest", description="B站视频速览工具: 转写文本 + 关键帧截图")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="只看视频信息/分P清单（不下载）")
    p_info.add_argument("url", help="链接 / BV号 / av号")

    p_run = sub.add_parser("run", help="完整处理: 转写 + 关键帧")
    p_run.add_argument("url", help="链接 / BV号 / av号")
    p_run.add_argument("--pages", default="",
                       help='处理哪些分P: all 或 "1,3-5"（单P视频无需指定; 多P视频建议先 info 查看）')
    p_run.add_argument("--no-transcript", action="store_true", help="跳过转写")
    p_run.add_argument("--no-frames", action="store_true", help="跳过关键帧")
    p_run.add_argument("--scene-threshold", type=float, default=None,
                       help="场景检测阈值 0-1，越小截帧越多 (默认 0.3)")
    p_run.add_argument("--fallback-interval", type=int, default=None,
                       help="画面静止时兜底截图间隔秒数 (默认 240)")
    p_run.add_argument("--model", default=None, help="覆盖 Whisper 模型: small / medium")
    p_run.add_argument("--force", action="store_true", help="忽略已有结果，全部重新生成")

    args = ap.parse_args()
    if args.cmd == "info":
        cmd_info(args)
    elif args.cmd == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
