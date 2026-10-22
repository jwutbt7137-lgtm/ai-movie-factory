"""SQLite 项目数据库。

每个方法使用独立连接，配合 WAL 模式，允许后台分析线程与界面同时访问。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import (
    AnalysisTimeline,
    ApiCallRecord,
    CostSummary,
    DialogueSegment,
    ExportResult,
    MediaInfo,
    ProjectRecord,
    ProjectStatus,
    Script,
    ScriptSummary,
    Shot,
    ShotMatchPlan,
    StoryAnalysis,
    Voiceover,
)
from .utils import utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    project_dir  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'imported',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    duration_s   REAL,
    width        INTEGER,
    height       INTEGER,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS media (
    project_id   TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shots (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    keyframe_path TEXT,
    PRIMARY KEY (project_id, idx)
);

CREATE TABLE IF NOT EXISTS dialogues (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    text         TEXT NOT NULL,
    language     TEXT NOT NULL DEFAULT '',
    avg_logprob  REAL,
    no_speech_prob REAL,
    PRIMARY KEY (project_id, idx)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    message      TEXT
);

CREATE TABLE IF NOT EXISTS story_analysis (
    project_id   TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    payload      TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    timeline_generated_at TEXT
);

CREATE TABLE IF NOT EXISTS scripts (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    style_id     TEXT NOT NULL DEFAULT '',
    style_name   TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    total_chars  INTEGER NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'ai',
    model        TEXT NOT NULL DEFAULT '',
    cost_cny     REAL NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    is_current   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, version)
);

CREATE TABLE IF NOT EXISTS api_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    purpose         TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_cny        REAL NOT NULL DEFAULT 0,
    cached          INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'success',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    error           TEXT,
    request_hash    TEXT NOT NULL DEFAULT '',
    tokens_estimated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS voiceovers (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    script_id    TEXT NOT NULL,
    script_version INTEGER NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL,
    audio_path   TEXT NOT NULL DEFAULT '',
    total_duration_s REAL NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    is_current   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shot_matches (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    script_id    TEXT NOT NULL,
    voiceover_id TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    is_current   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exports (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    plan_id      TEXT NOT NULL DEFAULT '',
    voiceover_id TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    output_path  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    is_current   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_cache (
    request_hash      TEXT PRIMARY KEY,
    response          TEXT NOT NULL,
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_scripts_project ON scripts(project_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_api_calls_project ON api_calls(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_api_calls_started ON api_calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_voiceovers_project ON voiceovers(project_id, is_current);
CREATE INDEX IF NOT EXISTS idx_shot_matches_project ON shot_matches(project_id, is_current);
CREATE INDEX IF NOT EXISTS idx_exports_project ON exports(project_id, is_current);
"""


class ProjectDB:
    """项目数据访问层。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # ------------------------------------------------------------------ 基础
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    # ------------------------------------------------------------------ 项目
    def create_project(self, name: str, source_path: Path | str, project_dir: Path | str) -> ProjectRecord:
        project_id = uuid.uuid4().hex[:16]
        now = utc_now()
        name = (name or "").strip() or Path(source_path).stem or "未命名项目"
        project_dir = Path(project_dir)
        project_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, name, source_path, project_dir, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    name,
                    str(Path(source_path).resolve()),
                    str(project_dir.resolve()),
                    str(ProjectStatus.IMPORTED),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        record = self.get_project(project_id)
        assert record is not None
        return record

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM shots s WHERE s.project_id = p.id) AS shot_count,
                       (SELECT COUNT(*) FROM dialogues d WHERE d.project_id = p.id) AS dialogue_count
                FROM projects p WHERE p.id = ?
                """,
                (project_id,),
            ).fetchone()
        return _row_to_project(row) if row else None

    def list_projects(self) -> list[ProjectRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM shots s WHERE s.project_id = p.id) AS shot_count,
                       (SELECT COUNT(*) FROM dialogues d WHERE d.project_id = p.id) AS dialogue_count
                FROM projects p
                ORDER BY p.updated_at DESC, p.created_at DESC
                """
            ).fetchall()
        return [_row_to_project(row) for row in rows]

    def update_project(self, project_id: str, **fields: object) -> None:
        allowed = {"name", "source_path", "status", "duration_s", "width", "height", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        updates["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [str(value) if isinstance(value, ProjectStatus) else value for value in updates.values()]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",  # noqa: S608 - 列名来自白名单
                [*values, project_id],
            )

    def delete_project(self, project_id: str) -> None:
        """仅删除数据库记录；项目目录的删除必须由界面显式确认后另行处理。"""
        with self._connect() as connection:
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ------------------------------------------------------------------ 媒体
    def save_media(self, project_id: str, media: MediaInfo) -> None:
        payload = media.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media(project_id, payload) VALUES (?, ?)
                ON CONFLICT(project_id) DO UPDATE SET payload = excluded.payload
                """,
                (project_id, payload),
            )
            connection.execute(
                "UPDATE projects SET duration_s = ?, width = ?, height = ?, updated_at = ? WHERE id = ?",
                (media.duration_s, media.width, media.height, utc_now().isoformat(), project_id),
            )

    def get_media(self, project_id: str) -> MediaInfo | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM media WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            return None
        return MediaInfo.model_validate(json.loads(row["payload"]))

    # ------------------------------------------------------------------ 镜头
    def replace_shots(self, project_id: str, shots: Sequence[Shot]) -> None:
        rows = [(project_id, shot.index, shot.start_s, shot.end_s, shot.keyframe_path) for shot in shots]
        with self._connect() as connection:
            connection.execute("DELETE FROM shots WHERE project_id = ?", (project_id,))
            connection.executemany(
                "INSERT INTO shots(project_id, idx, start_s, end_s, keyframe_path) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now().isoformat(), project_id))

    def update_shot_keyframes(self, project_id: str, keyframes: dict[int, str]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE shots SET keyframe_path = ? WHERE project_id = ? AND idx = ?",
                [(path, project_id, index) for index, path in keyframes.items()],
            )
            connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now().isoformat(), project_id))

    def get_shots(self, project_id: str) -> list[Shot]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT idx, start_s, end_s, keyframe_path FROM shots WHERE project_id = ? ORDER BY idx",
                (project_id,),
            ).fetchall()
        return [
            Shot(index=row["idx"], start_s=row["start_s"], end_s=row["end_s"], keyframe_path=row["keyframe_path"])
            for row in rows
        ]

    # ------------------------------------------------------------------ 对白
    def replace_dialogues(self, project_id: str, segments: Sequence[DialogueSegment]) -> None:
        rows = [
            (
                project_id,
                segment.index,
                segment.start_s,
                segment.end_s,
                segment.text,
                segment.language,
                segment.avg_logprob,
                segment.no_speech_prob,
            )
            for segment in segments
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM dialogues WHERE project_id = ?", (project_id,))
            connection.executemany(
                """
                INSERT INTO dialogues(project_id, idx, start_s, end_s, text, language, avg_logprob, no_speech_prob)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utc_now().isoformat(), project_id))

    def get_dialogues(self, project_id: str) -> list[DialogueSegment]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT idx, start_s, end_s, text, language, avg_logprob, no_speech_prob
                FROM dialogues WHERE project_id = ? ORDER BY idx
                """,
                (project_id,),
            ).fetchall()
        return [
            DialogueSegment(
                index=row["idx"],
                start_s=row["start_s"],
                end_s=row["end_s"],
                text=row["text"],
                language=row["language"] or "",
                avg_logprob=row["avg_logprob"],
                no_speech_prob=row["no_speech_prob"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 运行
    def add_run(self, project_id: str, stage: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runs(project_id, stage, status, started_at) VALUES (?, ?, 'running', ?)",
                (project_id, stage, utc_now().isoformat()),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, message: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, message = ? WHERE id = ?",
                (status, utc_now().isoformat(), message, run_id),
            )

    def recent_runs(self, project_id: str, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 剧情分析
    def save_story_analysis(self, analysis: StoryAnalysis) -> None:
        payload = analysis.to_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO story_analysis(project_id, payload, model, created_at, timeline_generated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    payload = excluded.payload,
                    model = excluded.model,
                    created_at = excluded.created_at,
                    timeline_generated_at = excluded.timeline_generated_at
                """,
                (
                    analysis.project_id,
                    payload,
                    analysis.model,
                    analysis.created_at.isoformat(),
                    analysis.timeline_generated_at.isoformat() if analysis.timeline_generated_at else None,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (utc_now().isoformat(), analysis.project_id),
            )

    def get_story_analysis(self, project_id: str) -> StoryAnalysis | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM story_analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return StoryAnalysis.model_validate_json(row["payload"])
        except ValueError:
            return None

    def delete_story_analysis(self, project_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM story_analysis WHERE project_id = ?", (project_id,))

    # ------------------------------------------------------------------ 文案版本
    def save_script_version(self, script: Script, *, make_current: bool = True) -> Script:
        """追加一个新版本；版本号由数据库递增分配，旧版本保持不变。"""
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM scripts WHERE project_id = ?",
                (script.project_id,),
            ).fetchone()
            script.version = int(row["max_version"]) + 1
            script.id = uuid.uuid4().hex[:16]
            script.created_at = now
            if make_current:
                connection.execute(
                    "UPDATE scripts SET is_current = 0 WHERE project_id = ?",
                    (script.project_id,),
                )
            connection.execute(
                """
                INSERT INTO scripts(
                    id, project_id, version, style_id, style_name, title, total_chars,
                    payload, source, model, cost_cny, needs_review, created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    script.id,
                    script.project_id,
                    script.version,
                    script.style_id,
                    script.style_name,
                    script.title,
                    script.character_count,
                    script.model_dump_json(),
                    script.source,
                    script.model,
                    script.cost_cny,
                    int(script.needs_review),
                    script.created_at.isoformat(),
                    int(bool(make_current)),
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (now.isoformat(), script.project_id),
            )
        return script

    def get_script(self, script_id: str) -> Script | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM scripts WHERE id = ?",
                (script_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return Script.model_validate_json(row["payload"])
        except ValueError:
            return None

    def get_current_script(self, project_id: str) -> Script | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM scripts WHERE project_id = ? AND is_current = 1 "
                "ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return Script.model_validate_json(row["payload"])
        except ValueError:
            return None

    def list_scripts(self, project_id: str) -> list[ScriptSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, project_id, version, style_id, style_name, title, total_chars,
                       source, cost_cny, needs_review, created_at, is_current
                FROM scripts WHERE project_id = ? ORDER BY version DESC
                """,
                (project_id,),
            ).fetchall()
        return [
            ScriptSummary(
                id=row["id"],
                project_id=row["project_id"],
                version=row["version"],
                style_id=row["style_id"],
                style_name=row["style_name"],
                title=row["title"],
                total_chars=row["total_chars"],
                source=row["source"],
                cost_cny=row["cost_cny"],
                needs_review=bool(row["needs_review"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                is_current=bool(row["is_current"]),
            )
            for row in rows
        ]

    def set_current_script(self, project_id: str, script_id: str) -> bool:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM scripts WHERE id = ? AND project_id = ?",
                (script_id, project_id),
            ).fetchone()
            if not exists:
                return False
            connection.execute(
                "UPDATE scripts SET is_current = 0 WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "UPDATE scripts SET is_current = 1 WHERE id = ? AND project_id = ?",
                (script_id, project_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (utc_now().isoformat(), project_id),
            )
        return True

    def count_scripts(self, project_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM scripts WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    # ------------------------------------------------------------------ 配音与镜头匹配
    def save_voiceover(self, voiceover: Voiceover, *, make_current: bool = True) -> Voiceover:
        with self._connect() as connection:
            if make_current:
                connection.execute(
                    "UPDATE voiceovers SET is_current = 0 WHERE project_id = ?",
                    (voiceover.project_id,),
                )
            connection.execute(
                """
                INSERT INTO voiceovers(
                    id, project_id, script_id, script_version, payload, audio_path,
                    total_duration_s, created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    voiceover.id,
                    voiceover.project_id,
                    voiceover.script_id,
                    voiceover.script_version,
                    voiceover.model_dump_json(),
                    voiceover.audio_path,
                    voiceover.total_duration_s,
                    voiceover.created_at.isoformat(),
                    int(bool(make_current)),
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (utc_now().isoformat(), voiceover.project_id),
            )
        return voiceover

    def get_voiceover(self, voiceover_id: str) -> Voiceover | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM voiceovers WHERE id = ?",
                (voiceover_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return Voiceover.model_validate_json(row["payload"])
        except ValueError:
            return None

    def get_current_voiceover(self, project_id: str) -> Voiceover | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM voiceovers WHERE project_id = ? AND is_current = 1 "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return Voiceover.model_validate_json(row["payload"])
        except ValueError:
            return None

    def save_shot_match(self, plan: ShotMatchPlan, *, make_current: bool = True) -> ShotMatchPlan:
        with self._connect() as connection:
            if make_current:
                connection.execute(
                    "UPDATE shot_matches SET is_current = 0 WHERE project_id = ?",
                    (plan.project_id,),
                )
            connection.execute(
                """
                INSERT INTO shot_matches(
                    id, project_id, script_id, voiceover_id, payload, created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.id,
                    plan.project_id,
                    plan.script_id,
                    plan.voiceover_id,
                    plan.model_dump_json(),
                    plan.created_at.isoformat(),
                    int(bool(make_current)),
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (utc_now().isoformat(), plan.project_id),
            )
        return plan

    def get_shot_match(self, plan_id: str) -> ShotMatchPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shot_matches WHERE id = ?",
                (plan_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return ShotMatchPlan.model_validate_json(row["payload"])
        except ValueError:
            return None

    def get_current_shot_match(self, project_id: str) -> ShotMatchPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shot_matches WHERE project_id = ? AND is_current = 1 "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return ShotMatchPlan.model_validate_json(row["payload"])
        except ValueError:
            return None

    def save_export(self, result: ExportResult, *, make_current: bool = True) -> ExportResult:
        with self._connect() as connection:
            if make_current:
                connection.execute(
                    "UPDATE exports SET is_current = 0 WHERE project_id = ?",
                    (result.project_id,),
                )
            connection.execute(
                """
                INSERT INTO exports(
                    id, project_id, plan_id, voiceover_id, payload, output_path,
                    created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.id,
                    result.project_id,
                    result.plan_id,
                    result.voiceover_id,
                    result.model_dump_json(),
                    result.output_path,
                    result.created_at.isoformat(),
                    int(bool(make_current)),
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (utc_now().isoformat(), result.project_id),
            )
        return result

    def get_export(self, export_id: str) -> ExportResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM exports WHERE id = ?", (export_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return ExportResult.model_validate_json(row["payload"])
        except ValueError:
            return None

    def get_current_export(self, project_id: str) -> ExportResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM exports WHERE project_id = ? AND is_current = 1 "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return ExportResult.model_validate_json(row["payload"])
        except ValueError:
            return None

    # ------------------------------------------------------------------ 费用与缓存
    def record_api_call(
        self,
        *,
        project_id: str,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_cny: float,
        cached: bool,
        status: str,
        started_at: datetime,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        error: str = "",
        request_hash: str = "",
        tokens_estimated: bool = False,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO api_calls(
                    project_id, purpose, model, prompt_tokens, completion_tokens, total_tokens,
                    cost_cny, cached, status, started_at, finished_at, duration_ms, error,
                    request_hash, tokens_estimated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    purpose,
                    model,
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    float(cost_cny),
                    int(bool(cached)),
                    status,
                    started_at.isoformat(),
                    finished_at.isoformat() if finished_at else None,
                    duration_ms,
                    (error or "")[:2000],
                    request_hash,
                    int(bool(tokens_estimated)),
                ),
            )
            return int(cursor.lastrowid)

    def recent_api_calls(self, project_id: str | None = None, limit: int = 50) -> list[ApiCallRecord]:
        query = "SELECT * FROM api_calls"
        params: list[object] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_to_api_call(row) for row in rows]

    def cost_summary(self, project_id: str | None = None, *, budget_cny: float = 0.0) -> CostSummary:
        query = """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END), 0) AS cached_calls,
                   COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failed_calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_cny), 0) AS spent_cny
            FROM api_calls
        """
        params: list[object] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return CostSummary(
            project_id=project_id,
            budget_cny=budget_cny,
            spent_cny=float(row["spent_cny"] or 0),
            calls=int(row["calls"] or 0),
            cached_calls=int(row["cached_calls"] or 0),
            failed_calls=int(row["failed_calls"] or 0),
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
        )

    def get_llm_cache(self, request_hash: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response, model, prompt_tokens, completion_tokens FROM llm_cache WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
        if not row:
            return None
        return {
            "response": row["response"],
            "model": row["model"],
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
        }

    def save_llm_cache(
        self,
        request_hash: str,
        response: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_cache(request_hash, response, model, prompt_tokens, completion_tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_hash) DO NOTHING
                """,
                (
                    request_hash,
                    response,
                    model,
                    int(prompt_tokens),
                    int(completion_tokens),
                    utc_now().isoformat(),
                ),
            )

    def clear_llm_cache(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM llm_cache")

    # ------------------------------------------------------------------ 聚合
    def build_timeline(self, project_id: str, warnings: list[str] | None = None) -> AnalysisTimeline:
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(f"项目不存在：{project_id}")
        media = self.get_media(project_id)
        if media is None:
            raise ValueError("项目尚未完成影片信息读取")
        return AnalysisTimeline(
            project_id=project.id,
            project_name=project.name,
            source_path=project.source_path,
            media=media,
            shots=self.get_shots(project_id),
            dialogues=self.get_dialogues(project_id),
            warnings=warnings or [],
        )

    def export_timeline(self, project_id: str, out_path: Path | str) -> Path:
        timeline = self.build_timeline(project_id)
        path = Path(out_path)
        timeline.write_json(path)
        return path


def _row_to_api_call(row: sqlite3.Row) -> ApiCallRecord:
    return ApiCallRecord(
        id=row["id"],
        project_id=row["project_id"],
        purpose=row["purpose"],
        model=row["model"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        total_tokens=row["total_tokens"],
        cost_cny=row["cost_cny"],
        cached=bool(row["cached"]),
        status=row["status"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        duration_ms=row["duration_ms"],
        error=row["error"] or "",
        request_hash=row["request_hash"] or "",
        tokens_estimated=bool(row["tokens_estimated"]),
    )


def _row_to_project(row: sqlite3.Row) -> ProjectRecord:
    keys = set(row.keys())
    return ProjectRecord(
        id=row["id"],
        name=row["name"],
        source_path=row["source_path"],
        project_dir=row["project_dir"],
        status=ProjectStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        duration_s=row["duration_s"],
        width=row["width"],
        height=row["height"],
        shot_count=row["shot_count"] if "shot_count" in keys else 0,
        dialogue_count=row["dialogue_count"] if "dialogue_count" in keys else 0,
        error=row["error"],
    )
