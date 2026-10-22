"""命令行入口（无需 Qt，可用于无界面分析、测试与批处理）。

用法示例::

    python -m aimovie.cli info D:\\movies\\demo.mp4
    python -m aimovie.cli analyze D:\\movies\\demo.mp4
    python -m aimovie.cli analyze demo.mp4 --skip-speech
    python -m aimovie.cli projects
    python -m aimovie.cli export <PROJECT_ID> --out timeline.json
    python -m aimovie.cli tts <PROJECT_ID> --engine edge --out voiceover.wav
    python -m aimovie.cli match <PROJECT_ID> --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
from datetime import datetime
from pathlib import Path

from . import APP_NAME, __version__
from .analysis.story import StoryAnalyzer, StoryRunResult
from .config import (
    AppConfig,
    ensure_data_dirs,
    has_deepseek_api_key,
    load_config,
    resolve_executable,
)
from .copywriting.generator import ScriptGenerator, ScriptRunResult
from .copywriting.styles import STYLES, get_style
from .costs.service import CostService
from .db import ProjectDB
from .errors import AIMovieError, TaskCancelled
from .export.video import VideoExporter
from .logging_setup import setup_logging
from .matching.matcher import ShotMatcher
from .media.probe import probe_media
from .models import ProjectStatus
from .tts.service import TTSService
from .pipeline.control import TaskControl
from .pipeline.phase1 import AnalysisPipeline, ProgressUpdate


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aimovie",
        description=f"{APP_NAME} v{__version__} 命令行工具（本地分析 + DeepSeek 剧情/文案 + 配音/匹配 + 剪辑导出）",
    )
    parser.add_argument("--data-dir", type=Path, help="覆盖数据目录")
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    info = subparsers.add_parser("info", help="读取并显示影片技术信息")
    info.add_argument("video", type=Path)
    info.add_argument("--json", action="store_true", help="以 JSON 输出")

    analyze = subparsers.add_parser("analyze", help="新建项目并执行第一阶段分析")
    analyze.add_argument("video", type=Path)
    analyze.add_argument("--name", help="项目名称，默认使用文件名")
    analyze.add_argument("--skip-speech", action="store_true", help="跳过语音识别")
    analyze.add_argument("--whisper-model", help="覆盖 Whisper 模型，例如 tiny/small")
    analyze.add_argument(
        "--whisper-language",
        choices=["zh", "en", "ja", "auto"],
        help="覆盖识别语言；auto 表示自动检测",
    )
    analyze.add_argument("--scene-backend", choices=["auto", "pyscenedetect", "ffmpeg"], help="镜头检测后端")
    analyze.add_argument("--no-keyframes", action="store_true", help="不生成镜头缩略图")
    analyze.add_argument("--out", type=Path, help="额外把时间轴复制到指定 JSON 路径")

    projects = subparsers.add_parser("projects", help="列出项目")
    projects.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export", help="导出项目时间轴 JSON")
    export.add_argument("project_id")
    export.add_argument("--out", type=Path, required=True)

    story = subparsers.add_parser("story", help="运行 DeepSeek 分段剧情分析（会产生 API 费用）")
    story.add_argument("project_id")
    story.add_argument("--allow-over-budget", action="store_true", help="明确允许本次预计超出预算")
    story.add_argument("--json", action="store_true", help="输出完整分析 JSON")

    script = subparsers.add_parser("script", help="生成解说文案（会产生 API 费用）")
    script.add_argument("project_id")
    script.add_argument("--style", default="quick3", choices=[item.id for item in STYLES])
    script.add_argument("--minutes", type=float, help="目标时长（分钟）")
    script.add_argument("--custom-name", default="", help="自定义风格名称")
    script.add_argument("--custom-instruction", default="", help="自定义风格要求")
    script.add_argument("--allow-over-budget", action="store_true", help="明确允许本次预计超出预算")
    script.add_argument("--out", type=Path, help="把文案保存为文本文件")

    scripts = subparsers.add_parser("scripts", help="列出项目的文案版本")
    scripts.add_argument("project_id")
    scripts.add_argument("--json", action="store_true")

    costs = subparsers.add_parser("costs", help="查看 API 费用统计")
    costs.add_argument("project_id", nargs="?", help="项目 ID 或前缀；省略表示全局")
    costs.add_argument("--limit", type=int, default=20)

    tts = subparsers.add_parser("tts", help="逐句生成中文配音与句子时间轴（免费，需网络或本地模型）")
    tts.add_argument("project_id")
    tts.add_argument("--engine", choices=["auto", "edge", "piper"], help="覆盖配音引擎")
    tts.add_argument("--voice", help="覆盖 Edge 中文声音")
    tts.add_argument("--out", type=Path, help="额外复制完整配音 WAV 到指定路径")

    match = subparsers.add_parser("match", help="根据剧情片段/镜头时长/连续性自动匹配镜头")
    match.add_argument("project_id")
    match.add_argument("--json", action="store_true", help="输出完整匹配 JSON")

    video = subparsers.add_parser("export-video", help="按当前配音/镜头方案合成 MP4 成片")
    video.add_argument("project_id")
    video.add_argument("--out-dir", type=Path, help="导出目录，默认项目 exports/")
    video.add_argument("--width", type=int, help="画面宽，默认 1920")
    video.add_argument("--height", type=int, help="画面高，默认 1080")
    video.add_argument("--fps", type=int, help="帧率，默认 30")
    video.add_argument("--crf", type=int, help="H.264 CRF，默认 20；越大越省空间")
    video.add_argument("--preset", help="x264 preset，例如 veryfast/medium/slow")
    video.add_argument("--subtitle", choices=["burn", "none"], help="烧录字幕 / 无字幕")
    video.add_argument("--bgm", type=Path, help="可选 BGM 音频文件")
    video.add_argument("--bgm-volume-db", type=float, help="BGM 音量 dB，默认 -20")
    return parser


def _prepare_config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    if args.data_dir:
        config.data_dir = str(args.data_dir.expanduser().resolve())
    ensure_data_dirs(config)
    return config


def _project_id_from_prefix(db: ProjectDB, value: str) -> str:
    projects = db.list_projects()
    matches = [project for project in projects if project.id == value or project.id.startswith(value)]
    if not matches:
        raise SystemExit(f"找不到项目：{value}")
    if len(matches) > 1:
        options = "\n".join(f"  {project.id}  {project.name}" for project in matches)
        raise SystemExit(f"项目 ID 前缀不唯一，请提供更长的 ID：\n{options}")
    return matches[0].id


def _cmd_info(args: argparse.Namespace, config: AppConfig) -> int:
    try:
        ffprobe = resolve_executable(config.ffprobe_path, "ffprobe")
        media = probe_media(args.video, ffprobe_path=ffprobe)
    except (AIMovieError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(media.model_dump_json(indent=2))
        return 0
    print(f"文件：{media.source_path}")
    print(f"时长：{media.duration_label}（{media.duration_s:.3f} 秒）")
    print(f"画面：{media.resolution_label} @ {media.fps_label} fps，编码 {media.video_codec or '未知'}")
    print(f"音频：{media.audio_label}")
    print(f"容器：{media.format_name or '未知'}，大小 {media.size_bytes or 0} 字节")
    print(f"字幕轨：{len(media.subtitle_streams)} 条")
    for stream in media.subtitle_streams:
        print(f"  - #{stream.index} {stream.codec} {stream.language or ''} {stream.title or ''}".rstrip())
    return 0


def _cmd_analyze(args: argparse.Namespace, config: AppConfig) -> int:
    source = args.video.expanduser()
    if not source.exists():
        print(f"错误：影片不存在：{source}", file=sys.stderr)
        return 1
    if args.whisper_model:
        config.whisper.model_size = args.whisper_model
    if args.whisper_language:
        config.whisper.language = "" if args.whisper_language == "auto" else args.whisper_language
    if args.scene_backend:
        config.scene.backend = args.scene_backend
    if args.no_keyframes:
        config.keyframes.enabled = False

    db = ProjectDB(config.resolved_data_dir() / "app.db")
    safe_stem = "".join(
        character for character in source.stem if character not in '<>:"/\\|?*' and character.isprintable()
    )[:24] or "movie"
    project_dir = (
        config.projects_dir()
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_stem}"
    )
    project = db.create_project(args.name or source.stem, source, project_dir)
    print(f"项目：{project.name}  (ID: {project.id})")
    print(f"项目目录：{project.project_dir}")

    control = TaskControl()
    last_percent = {"value": -1}

    def on_progress(update: ProgressUpdate) -> None:
        if update.percent != last_percent["value"] or update.stage == "finish":
            last_percent["value"] = update.percent
            print(f"[{update.percent:3d}%] {update.stage}: {update.message}", flush=True)

    def on_log(message: str) -> None:
        print(f"  · {message}", flush=True)

    previous_handler = None
    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _signum, _frame: (print("\n收到中断，正在取消…", flush=True), control.cancel()))
    except (ValueError, OSError):
        previous_handler = None

    pipeline = AnalysisPipeline(db, config)
    try:
        result = pipeline.run(
            project.id,
            control=control,
            on_progress=on_progress,
            on_log=on_log,
            skip_speech=args.skip_speech,
        )
    except TaskCancelled:
        print("\n任务已取消。已保存的部分结果可在项目中查看。", file=sys.stderr)
        return 130
    except (AIMovieError, FileNotFoundError, OSError) as exc:
        print(f"\n分析失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n分析失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if previous_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_handler)
            except (ValueError, OSError):
                pass

    if args.out:
        db.export_timeline(project.id, args.out)
        print(f"时间轴副本：{args.out}")
    summary = {
        "project_id": result.project_id,
        "project_name": project.name,
        "project_dir": project.project_dir,
        "timeline": result.timeline_path,
        "shot_count": result.shot_count,
        "dialogue_count": result.dialogue_count,
        "scene_backend": result.scene_backend,
        "speech_engine": result.speech_engine,
        "elapsed_s": result.elapsed_s,
        "warnings": result.warnings,
    }
    print("\n分析完成：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_projects(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    projects = db.list_projects()
    if args.json:
        print(json.dumps([project.model_dump(mode="json") for project in projects], ensure_ascii=False, indent=2))
        return 0
    if not projects:
        print("暂无项目。")
        return 0
    print(f"{'ID':16}  {'状态':8}  {'镜头':>5}  {'对白':>5}  名称")
    for project in projects:
        print(
            f"{project.id:16}  {project.status_label:8}  "
            f"{project.shot_count:5d}  {project.dialogue_count:5d}  {project.name}"
        )
    return 0


def _cmd_export(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    try:
        path = db.export_timeline(project_id, args.out)
    except (KeyError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已导出：{path}")
    return 0


def _install_sigint(control: TaskControl):
    def handler(_signum, _frame):  # noqa: ANN001
        print("\n收到中断，正在取消…", file=sys.stderr, flush=True)
        control.cancel()

    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handler)
        return previous
    except (ValueError, OSError):
        return None


def _restore_sigint(previous) -> None:  # noqa: ANN001
    if previous is None:
        return
    try:
        signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError):
        pass


def _print_progress(update: ProgressUpdate) -> None:
    print(f"[{update.percent:3d}%] {update.stage}: {update.message}", flush=True)


def _require_api_key(config: AppConfig) -> bool:
    if has_deepseek_api_key(config):
        return True
    print(
        f"未配置 DeepSeek API Key。请设置环境变量 {config.deepseek.api_key_env}；"
        "Key 只从环境变量读取，不会写入配置或日志。",
        file=sys.stderr,
    )
    return False


def _confirm_budget_cli(
    config: AppConfig,
    db: ProjectDB,
    project_id: str,
    estimated_cny: float,
    allow_over_budget: bool,
) -> bool:
    check = CostService(config, db).check_budget(project_id, estimated_cny)
    if not check.requires_confirmation:
        return True
    snapshot = check.snapshot
    if not allow_over_budget:
        print(
            f"预计本次费用约 ¥{estimated_cny:.4f}，项目已用 ¥{snapshot.spent_cny:.4f}，"
            f"预算上限 ¥{snapshot.budget_cny:.2f}。为避免超支已停止；"
            "确认后可加 --allow-over-budget 再执行。",
            file=sys.stderr,
        )
        return False
    print(
        f"警告：已确认可能超出预算（预计 ¥{estimated_cny:.4f}，已用 ¥{snapshot.spent_cny:.4f}，"
        f"预算 ¥{snapshot.budget_cny:.2f}）。",
        file=sys.stderr,
    )
    return True


def _cmd_story(args: argparse.Namespace, config: AppConfig) -> int:
    if not _require_api_key(config):
        return 2
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    dialogues = db.get_dialogues(project_id)
    estimate = CostService(config, db).estimate_story_cost(dialogues)
    if not _confirm_budget_cli(
        config, db, project_id, estimate, bool(getattr(args, "allow_over_budget", False))
    ):
        return 2
    control = TaskControl()
    previous = _install_sigint(control)
    analyzer = StoryAnalyzer(db, config)
    try:
        result: StoryRunResult = analyzer.run(
            project_id,
            control=control,
            on_progress=_print_progress,
            on_log=lambda message: print(f"  · {message}", flush=True),
            allow_over_budget=bool(args.allow_over_budget),
        )
    except TaskCancelled:
        print("任务已取消。", file=sys.stderr)
        return 130
    except (AIMovieError, OSError) as exc:
        print(f"剧情分析失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"剧情分析失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        _restore_sigint(previous)
    if args.json:
        print(result.analysis.to_json())
        return 0
    analysis = result.analysis
    print(f"\n剧情分析完成：{analysis.segment_count} 段，{len(analysis.characters)} 个人物，"
          f"{len(analysis.turning_points)} 个转折，费用约 ¥{result.cost_cny:.4f}")
    print(f"标题：{analysis.title}")
    print(f"梗概：{analysis.logline}")
    for segment in analysis.segments:
        print(f"  第 {segment.index} 段 [{segment.time_label}] {segment.summary[:80]}")
    if analysis.warnings:
        print("注意事项：")
        for warning in analysis.warnings[:10]:
            print(f"  · {warning}")
    return 0


def _cmd_script(args: argparse.Namespace, config: AppConfig) -> int:
    if not _require_api_key(config):
        return 2
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    analysis = db.get_story_analysis(project_id)
    if analysis is None:
        print("项目还没有剧情分析结果，请先运行 story 命令。", file=sys.stderr)
        return 1
    style = get_style(args.style)
    minutes = float(args.minutes or style.default_minutes)
    estimate = CostService(config, db).estimate_script_cost(analysis, minutes)
    if not _confirm_budget_cli(
        config, db, project_id, estimate, bool(getattr(args, "allow_over_budget", False))
    ):
        return 2
    control = TaskControl()
    previous = _install_sigint(control)
    generator = ScriptGenerator(db, config)
    try:
        result: ScriptRunResult = generator.run(
            project_id,
            args.style,
            minutes=minutes,
            custom_name=args.custom_name,
            custom_instruction=args.custom_instruction,
            control=control,
            on_progress=_print_progress,
            on_log=lambda message: print(f"  · {message}", flush=True),
            allow_over_budget=bool(args.allow_over_budget),
        )
    except TaskCancelled:
        print("任务已取消。", file=sys.stderr)
        return 130
    except (AIMovieError, OSError) as exc:
        print(f"文案生成失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"文案生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        _restore_sigint(previous)
    script = result.script
    print(f"\n文案 v{script.version}：{script.title}")
    print(f"字数：{script.character_count} / 目标 {script.target_chars}；"
          f"费用约 ¥{result.cost_cny:.4f}" + ("（缓存命中）" if result.cached else ""))
    print(script.full_text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(f"{script.title}\n\n{script.full_text}\n", encoding="utf-8")
        print(f"\n已保存：{args.out}")
    return 0


def _cmd_scripts(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    versions = db.list_scripts(project_id)
    if args.json:
        print(json.dumps([item.model_dump(mode="json") for item in versions], ensure_ascii=False, indent=2))
        return 0
    if not versions:
        print("该项目暂无文案版本。")
        return 0
    for item in versions:
        marker = "*" if item.is_current else " "
        print(f"{marker} v{item.version:<3} {item.style_name:<12} {item.total_chars:>5} 字 "
              f"{item.source:<12} ¥{item.cost_cny:.4f}  {item.title}")
    return 0


def _cmd_tts(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    if args.engine:
        config.tts.engine = args.engine
    if args.voice:
        config.tts.edge_voice = args.voice
    last_percent = [-10]

    def _progress(update: ProgressUpdate) -> None:
        if update.percent >= last_percent[0] + 5 or update.percent >= 100:
            last_percent[0] = update.percent
            print(f"[{update.percent:>3}%] {update.stage}: {update.message}", flush=True)

    try:
        service = TTSService(db, config)
        voiceover = service.synthesize(project_id, on_progress=_progress)
    except (AIMovieError, TaskCancelled, FileNotFoundError) as exc:
        print(f"配音失败：{exc}", file=sys.stderr)
        return 1
    print(f"配音完成：{voiceover.unit_count} 句，总时长 {voiceover.duration_label}")
    print(f"引擎：{voiceover.engine}；声音：{voiceover.voice}")
    print(f"音频：{voiceover.audio_path}")
    print(f"缓存：{'是' if voiceover.cached else '否'}；耗时 {voiceover.elapsed_s:.1f} 秒")
    if voiceover.warnings:
        print("注意：")
        for warning in voiceover.warnings:
            print(f"  · {warning}")
    if args.out:
        target = args.out.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(voiceover.audio_path, target)
        print(f"已复制：{target}")
    return 0


def _cmd_match(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    try:
        plan = ShotMatcher(db, config).match(project_id)
    except (AIMovieError, FileNotFoundError) as exc:
        print(f"镜头匹配失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(plan.model_dump_json(indent=2))
        return 0
    print(
        f"镜头匹配：{plan.selection_count} 句，平均置信度 {plan.average_confidence:.2f}，"
        f"{plan.low_confidence_count} 句需审核"
    )
    print(f"配音时长：{plan.total_duration_s:.1f} 秒")
    for selection in plan.selections:
        flag = "需审核" if selection.needs_review else "通过"
        shots = ",".join(str(item) for item in selection.shot_ids) or "—"
        print(
            f"  {selection.sentence_index:>2}. [{flag}] 镜头 {shots:<18} "
            f"覆盖 {selection.coverage_s:.2f}/{selection.target_duration_s:.2f}s "
            f"置信度 {selection.confidence:.2f}"
        )
    if plan.warnings:
        print("注意：")
        for warning in plan.warnings:
            print(f"  · {warning}")
    return 0


def _cmd_export_video(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = _project_id_from_prefix(db, args.project_id)
    cfg = config.export.model_copy(deep=True)
    if args.out_dir:
        cfg.output_dir = str(args.out_dir.expanduser().resolve())
    if args.width is not None:
        cfg.width = args.width
    if args.height is not None:
        cfg.height = args.height
    if args.fps is not None:
        cfg.fps = args.fps
    if args.crf is not None:
        cfg.crf = args.crf
    if args.preset:
        cfg.preset = args.preset
    if args.subtitle:
        cfg.subtitle_mode = args.subtitle
    if args.bgm:
        cfg.bgm_path = str(args.bgm.expanduser().resolve())
    if args.bgm_volume_db is not None:
        cfg.bgm_volume_db = args.bgm_volume_db
    try:
        exporter = VideoExporter(db, config)
        result = exporter.export(
            project_id,
            export_config=cfg,
            on_progress=lambda update: print(
                f"[{update.percent:>3}%] {update.stage}: {update.message}", flush=True
            ),
        )
    except (AIMovieError, TaskCancelled, FileNotFoundError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    print(f"成片：{result.output_path}")
    print(f"时长：{result.duration_label}；大小：{result.size_label}")
    print(f"画面：{result.width}×{result.height} @ {result.fps}fps；字幕：{result.subtitle_mode}")
    print(f"字幕文件：{result.srt_path}")
    if result.warnings:
        print("注意：")
        for warning in result.warnings:
            print(f"  · {warning}")
    return 0


def _cmd_costs(args: argparse.Namespace, config: AppConfig) -> int:
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project_id = None
    if args.project_id:
        project_id = _project_id_from_prefix(db, args.project_id)
    service = CostService(config, db)
    summary = service.snapshot(project_id)
    print(f"范围：{'项目 ' + project_id if project_id else '全局'}")
    print(f"预算：{'不限制' if summary.budget_cny <= 0 else f'¥{summary.budget_cny:.2f}'}")
    print(f"已用：¥{summary.spent_cny:.4f}")
    if summary.remaining_cny is not None:
        print(f"剩余：¥{summary.remaining_cny:.4f}")
    print(f"调用：{summary.calls} 次（缓存 {summary.cached_calls}，失败 {summary.failed_calls}）")
    print(f"Token：输入 {summary.prompt_tokens} / 输出 {summary.completion_tokens}")
    calls = service.recent_calls(project_id, limit=max(1, args.limit))
    if calls:
        print("\n最近调用：")
        for call in calls:
            print(
                f"  {call.started_at.astimezone().strftime('%m-%d %H:%M:%S')} "
                f"{call.purpose:<22} {call.model:<16} "
                f"{call.prompt_tokens}+{call.completion_tokens} tok "
                f"¥{call.cost_cny:.4f} {'缓存' if call.cached else call.status}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = _prepare_config(args)
        log_path = setup_logging(config.logs_dir(), config.log_level, console=False)
        print(f"日志：{log_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1

    if args.command == "info":
        return _cmd_info(args, config)
    if args.command == "analyze":
        return _cmd_analyze(args, config)
    if args.command == "projects":
        return _cmd_projects(args, config)
    if args.command == "export":
        return _cmd_export(args, config)
    if args.command == "story":
        return _cmd_story(args, config)
    if args.command == "script":
        return _cmd_script(args, config)
    if args.command == "scripts":
        return _cmd_scripts(args, config)
    if args.command == "tts":
        return _cmd_tts(args, config)
    if args.command == "match":
        return _cmd_match(args, config)
    if args.command == "export-video":
        return _cmd_export_video(args, config)
    if args.command == "costs":
        return _cmd_costs(args, config)
    parser.error("未知命令")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
