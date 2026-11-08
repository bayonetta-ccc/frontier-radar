"""SQLite 持久层：任务、来源、运行、信号、证据、判断、版本、反馈、审计事件。

设计原则：
* 所有“当前状态”都可重建：判断的最新版本来自 claim_versions；
* 所有导出物（digest JSON / Markdown）都落库，便于复核与重放；
* 不使用 ORM，保持零依赖，方便 fork 后按需替换存储。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    Claim,
    ClaimStatus,
    ClaimVersion,
    InfoLayer,
    SignalRecord,
    Signal,
)

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    constraints TEXT NOT NULL DEFAULT '',
    horizon TEXT NOT NULL DEFAULT '',
    decision_criteria TEXT NOT NULL DEFAULT '',
    watchlist TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    profile_slug TEXT NOT NULL DEFAULT '',
    high_risk_threshold TEXT NOT NULL DEFAULT '0.6',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    task_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    authority TEXT NOT NULL,
    cadence TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    user_added INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_error TEXT,
    PRIMARY KEY (task_id, source_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    url_key TEXT NOT NULL,
    published_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    info_layer TEXT NOT NULL DEFAULT 'fact',
    stage_level INTEGER NOT NULL DEFAULT 0,
    stage_label TEXT NOT NULL DEFAULT '',
    entities TEXT NOT NULL DEFAULT '[]',
    anchors TEXT NOT NULL DEFAULT '[]',
    novelty REAL NOT NULL DEFAULT 0,
    reliability TEXT NOT NULL DEFAULT 'medium',
    noise_reason TEXT NOT NULL DEFAULT '',
    duplicate_of TEXT,
    canonical_group TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, signal_id)
);

CREATE INDEX IF NOT EXISTS idx_signals_task ON signals(task_id, published_at);
CREATE INDEX IF NOT EXISTS idx_signals_urlkey ON signals(task_id, url_key);
CREATE INDEX IF NOT EXISTS idx_signals_hash ON signals(task_id, content_hash);

CREATE TABLE IF NOT EXISTS evidence (
    claim_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    weight REAL NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (claim_id, signal_id, run_id)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    anchor_key TEXT NOT NULL DEFAULT '',
    statement TEXT NOT NULL,
    claim_kind TEXT NOT NULL DEFAULT 'technology',
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_reliability TEXT NOT NULL DEFAULT '中',
    impact_confidence TEXT NOT NULL DEFAULT '低',
    stage_level INTEGER NOT NULL DEFAULT 0,
    high_risk INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'system',
    needs_review INTEGER NOT NULL DEFAULT 1,
    pending_review INTEGER NOT NULL DEFAULT 0,
    supporting TEXT NOT NULL DEFAULT '[]',
    contradicting TEXT NOT NULL DEFAULT '[]',
    open_questions TEXT NOT NULL DEFAULT '[]',
    next_verification TEXT NOT NULL DEFAULT '',
    watch_indicators TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_task ON claims(task_id, status);

CREATE TABLE IF NOT EXISTS claim_versions (
    claim_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    stage_level INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    trigger_signal_ids TEXT NOT NULL DEFAULT '[]',
    user_correction TEXT NOT NULL DEFAULT '',
    rolled_back_from INTEGER,
    PRIMARY KEY (claim_id, version)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    claim_id TEXT NOT NULL DEFAULT '',
    signal_id TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, kind)
);
"""


class Database:
    def __init__(self, path: str | Path = "data/radar.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._set_meta("schema_version", str(SCHEMA_VERSION))

    # ---- 基础 -------------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _set_meta(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _load(raw: str | None, default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    # ---- 任务 -------------------------------------------------------------
    def create_task(self, task: dict[str, Any]) -> str:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, topic, role, purpose, decision, constraints, horizon,
                                  decision_criteria, watchlist, notes, profile_slug, high_risk_threshold,
                                  created_at, updated_at)
                VALUES(:task_id, :topic, :role, :purpose, :decision, :constraints, :horizon,
                       :decision_criteria, :watchlist, :notes, :profile_slug, :high_risk_threshold,
                       :created_at, :updated_at)
                ON CONFLICT(task_id) DO UPDATE SET
                    topic=excluded.topic, role=excluded.role, purpose=excluded.purpose,
                    decision=excluded.decision, constraints=excluded.constraints,
                    horizon=excluded.horizon, decision_criteria=excluded.decision_criteria,
                    watchlist=excluded.watchlist, notes=excluded.notes,
                    profile_slug=excluded.profile_slug,
                    high_risk_threshold=excluded.high_risk_threshold,
                    updated_at=excluded.updated_at
                """,
                {
                    **task,
                    "watchlist": self._json(task.get("watchlist", [])),
                    "high_risk_threshold": str(task.get("high_risk_threshold", 0.6)),
                },
            )
        return str(task["task_id"])

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        task = dict(row)
        task["watchlist"] = self._load(task.get("watchlist"), [])
        return task

    def list_tasks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        out = []
        for row in rows:
            task = dict(row)
            task["watchlist"] = self._load(task.get("watchlist"), [])
            out.append(task)
        return out

    def upsert_sources(self, task_id: str, sources: Iterable[dict[str, Any]]) -> None:
        with self.tx() as conn:
            for src in sources:
                conn.execute(
                    """
                    INSERT INTO sources(task_id, source_id, name, kind, authority, cadence, url, note, enabled)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, source_id) DO UPDATE SET
                        name=excluded.name, kind=excluded.kind, authority=excluded.authority,
                        cadence=excluded.cadence, url=excluded.url, note=excluded.note
                    """,
                    (
                        task_id,
                        src["source_id"],
                        src["name"],
                        src["kind"],
                        src["authority"],
                        src.get("cadence", "weekly"),
                        src.get("url", ""),
                        src.get("note", ""),
                        int(src.get("enabled", 1)),
                    ),
                )

    def list_sources(self, task_id: str, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources WHERE task_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY kind, authority DESC, source_id"
        return [dict(r) for r in self.conn.execute(sql, (task_id,)).fetchall()]

    def mark_source_access(self, task_id: str, source_id: str, ok: bool, error: str = "") -> None:
        with self.tx() as conn:
            if ok:
                conn.execute(
                    "UPDATE sources SET last_success_at=?, last_error='' WHERE task_id=? AND source_id=?",
                    (_now(), task_id, source_id),
                )
            else:
                conn.execute(
                    "UPDATE sources SET last_error=? WHERE task_id=? AND source_id=?",
                    (error[:500], task_id, source_id),
                )

    def set_source_enabled(self, task_id: str, source_id: str, enabled: bool) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE sources SET enabled=? WHERE task_id=? AND source_id=?",
                (int(enabled), task_id, source_id),
            )

    # ---- 运行 -------------------------------------------------------------
    def create_run(self, run: dict[str, Any]) -> str:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, task_id, mode, period_start, period_end, started_at,
                                 finished_at, source_mode, status, summary)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["task_id"],
                    run["mode"],
                    run["period_start"],
                    run["period_end"],
                    run["started_at"],
                    run.get("finished_at", run["started_at"]),
                    run.get("source_mode", "offline"),
                    run.get("status", "running"),
                    self._json(run.get("summary", {})),
                ),
            )
        return str(run["run_id"])

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET status=?, summary=? WHERE run_id=?",
                (status, self._json(summary), run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["summary"] = self._load(run["summary"], {})
        return run

    def list_runs(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY finished_at DESC, started_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        out = []
        for row in rows:
            run = dict(row)
            run["summary"] = self._load(run["summary"], {})
            out.append(run)
        return out

    def last_finished_run(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE task_id=? AND status IN ('ok','partial') "
            "ORDER BY finished_at DESC, started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        run = dict(row)
        run["summary"] = self._load(run["summary"], {})
        return run

    # ---- 信号 -------------------------------------------------------------
    def insert_signals(self, task_id: str, run_id: str, signals: Iterable[SignalRecord]) -> None:
        with self.tx() as conn:
            for s in signals:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO signals(signal_id, run_id, task_id, source_id, title, url,
                        url_key, published_at, retrieved_at, excerpt, category, info_layer, stage_level,
                        stage_label, entities, anchors, novelty, reliability, noise_reason, duplicate_of,
                        canonical_group, content_hash, raw)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        s.signal_id,
                        run_id,
                        task_id,
                        s.source_id,
                        s.title,
                        s.url,
                        s.url_key,
                        s.published_at,
                        s.retrieved_at,
                        s.excerpt,
                        s.category,
                        s.info_layer.value,
                        s.stage_level,
                        s.stage_label,
                        self._json(list(s.entities)),
                        self._json(list(s.anchors)),
                        s.novelty,
                        s.reliability,
                        s.noise_reason,
                        s.duplicate_of,
                        s.canonical_group,
                        s.content_hash,
                        self._json(s.raw),
                    ),
                )

    def _signal_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["entities"] = self._load(data.get("entities"), [])
        data["anchors"] = self._load(data.get("anchors"), [])
        data["raw"] = self._load(data.get("raw"), {})
        return data

    def get_signal(self, run_id: str, signal_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM signals WHERE run_id=? AND signal_id=?", (run_id, signal_id)
        ).fetchone()
        return self._signal_row(row) if row else None

    def list_signals(self, run_id: str, include_noise: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signals WHERE run_id=?"
        if not include_noise:
            sql += " AND noise_reason='' AND duplicate_of IS NULL"
        sql += " ORDER BY published_at, signal_id"
        return [self._signal_row(r) for r in self.conn.execute(sql, (run_id,)).fetchall()]

    def find_recent_signals(self, task_id: str, before: str, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE task_id=? AND published_at < ? ORDER BY published_at DESC LIMIT ?",
            (task_id, before, limit),
        ).fetchall()
        return [self._signal_row(r) for r in rows]

    def find_url_key(self, task_id: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM signals WHERE task_id=? AND url_key=? AND noise_reason=''",
            (task_id, key),
        ).fetchone()
        return self._signal_row(row) if row else None

    def find_content_hash(self, task_id: str, digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM signals WHERE task_id=? AND content_hash=? LIMIT 1",
            (task_id, digest),
        ).fetchone()
        return self._signal_row(row) if row else None

    def signal_seen_in_run(self, task_id: str, url_key: str, run_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE task_id=? AND url_key=? AND run_id=? LIMIT 1",
            (task_id, url_key, run_id),
        ).fetchone()
        return row is not None

    # ---- 判断 -------------------------------------------------------------
    def upsert_claim(self, claim: Claim) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO claims(claim_id, task_id, anchor_key, statement, claim_kind, status,
                    confidence, source_reliability, impact_confidence, stage_level, high_risk, origin,
                    needs_review, pending_review, supporting, contradicting, open_questions, next_verification,
                    watch_indicators, created_at, updated_at, confirmed_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    statement=excluded.statement, status=excluded.status, confidence=excluded.confidence,
                    source_reliability=excluded.source_reliability, impact_confidence=excluded.impact_confidence,
                    stage_level=excluded.stage_level, high_risk=excluded.high_risk, origin=excluded.origin,
                    needs_review=excluded.needs_review, pending_review=excluded.pending_review,
                    supporting=excluded.supporting,
                    contradicting=excluded.contradicting, open_questions=excluded.open_questions,
                    next_verification=excluded.next_verification, watch_indicators=excluded.watch_indicators,
                    updated_at=excluded.updated_at, confirmed_at=excluded.confirmed_at
                """,
                (
                    claim.claim_id,
                    claim.task_id,
                    claim.anchor_key,
                    claim.statement,
                    claim.claim_kind,
                    claim.status.value,
                    claim.confidence,
                    claim.source_reliability,
                    claim.impact_confidence,
                    claim.stage_level,
                    int(claim.high_risk),
                    claim.origin,
                    int(claim.needs_review),
                    int(claim.pending_review),
                    self._json(list(claim.supporting_signal_ids)),
                    self._json(list(claim.contradicting_signal_ids)),
                    self._json(list(claim.open_questions)),
                    claim.next_verification,
                    self._json(list(claim.watch_indicators)),
                    claim.created_at,
                    claim.updated_at,
                    claim.confirmed_at,
                ),
            )

    def _claim_row(self, row: sqlite3.Row) -> Claim:
        data = dict(row)
        return Claim(
            claim_id=data["claim_id"],
            task_id=data["task_id"],
            anchor_key=data["anchor_key"],
            statement=data["statement"],
            claim_kind=data["claim_kind"],
            status=ClaimStatus(data["status"]),
            confidence=float(data["confidence"]),
            source_reliability=data["source_reliability"],
            impact_confidence=data["impact_confidence"],
            stage_level=int(data["stage_level"]),
            high_risk=bool(data["high_risk"]),
            origin=data["origin"],
            needs_review=bool(data["needs_review"]),
            pending_review=bool(data.get("pending_review", 0)),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            confirmed_at=data["confirmed_at"],
            supporting_signal_ids=tuple(self._load(data["supporting"], [])),
            contradicting_signal_ids=tuple(self._load(data["contradicting"], [])),
            open_questions=tuple(self._load(data["open_questions"], [])),
            next_verification=data["next_verification"],
            watch_indicators=tuple(self._load(data["watch_indicators"], [])),
            version=self.latest_version_number(data["claim_id"]),
        )

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self.conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        return self._claim_row(row) if row else None

    def list_claims(self, task_id: str, statuses: Iterable[ClaimStatus] | None = None) -> list[Claim]:
        sql = "SELECT * FROM claims WHERE task_id=?"
        params: list[Any] = [task_id]
        status_list = [s.value for s in statuses] if statuses else None
        if status_list:
            placeholders = ",".join("?" for _ in status_list)
            sql += f" AND status IN ({placeholders})"
            params.extend(status_list)
        sql += " ORDER BY high_risk DESC, confidence DESC"
        return [self._claim_row(r) for r in self.conn.execute(sql, params).fetchall()]

    def find_claim_by_anchor(self, task_id: str, anchor_key: str) -> Claim | None:
        row = self.conn.execute(
            "SELECT * FROM claims WHERE task_id=? AND anchor_key=? ORDER BY created_at LIMIT 1",
            (task_id, anchor_key),
        ).fetchone()
        return self._claim_row(row) if row else None

    def add_claim_version(self, version: ClaimVersion) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO claim_versions(claim_id, version, statement, status, confidence,
                    stage_level, changed_at, change_kind, reason, trigger_signal_ids, user_correction,
                    rolled_back_from)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.claim_id,
                    version.version,
                    version.statement,
                    version.status,
                    version.confidence,
                    version.stage_level,
                    version.changed_at,
                    version.change_kind,
                    version.reason,
                    self._json(list(version.trigger_signal_ids)),
                    version.user_correction,
                    version.rolled_back_from,
                ),
            )

    def latest_version_number(self, claim_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM claim_versions WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return int(row["v"] or 0)

    def get_claim_version(self, claim_id: str, version: int) -> ClaimVersion | None:
        row = self.conn.execute(
            "SELECT * FROM claim_versions WHERE claim_id=? AND version=?", (claim_id, version)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        return ClaimVersion(
            claim_id=data["claim_id"],
            version=int(data["version"]),
            statement=data["statement"],
            status=data["status"],
            confidence=float(data["confidence"]),
            stage_level=int(data["stage_level"]),
            changed_at=data["changed_at"],
            change_kind=data["change_kind"],
            reason=data["reason"],
            trigger_signal_ids=tuple(self._load(data["trigger_signal_ids"], [])),
            user_correction=data["user_correction"],
            rolled_back_from=data["rolled_back_from"],
        )

    def list_claim_versions(self, claim_id: str) -> list[ClaimVersion]:
        rows = self.conn.execute(
            "SELECT version FROM claim_versions WHERE claim_id=? ORDER BY version DESC", (claim_id,)
        ).fetchall()
        out = []
        for row in rows:
            version = self.get_claim_version(claim_id, int(row["version"]))
            if version:
                out.append(version)
        return out

    def has_prior_support(self, claim_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM evidence WHERE claim_id=? AND direction='support' LIMIT 1",
            (claim_id,),
        ).fetchone()
        return row is not None

    def prior_evidence_count(self, claim_id: str, before_run_id: str, direction: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE claim_id=? AND direction=? AND run_id<>?",
            (claim_id, direction, before_run_id),
        ).fetchone()
        return int(row["n"] if row else 0)

    def pending_version(self, claim_id: str) -> ClaimVersion | None:
        row = self.conn.execute(
            "SELECT * FROM claim_versions WHERE claim_id=? AND user_correction='pending' "
            "ORDER BY version DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
        if not row:
            return None
        return self.get_claim_version(claim_id, int(row["version"]))

    def delete_claim_version(self, claim_id: str, version: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "DELETE FROM claim_versions WHERE claim_id=? AND version=?", (claim_id, version)
            )

    # ---- 证据 / 反馈 / 事件 / 产物 -----------------------------------------
    def add_evidence(
        self,
        claim_id: str,
        run_id: str,
        signal_id: str,
        direction: str,
        weight: float,
        rationale: str,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence(claim_id, signal_id, run_id, direction, weight, rationale)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (claim_id, signal_id, run_id, direction, weight, rationale),
            )

    def remove_evidence(self, claim_id: str, signal_ids: Iterable[str]) -> None:
        ids = list(signal_ids)
        if not ids:
            return
        with self.tx() as conn:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM evidence WHERE claim_id=? AND signal_id IN ({placeholders})",
                [claim_id, *ids],
            )

    def evidence_for_claim(self, claim_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.*, s.title, s.url, s.published_at, s.source_id FROM evidence e "
            "JOIN signals s ON s.signal_id=e.signal_id AND s.run_id=e.run_id "
            "WHERE e.claim_id=? ORDER BY s.published_at",
            (claim_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_feedback(
        self,
        task_id: str,
        verdict: str,
        run_id: str = "",
        claim_id: str = "",
        signal_id: str = "",
        note: str = "",
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO feedback(task_id, run_id, claim_id, signal_id, verdict, note, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, claim_id, signal_id, verdict, note, _now()),
            )
            return int(cur.lastrowid or 0)

    def list_feedback(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM feedback WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def add_event(
        self, task_id: str, kind: str, payload: dict[str, Any], run_id: str = ""
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO events(task_id, run_id, kind, payload, created_at) VALUES(?, ?, ?, ?, ?)",
                (task_id, run_id, kind, self._json(payload), _now()),
            )

    def list_events(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._load(item["payload"], {})
            out.append(item)
        return out

    def add_artifact(self, run_id: str, task_id: str, kind: str, path: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts(run_id, task_id, kind, path, created_at) VALUES(?, ?, ?, ?, ?)",
                (run_id, task_id, kind, path, _now()),
            )

    def artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM artifacts WHERE run_id=?", (run_id,)).fetchall()
        return [dict(r) for r in rows]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> Database:
    return Database(path)


__all__ = ["Database", "SCHEMA_VERSION", "connect", "Signal", "SignalRecord", "InfoLayer"]
