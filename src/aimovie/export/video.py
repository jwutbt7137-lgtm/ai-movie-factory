"""FFmpeg 视频合成：逐句裁切/拼接、配音、BGM、字幕和导出。

FFmpeg 9 的构建移除了 -filter_complex_script，因此这里按句生成短滤镜图，
再把逐句片段用 concat demuxer 送入最终混音/字幕步骤，避免超长命令行。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import AppConfig, ExportConfig, resolve_executable
from ..db import ProjectDB
from ..errors import ExportError, TaskCancelled
from ..media.ffmpeg import probe_audio_duration
from ..models import ExportResult, Shot, ShotMatchPlan, SpeechUnit, ShotSelection, Voiceover, format_clock
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import ProgressUpdate
from ..utils import utc_now
from .subtitles import write_subtitles

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[ProgressUpdate], None]
LogFn = Callable[[str], None]


class VideoExporter:
    """把当前文案、配音、镜头方案合成为 MP4。"""

    def __init__(self, db: ProjectDB, config: AppConfig):
        self.db = db
        self.config = config
        self.ffmpeg = resolve_executable(config.ffmpeg_path, "ffmpeg")
        self.ffprobe = resolve_executable(config.ffprobe_path, "ffprobe")

    def export(
        self,
        project_id: str,
        *,
        export_config: ExportConfig | None = None,
        control: TaskControl | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
    ) -> ExportResult:
        control = control or TaskControl()
        cfg = (export_config or self.config.export).model_copy(deep=True)
        cfg.width = max(2, int(cfg.width) - int(cfg.width) % 2)
        cfg.height = max(2, int(cfg.height) - int(cfg.height) % 2)
        if cfg.width < 320 or cfg.height < 240:
            raise ExportError("导出分辨率过小")
        started = time.perf_counter()

        project = self.db.get_project(project_id)
        if project is None:
            raise ExportError(f"项目不存在：{project_id}")
        source = Path(project.source_path)
        if not source.exists():
            raise ExportError(f"源影片不存在：{source}")
        script = self.db.get_current_script(project_id)
        voiceover = self.db.get_current_voiceover(project_id)
        plan = self.db.get_current_shot_match(project_id)
        if script is None:
            raise ExportError("项目还没有文案。")
        if voiceover is None:
            raise ExportError("项目还没有配音。")
        if plan is None:
            raise ExportError("项目还没有镜头匹配方案。")
        if voiceover.script_id != script.id:
            raise ExportError("当前配音基于旧文案，请重新生成配音和镜头匹配。")
        if plan.voiceover_id != voiceover.id:
            raise ExportError("当前镜头方案基于旧配音，请重新自动匹配或手动确认。")
        if plan.script_id != script.id:
            raise ExportError("当前镜头方案基于旧文案，请重新匹配。")
        if not Path(voiceover.audio_path).exists():
            raise ExportError(f"配音文件不存在：{voiceover.audio_path}")
        if cfg.subtitle_mode == "burn" and not cfg.font_name.strip():
            cfg.font_name = "Microsoft YaHei"
        bgm = Path(cfg.bgm_path).expanduser() if cfg.bgm_path.strip() else None
        if bgm is not None and not bgm.is_file():
            raise ExportError(f"BGM 文件不存在：{bgm}")

        shots = {shot.index: shot for shot in self.db.get_shots(project_id)}
        if not shots:
            raise ExportError("项目没有镜头数据。")
        units = {unit.index: unit for unit in voiceover.units}
        selections = {item.sentence_index: item for item in plan.selections}
        missing = [index for index in units if index not in selections]
        if missing:
            raise ExportError(f"镜头方案缺少句子：{missing[:10]}")
        extra = [index for index in selections if index not in units]
        if extra:
            raise ExportError(f"镜头方案包含不属于当前配音的句子：{extra[:10]}")

        out_dir = (
            Path(cfg.output_dir).expanduser()
            if cfg.output_dir.strip()
            else Path(project.project_dir) / "exports"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        export_id = uuid.uuid4().hex[:8]
        base = out_dir / f"movie_{stamp}_{export_id}"
        output_path = base.with_suffix(".mp4")
        temp_dir = out_dir / f".segments_{export_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        srt_path, ass_path = write_subtitles(
            voiceover,
            out_dir,
            width=cfg.width,
            height=cfg.height,
            font_name=cfg.font_name,
            font_size=cfg.font_size,
            margin_v=cfg.margin_v,
        )
        batch_srt = base.with_suffix(".srt")
        batch_ass = base.with_suffix(".ass")
        srt_path.replace(batch_srt)
        ass_path.replace(batch_ass)
        srt_path, ass_path = batch_srt, batch_ass

        warnings: list[str] = []
        run_id: int | None = None
        try:
            total, pieces = self._render_segments(
                source,
                voiceover,
                plan,
                shots,
                units,
                selections,
                cfg,
                temp_dir,
                control,
                on_progress,
                on_log,
                warnings,
            )
            run_id = self.db.add_run(project_id, "export")
            concat_file = temp_dir / "concat.txt"
            lines = []
            for index in units:
                segment = temp_dir / f"segment_{index:04d}.mp4"
                if not segment.exists() or segment.stat().st_size < 512:
                    raise ExportError(f"第 {index} 句视频片段生成失败")
                safe_path = str(segment.resolve()).replace("\\", "/").replace("'", "'\\''")
                lines.append(f"file '{safe_path}'")
            concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            filter_graph, video_label, audio_label = self._final_filters(
                cfg, ass_path, bgm is not None
            )
            command = self._final_command(
                concat_file,
                Path(voiceover.audio_path),
                bgm,
                srt_path,
                output_path,
                cfg,
                total,
                filter_graph,
                video_label,
                audio_label,
            )
            self._emit(
                on_progress,
                65,
                "合成成片",
                f"{cfg.width}×{cfg.height} / {cfg.fps}fps / {pieces} 个镜头片段",
            )
            self._log(on_log, f"逐句片段 {len(units)} 个，镜头片段 {pieces} 个，开始最终合成")
            self._run_ffmpeg(
                command,
                total,
                control,
                on_progress,
                on_log,
                output_path,
                percent_start=65,
                percent_end=97,
            )
            if not output_path.exists() or output_path.stat().st_size < 1024:
                raise ExportError("FFmpeg 未生成有效成片")
            duration = probe_audio_duration(self.ffprobe, output_path)
            if abs(duration - total) > 1.0:
                warnings.append(f"成片时长 {duration:.2f}s 与配音 {total:.2f}s 略有差异。")
            result = ExportResult(
                project_id=project_id,
                plan_id=plan.id,
                voiceover_id=voiceover.id,
                created_at=utc_now(),
                output_path=str(output_path.resolve()),
                srt_path=str(srt_path.resolve()),
                ass_path=str(ass_path.resolve()),
                width=cfg.width,
                height=cfg.height,
                fps=cfg.fps,
                duration_s=round(duration, 3),
                size_bytes=output_path.stat().st_size,
                subtitle_mode=cfg.subtitle_mode,
                bgm_used=bgm is not None,
                elapsed_s=round(time.perf_counter() - started, 2),
                warnings=warnings,
            )
            self.db.save_export(result)
            self.db.finish_run(
                run_id,
                "completed",
                f"导出 {format_clock(total)} / {result.size_label} / {cfg.width}×{cfg.height}",
            )
            self._emit(on_progress, 100, "完成", f"成片已导出：{format_clock(total)}")
            LOGGER.info(
                "导出完成：project=%s %sx%s %.1fs size=%d",
                project_id,
                cfg.width,
                cfg.height,
                duration,
                result.size_bytes,
            )
            return result
        except TaskCancelled:
            output_path.unlink(missing_ok=True)
            if run_id is not None:
                try:
                    self.db.finish_run(run_id, "cancelled", "用户取消导出")
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception:
            output_path.unlink(missing_ok=True)
            if run_id is not None:
                try:
                    self.db.finish_run(run_id, "failed", "导出失败")
                except Exception:  # noqa: BLE001
                    pass
            LOGGER.exception("导出失败")
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 逐句片段
    def _render_segments(
        self,
        source: Path,
        voiceover: Voiceover,
        plan: ShotMatchPlan,
        shots: dict[int, Shot],
        units: dict[int, SpeechUnit],
        selections: dict[int, ShotSelection],
        cfg: ExportConfig,
        temp_dir: Path,
        control: TaskControl,
        on_progress: ProgressFn | None,
        on_log: LogFn | None,
        warnings: list[str],
    ) -> tuple[float, int]:
        total = 0.0
        pieces = 0
        ordered = list(units.values())
        audio_total = float(voiceover.total_duration_s) or max(
            (float(unit.end_s) for unit in ordered), default=0.0
        )
        count = len(ordered)
        for position, unit in enumerate(ordered, start=1):
            control.checkpoint()
            index = unit.index
            # 视频槽位包含该句后的停顿，冻结最后一帧到下一句开始，保证与完整配音同长
            slot_end = (
                float(ordered[position].start_s)
                if position < len(ordered)
                else max(float(unit.end_s), audio_total)
            )
            target = max(0.1, slot_end - float(unit.start_s))
            total += target
            graph, piece_count = self._segment_graph(
                selections[index], shots, target, cfg, index, warnings
            )
            pieces += piece_count
            segment = temp_dir / f"segment_{index:04d}.mp4"
            command = [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-filter_complex",
                graph,
                "-map",
                "[vseg]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                cfg.preset,
                "-crf",
                str(cfg.crf),
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(cfg.fps),
                "-t",
                f"{target:.3f}",
                str(segment),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise ExportError(
                    f"第 {index} 句视频片段失败：{(completed.stderr or '')[-1000:]}"
                )
            if not segment.exists() or segment.stat().st_size < 512:
                raise ExportError(f"第 {index} 句视频片段为空")
            percent = 5 + int(55 * position / max(1, count))
            self._emit(on_progress, percent, "裁切镜头", f"已完成 {position}/{count} 句")
            self._log(on_log, f"第 {index} 句：{piece_count} 个镜头片段")
        return total, pieces

    def _segment_graph(
        self,
        selection: ShotSelection,
        shots: dict[int, Shot],
        target: float,
        cfg: ExportConfig,
        sentence_index: int,
        warnings: list[str],
    ) -> tuple[str, int]:
        filters: list[str] = []
        labels: list[str] = []
        used = 0.0
        for shot_id in selection.shot_ids:
            if used >= target - 0.02 or len(labels) >= 12:
                break
            shot = shots.get(shot_id)
            if shot is None:
                warnings.append(f"第 {sentence_index} 句引用了不存在的镜头 {shot_id}，已跳过。")
                continue
            take = min(shot.duration_s, target - used)
            if take <= 0.02:
                continue
            start = shot.start_s
            end = min(shot.end_s, shot.start_s + take)
            if end <= start + 0.01:
                continue
            label = f"p{len(labels)}"
            if cfg.fit_mode == "crop":
                fit = (
                    f"scale={cfg.width}:{cfg.height}:force_original_aspect_ratio=increase,"
                    f"crop={cfg.width}:{cfg.height}"
                )
            else:
                fit = (
                    f"scale={cfg.width}:{cfg.height}:force_original_aspect_ratio=decrease,"
                    f"pad={cfg.width}:{cfg.height}:(ow-iw)/2:(oh-ih)/2:color=black"
                )
            filters.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},"
                f"setpts=PTS-STARTPTS,{fit},"
                f"setsar=1,fps={cfg.fps},format=yuv420p[{label}]"
            )
            labels.append(label)
            used += take
        if not labels:
            filters.append(
                f"color=c=black:s={cfg.width}x{cfg.height}:r={cfg.fps}:d={target:.3f},"
                f"format=yuv420p[p0]"
            )
            labels.append("p0")
            warnings.append(f"第 {sentence_index} 句没有可用镜头，使用黑场填充。")
        if len(labels) == 1:
            cat = labels[0]
        else:
            cat = "vcat"
            filters.append(
                "".join(f"[{label}]" for label in labels)
                + f"concat=n={len(labels)}:v=1:a=0[{cat}]"
            )
        filters.append(
            f"[{cat}]tpad=stop_mode=clone:stop_duration={target + 0.5:.3f},"
            f"trim=duration={target:.3f},setpts=PTS-STARTPTS,"
            f"fps={cfg.fps},format=yuv420p[vseg]"
        )
        return ";\n".join(filters), len(labels)

    # ------------------------------------------------------------------ 最终合成
    def _final_filters(
        self, cfg: ExportConfig, ass_path: Path, has_bgm: bool
    ) -> tuple[str, str, str]:
        filters: list[str] = []
        video_label = "0:v"
        if cfg.subtitle_mode == "burn":
            escaped = self._escape_filter_path(ass_path)
            filters.append(f"[0:v]ass=filename='{escaped}'[vout]")
            video_label = "vout"
        if has_bgm:
            if cfg.bgm_ducking:
                filters.append(
                    "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    "asplit=2[voice][side]"
                )
            else:
                filters.append(
                    "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[voice]"
                )
            filters.append(
                f"[2:a]volume={cfg.bgm_volume_db:.1f}dB,"
                "aformat=sample_rates=48000:channel_layouts=stereo[bgm]"
            )
            if cfg.bgm_ducking:
                # voice 作为 sidechain 控制 BGM 压缩；voice 本身保持原音量
                filters.append(
                    "[bgm][side]sidechaincompress=threshold=0.03:ratio=8:"
                    "attack=20:release=300[bgmduck]"
                )
                filters.append(
                    "[voice][bgmduck]amix=inputs=2:duration=first:dropout_transition=2:"
                    "normalize=0[aout]"
                )
            else:
                filters.append(
                    "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2:"
                    "normalize=0[aout]"
                )
            audio_label = "aout"
        else:
            filters.append("[1:a]aformat=sample_rates=48000:channel_layouts=stereo[voice]")
            filters.append("[voice]anull[aout]")
            audio_label = "aout"
        return ";\n".join(filters), video_label, audio_label

    def _final_command(
        self,
        concat_file: Path,
        voiceover: Path,
        bgm: Path | None,
        srt_path: Path,
        output: Path,
        cfg: ExportConfig,
        total: float,
        filter_graph: str,
        video_label: str,
        audio_label: str,
    ) -> list[str]:
        command = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(voiceover),
        ]
        if bgm is not None:
            command.extend(["-stream_loop", "-1", "-i", str(bgm)])
        subtitle_input = None
        if cfg.subtitle_mode == "soft":
            subtitle_input = 3 if bgm is not None else 2
            command.extend(["-i", str(srt_path)])
        video_map = f"[{video_label}]" if video_label != "0:v" else "0:v"
        command.extend(["-filter_complex", filter_graph, "-map", video_map, "-map", f"[{audio_label}]"])
        if subtitle_input is not None:
            command.extend(
                [
                    "-map",
                    f"{subtitle_input}:s",
                    "-c:s",
                    "mov_text",
                    "-metadata:s:s:0",
                    "language=chi",
                    "-disposition:s:0",
                    "default",
                ]
            )
        if cfg.subtitle_mode == "burn":
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    cfg.preset,
                    "-crf",
                    str(cfg.crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    str(cfg.fps),
                ]
            )
        else:
            command.extend(["-c:v", "copy"])
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                cfg.audio_bitrate,
                "-movflags",
                "+faststart",
                "-t",
                f"{total:.3f}",
                str(output),
            ]
        )
        return command

    # ------------------------------------------------------------------ 执行
    def _run_ffmpeg(
        self,
        command: list[str],
        total: float,
        control: TaskControl,
        on_progress: ProgressFn | None,
        on_log: LogFn | None,
        output: Path,
        *,
        percent_start: int,
        percent_end: int,
    ) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        cancelled = False
        last_report = -10
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if control.cancelled:
                    cancelled = True
                    break
                line = line.strip()
                if not line:
                    continue
                seconds: float | None = None
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        seconds = None
                elif line.startswith("out_time_ms="):
                    # FFmpeg 9 的 out_time_ms 实际也是微秒；统一以 out_time_us 为准
                    continue
                elif line.startswith("out_time="):
                    try:
                        h, m, s = line.split("=", 1)[1].split(":")
                        seconds = int(h) * 3600 + int(m) * 60 + float(s)
                    except ValueError:
                        seconds = None
                if seconds is not None:
                    ratio = min(1.0, max(0.0, seconds / max(total, 0.1)))
                    percent = min(percent_end, max(percent_start, int(percent_start + (percent_end - percent_start) * ratio)))
                    if percent >= last_report + 2:
                        last_report = percent
                        self._emit(
                            on_progress,
                            percent,
                            "合成视频",
                            f"{format_clock(seconds)} / {format_clock(total)}",
                        )
                        self._log(on_log, f"编码进度 {percent}%")
            if cancelled:
                self._terminate(process)
                raise TaskCancelled("用户取消导出")
            process.wait(timeout=30)
            stderr = (process.stderr.read() if process.stderr else "") or ""
            if process.returncode != 0:
                raise ExportError(f"FFmpeg 导出失败（code {process.returncode}）：{stderr[-1200:]}")
            if not output.exists():
                raise ExportError("FFmpeg 返回成功但没有输出文件。")
        finally:
            if process.poll() is None:
                self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _escape_filter_path(path: Path) -> str:
        value = str(path.resolve()).replace("\\", "/")
        value = value.replace(":", "\\:").replace("'", "\\'")
        return value

    @staticmethod
    def _emit(on_progress: ProgressFn | None, percent: int, stage: str, message: str) -> None:
        if on_progress is not None:
            on_progress(ProgressUpdate(percent=percent, stage=stage, message=message))

    @staticmethod
    def _log(on_log: LogFn | None, message: str) -> None:
        if on_log is not None:
            on_log(message)
