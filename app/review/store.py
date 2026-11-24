import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.business import DomainIssue

SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, report TEXT NOT NULL, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', revision INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '', reviewed TEXT
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    root_id TEXT NOT NULL,
    run_id TEXT NOT NULL PRIMARY KEY,
    prev_id TEXT,
    depth INTEGER NOT NULL,
    affected TEXT NOT NULL,
    created TEXT NOT NULL,
    UNIQUE(root_id, depth)
);
CREATE TABLE IF NOT EXISTS record_versions (
    root_id TEXT NOT NULL,
    record_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    line INTEGER,
    PRIMARY KEY (root_id, record_key, version)
);
CREATE TABLE IF NOT EXISTS supplement_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id TEXT NOT NULL,
    base_id TEXT NOT NULL,
    result TEXT NOT NULL,
    reason TEXT NOT NULL,
    blockers TEXT NOT NULL,
    created TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            yield db

    def register(self, run_id, report):
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,report,created) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
                (
                    run_id,
                    json.dumps(report, ensure_ascii=False),
                    report["manifest"]["created"],
                ),
            )
        return self.load(run_id)

    def load(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise DomainIssue("RUN_NOT_FOUND")
        result = dict(row)
        result["report"] = json.loads(result["report"])
        return result

    def list(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,created,status,revision,note FROM runs ORDER BY created DESC,id LIMIT 50"
                )
            ]

    def revise(self, run_id, revision, status, note, now):
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE runs SET status=?,note=?,reviewed=?,revision=revision+1 WHERE id=? AND revision=?",
                (status, note, now, run_id, revision),
            )
            if cursor.rowcount != 1:
                raise DomainIssue("STALE_REVIEW")
        return self.load(run_id)

    # --- 版本谱系 --------------------------------------------------------

    def root_of(self, run_id):
        """返回某批次所属谱系根；没有谱系记录时以自身为根。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT root_id FROM lineage_edges WHERE run_id=?", (run_id,)
            ).fetchone()
        return row["root_id"] if row else run_id

    def lineage(self, root_id):
        with self.connect() as db:
            edges = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM lineage_edges WHERE root_id=? ORDER BY depth",
                    (root_id,),
                )
            ]
            if not edges:
                seed = db.execute(
                    "SELECT id,created,status FROM runs WHERE id=?", (root_id,)
                ).fetchone()
                if seed is None:
                    raise DomainIssue("RUN_NOT_FOUND")
                edges = [
                    {
                        "root_id": root_id,
                        "run_id": root_id,
                        "prev_id": None,
                        "depth": 0,
                        "affected": "[]",
                        "created": seed["created"],
                    }
                ]
            statuses = {
                r["id"]: r["status"]
                for r in db.execute(
                    f"SELECT id,status FROM runs WHERE id IN ({','.join('?' for _ in edges)})",
                    [e["run_id"] for e in edges],
                )
            }
            head = db.execute(
                "SELECT run_id FROM lineage_edges WHERE root_id=? ORDER BY depth DESC LIMIT 1",
                (root_id,),
            ).fetchone()
        versions = [
            dict(
                run_id=edge["run_id"],
                depth=edge["depth"],
                prev_id=edge["prev_id"],
                affected=json.loads(edge["affected"]),
                created=edge["created"],
                status=statuses.get(edge["run_id"], "PENDING"),
            )
            for edge in edges
        ]
        return dict(
            root_run_id=root_id,
            head_run_id=head["run_id"] if head else root_id,
            versions=versions,
        )

    def current_rows(self, root_id):
        """谱系根下每个记录键的当前生效版本（版本号最大者）。"""
        with self.connect() as db:
            rows = db.execute(
                """SELECT v.record_key, v.version, v.payload, v.run_id, v.line
                   FROM record_versions v
                   JOIN (SELECT record_key, MAX(version) AS mv
                         FROM record_versions WHERE root_id=? GROUP BY record_key) m
                     ON v.record_key=m.record_key AND v.version=m.mv
                   WHERE v.root_id=?""",
                (root_id, root_id),
            ).fetchall()
        return [
            dict(
                json.loads(r["payload"]),
                _version=r["version"],
                _run_id=r["run_id"],
                _line=r["line"],
            )
            for r in rows
        ]

    def record_versions(self, root_id, record_key):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM record_versions WHERE root_id=? AND record_key=? ORDER BY version",
                (root_id, record_key),
            ).fetchall()
        if not rows:
            raise DomainIssue("RECORD_NOT_FOUND")
        return [
            dict(
                json.loads(r["payload"]),
                version=r["version"],
                introduced_by=r["run_id"],
                source_line=r["line"],
            )
            for r in rows
        ]

    def seed_lineage(self, run_id, rows, created):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO lineage_edges(root_id,run_id,prev_id,depth,affected,created)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, run_id, None, 0, "[]", created),
            )
            for row in rows:
                payload = {k: v for k, v in row.items() if not k.startswith("_")}
                db.execute(
                    "INSERT OR IGNORE INTO record_versions"
                    "(root_id,record_key,version,run_id,payload,line)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        run_id,
                        row["record"],
                        1,
                        run_id,
                        json.dumps(payload, ensure_ascii=False),
                        row.get("_line"),
                    ),
                )

    def commit_version(
        self,
        root_id,
        run_id,
        prev_id,
        depth,
        affected,
        new_versions,
        created,
    ):
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO lineage_edges(root_id,run_id,prev_id,depth,affected,created)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        root_id,
                        run_id,
                        prev_id,
                        depth,
                        json.dumps(affected, ensure_ascii=False),
                        created,
                    ),
                )
                for item in new_versions:
                    row = item["row"]
                    payload = {k: v for k, v in row.items() if not k.startswith("_")}
                    db.execute(
                        "INSERT INTO record_versions"
                        "(root_id,record_key,version,run_id,payload,line)"
                        " VALUES (?,?,?,?,?,?)",
                        (
                            root_id,
                            row["record"],
                            item["version"],
                            run_id,
                            json.dumps(payload, ensure_ascii=False),
                            row.get("_line"),
                        ),
                    )
            except sqlite3.IntegrityError as error:
                # 并发补录抢先占用了同一谱系深度或记录版本号。
                raise DomainIssue("LINEAGE_ADVANCED") from error

    def next_version_numbers(self, root_id, keys):
        if not keys:
            return {}
        with self.connect() as db:
            rows = db.execute(
                f"SELECT record_key, MAX(version) AS mv FROM record_versions"
                f" WHERE root_id=? AND record_key IN ({','.join('?' for _ in keys)})"
                f" GROUP BY record_key",
                (root_id, *keys),
            ).fetchall()
        return {r["record_key"]: r["mv"] + 1 for r in rows}

    def record_attempt(self, root_id, base_id, result, reason, blockers, now):
        with self.connect() as db:
            db.execute(
                "INSERT INTO supplement_attempts(root_id,base_id,result,reason,blockers,created)"
                " VALUES (?,?,?,?,?,?)",
                (
                    root_id,
                    base_id,
                    result,
                    reason,
                    json.dumps(blockers, ensure_ascii=False),
                    now,
                ),
            )

    def attempts(self, root_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM supplement_attempts WHERE root_id=? ORDER BY id",
                (root_id,),
            ).fetchall()
        return [
            dict(
                base_id=r["base_id"],
                result=r["result"],
                reason=r["reason"],
                blockers=json.loads(r["blockers"]),
                created=r["created"],
            )
            for r in rows
        ]

    def supplement(self, base_id, data):
        # 懒导入：lineage.service 依赖 review.service，模块级导入会形成环。
        from app.lineage.service import supplement

        return supplement(self, base_id, data)

    def discard(self, run_id):
        with self.connect() as db:
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))
