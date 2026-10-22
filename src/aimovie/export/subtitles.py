"""从配音句子时间轴生成 SRT / ASS 字幕。"""

from __future__ import annotations

from pathlib import Path

from ..models import Voiceover


def _clamp(seconds: float) -> float:
    return max(0.0, float(seconds))


def format_srt_time(seconds: float) -> str:
    total_ms = int(round(_clamp(seconds) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_ass_time(seconds: float) -> str:
    total_cs = int(round(_clamp(seconds) * 100))
    hours, rem = divmod(total_cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _clean_text(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def build_srt(voiceover: Voiceover) -> str:
    """生成标准 SRT。"""
    lines: list[str] = []
    for unit in voiceover.units:
        text = _clean_text(unit.text)
        if not text:
            continue
        lines.extend(
            [
                str(unit.index),
                f"{format_srt_time(unit.start_s)} --> {format_srt_time(unit.end_s)}",
                text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _escape_ass(text: str) -> str:
    # ASS 的 {} 是覆盖标签；普通文案里出现时替换为全角括号，避免被解释。
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return text.replace("\n", "\\N")


def build_ass(
    voiceover: Voiceover,
    *,
    width: int,
    height: int,
    font_name: str = "Microsoft YaHei",
    font_size: int = 54,
    margin_v: int = 64,
) -> str:
    """生成带简单样式的 ASS，供 ffmpeg/libass 烧录。"""
    font = (font_name or "Microsoft YaHei").replace(",", " ")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for unit in voiceover.units:
        text = _escape_ass(_clean_text(unit.text))
        if not text:
            continue
        events.append(
            "Dialogue: 0,"
            f"{format_ass_time(unit.start_s)},{format_ass_time(unit.end_s)},"
            f"Default,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"


def write_subtitles(
    voiceover: Voiceover,
    directory: Path | str,
    *,
    width: int,
    height: int,
    font_name: str = "Microsoft YaHei",
    font_size: int = 54,
    margin_v: int = 64,
) -> tuple[Path, Path]:
    """写出 .srt 和 .ass，返回两个路径。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    srt_path = target / "subtitles.srt"
    ass_path = target / "subtitles.ass"
    srt_path.write_text(build_srt(voiceover), encoding="utf-8")
    ass_path.write_text(
        build_ass(
            voiceover,
            width=width,
            height=height,
            font_name=font_name,
            font_size=font_size,
            margin_v=margin_v,
        ),
        encoding="utf-8",
    )
    return srt_path, ass_path
