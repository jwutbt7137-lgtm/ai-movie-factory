"""生成用于本机测试的合成影片（无版权问题）。

- 三个不同颜色的镜头，带网格纹理，方便观察镜头切分和缩略图。
- 优先用 Windows SAPI 生成中文旁白；没有中文语音包时使用正弦音。
- 输出 sample_media/sample_movie.mp4。

用法：
    .venv\\Scripts\\python.exe scripts\\make_sample_video.py
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

NARRATION = "这是一段用于测试的影片旁白。画面里有三个不同的镜头，请检查镜头切分和对白时间轴是否正确。"


def _run(command: list[str], description: str) -> subprocess.CompletedProcess[str]:
    print(f"[执行] {description}")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        print(completed.stdout[-1000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"{description} 失败（退出码 {completed.returncode}）")
    return completed


def synthesize_narration(target: Path) -> bool:
    """调用 Windows SAPI 生成中文 WAV；失败返回 False。"""
    if sys.platform != "win32":
        return False
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$zh = $synth.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Culture.Name -like 'zh*' }} | Select-Object -First 1
if (-not $zh) {{ Write-Error 'NO_ZH_VOICE'; exit 3 }}
$synth.SelectVoice($zh.VoiceInfo.Name)
$synth.Rate = 0
$synth.Volume = 100
$synth.SetOutputToWaveFile('{str(target).replace("'", "''")}')
$synth.Speak('{NARRATION}')
$synth.Dispose()
Write-Output 'OK'
"""
    script_path = target.with_suffix(".ps1")
    script_path.write_text(script, encoding="utf-8-sig")
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[警告] SAPI 调用失败：{exc}")
        return False
    finally:
        script_path.unlink(missing_ok=True)
    if completed.returncode != 0 or not target.exists():
        print(f"[警告] 未找到中文 SAPI 语音，改用正弦音。{completed.stdout.strip()} {completed.stderr.strip()[:300]}")
        return False
    print(f"[信息] 中文旁白：{target}（{target.stat().st_size / 1024:.0f} KB）")
    return True


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("sample_media/sample_movie.mp4"))
    parser.add_argument("--duration", type=float, default=9.0, help="成片最短时长（秒）")
    parser.add_argument("--narration", type=Path, help="使用已有的中文旁白 WAV，而不是调用 SAPI")
    args = parser.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("找不到 ffmpeg，请先安装并加入 PATH。", file=sys.stderr)
        return 1

    out = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    narration = out.parent / "narration.wav"
    if args.narration:
        source_narration = args.narration.expanduser().resolve()
        if not source_narration.exists():
            print(f"找不到旁白文件：{source_narration}", file=sys.stderr)
            return 1
        if source_narration != narration.resolve():
            shutil.copyfile(source_narration, narration)
        has_narration = True
        print(f"[信息] 使用已有旁白：{source_narration}")
    elif narration.exists() and narration.stat().st_size > 1024:
        has_narration = True
        print(f"[信息] 复用目录中已有的中文旁白：{narration}")
    else:
        has_narration = synthesize_narration(narration)
    total_duration = args.duration
    if has_narration:
        total_duration = max(total_duration, wav_duration(narration) + 1.0)
    shot_duration = round(total_duration / 3.0, 3)
    total_duration = shot_duration * 3

    colors = ["0x8B1A1A", "0x1A6B3C", "0x1A3C8B"]
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for color in colors:
        command += [
            "-f",
            "lavfi",
            "-i",
            (
                f"color=c={color}:s=1280x720:r=24:d={shot_duration},"
                "drawgrid=w=160:h=90:t=2:c=white@0.22"
            ),
        ]
    if has_narration:
        command += ["-i", str(narration)]
    else:
        command += ["-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={total_duration}"]

    filter_complex = (
        "[0:v]format=yuv420p[v0];[1:v]format=yuv420p[v1];[2:v]format=yuv420p[v2];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[v];"
        f"[3:a]apad,atrim=0:{total_duration},asetpts=PTS-STARTPTS[a]"
    )
    command += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(out),
    ]
    _run(command, "合成测试影片")
    meta = {
        "output": str(out),
        "duration_s": total_duration,
        "shot_count": 3,
        "shot_duration_s": shot_duration,
        "narration": has_narration,
        "note": "合成测试素材，无版权风险；用于验证镜头切分、缩略图与语音识别。",
    }
    (out.parent / "sample_movie.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] {out}（{total_duration:.1f} 秒，3 个镜头）")
    if not has_narration:
        print("[提示] 本次没有中文旁白，语音识别可能为空；这不是程序错误。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
