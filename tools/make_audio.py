#!/usr/bin/env python3
"""產生內建泰語錄音，並以 base64 內嵌進 index.html。

流程：
1. 從 index.html 找出所有內建資料的泰文（`thai: '...'`）。
2. 用 edge-tts（預設 th-TH-PremwadeeNeural）逐一產生 MP3，存在 tools/audio_cache/。
   已產生過的會直接沿用，不會重複下載。
3. 有 ffmpeg 時，轉成單聲道 32kbps 並剪掉前後靜音；沒有 ffmpeg 則保留 edge-tts 原始的
   單聲道 48kbps。
4. 把 index.html 裡 AUDIO:BEGIN 與 AUDIO:END 之間的內容，換成最新的 base64 音檔。

用法：
    pip install edge-tts
    python tools/make_audio.py              # 產生並內嵌
    python tools/make_audio.py --force      # 全部重新產生
    python tools/make_audio.py --rate -10%  # 語速稍慢
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML = ROOT / "index.html"
CACHE_DIR = Path(__file__).resolve().parent / "audio_cache"
DEFAULT_VOICE = "th-TH-PremwadeeNeural"
BITRATE = "32k"
CONCURRENCY = 4

THAI_RE = re.compile(r"\bthai:\s*'([^']+)'")
BLOCK_RE = re.compile(r"(/\* AUDIO:BEGIN[^\n]*\*/\n)(.*?)(/\* AUDIO:END \*/)", re.S)


def find_texts(html: str) -> list[str]:
    """依出現順序列出不重複的泰文。"""
    seen = {}
    for m in THAI_RE.finditer(html):
        seen.setdefault(m.group(1).strip(), None)
    return list(seen)


def cache_path(text: str, voice: str, rate: str) -> Path:
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{key}.mp3"


async def synthesize(text: str, voice: str, rate: str, out: Path) -> None:
    import edge_tts

    tmp = out.with_suffix(".part")
    await edge_tts.Communicate(text, voice, rate=rate).save(str(tmp))
    if tmp.stat().st_size == 0:
        tmp.unlink()
        raise RuntimeError("edge-tts 回傳空的音檔")
    tmp.replace(out)


def compress(src: Path, dst: Path) -> None:
    """轉成單聲道 32kbps，並剪掉前後靜音（保留 0.1 秒）。"""
    trim = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-af", f"{trim},areverse,{trim},areverse",
         "-ac", "1", "-ar", "24000", "-b:a", BITRATE, "-map_metadata", "-1", str(dst)],
        check=True,
    )


async def build(texts, voice, rate, force):
    CACHE_DIR.mkdir(exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    failed = []

    async def one(i, text):
        raw = cache_path(text, voice, rate)
        if raw.exists() and not force:
            return
        async with sem:
            for attempt in range(3):
                try:
                    await synthesize(text, voice, rate, raw)
                    print(f"  [{i + 1}/{len(texts)}] OK  {text}")
                    return
                except Exception as e:  # 網路不穩時重試
                    if attempt == 2:
                        print(f"  [{i + 1}/{len(texts)}] 失敗 {text}: {e}")
                        failed.append(text)
                    else:
                        await asyncio.sleep(2 * (attempt + 1))

    await asyncio.gather(*(one(i, t) for i, t in enumerate(texts)))
    return failed


def encode(texts, voice, rate) -> dict[str, str]:
    use_ffmpeg = shutil.which("ffmpeg") is not None
    if not use_ffmpeg:
        print("提醒：找不到 ffmpeg，音檔維持 edge-tts 原始的 48kbps（檔案會大約 1.5 倍）。")
    out = {}
    for text in texts:
        raw = cache_path(text, voice, rate)
        if not raw.exists():
            continue
        data = raw
        if use_ffmpeg:
            data = raw.with_name(raw.stem + f"-{BITRATE}.mp3")
            if not data.exists() or data.stat().st_mtime < raw.stat().st_mtime:
                compress(raw, data)
        out[text] = base64.b64encode(data.read_bytes()).decode("ascii")
    return out


def embed(html: str, audio: dict[str, str]) -> str:
    body = "window.PASATHAI_AUDIO = {\n"
    body += "".join(f"  {json.dumps(k, ensure_ascii=False)}: \"{v}\",\n" for k, v in audio.items())
    body += "};\n"
    if not BLOCK_RE.search(html):
        sys.exit("index.html 裡找不到 AUDIO:BEGIN / AUDIO:END 標記。")
    return BLOCK_RE.sub(lambda m: m.group(1) + body + m.group(3), html, count=1)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 主控台顯示泰文
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", type=Path, default=DEFAULT_HTML, help="index.html 路徑")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"edge-tts 語音（預設 {DEFAULT_VOICE}）")
    ap.add_argument("--rate", default="+0%", help="語速，例如 -10%%")
    ap.add_argument("--force", action="store_true", help="忽略快取，全部重新產生")
    args = ap.parse_args()

    html = args.html.read_text(encoding="utf-8")
    texts = find_texts(html)
    print(f"找到 {len(texts)} 個要錄音的泰文（語音：{args.voice}）")

    failed = asyncio.run(build(texts, args.voice, args.rate, args.force))
    audio = encode(texts, args.voice, args.rate)
    new_html = embed(html, audio)
    with open(args.html, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_html)

    size_kb = len(new_html.encode("utf-8")) / 1024
    print(f"已內嵌 {len(audio)}/{len(texts)} 個音檔，index.html 目前 {size_kb:.0f} KB")
    if failed:
        print(f"有 {len(failed)} 個失敗，這些字會改用瀏覽器語音。請稍後再執行一次（已完成的不會重做）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
