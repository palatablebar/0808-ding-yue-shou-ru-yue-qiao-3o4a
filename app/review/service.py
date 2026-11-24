from datetime import datetime, timezone
from copy import deepcopy
from hashlib import sha256
from time import monotonic

from app.business import DomainIssue
from app.config import RULE_VERSION
from app.intake.service import accepted_rows_with_lines
from app.modules.pipeline import run


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def execute(store, data):
    start = monotonic()
    fingerprint = sha256(data).hexdigest()
    run_id = sha256(f"{RULE_VERSION}:{fingerprint}".encode()).hexdigest()
    try:
        return store.load(run_id)
    except DomainIssue as error:
        if error.code != "RUN_NOT_FOUND":
            raise
    report = run(data)
    report["manifest"] = dict(
        sha256=fingerprint,
        version=RULE_VERSION,
        created=now(),
        duration_ms=round((monotonic() - start) * 1000, 3),
        bytes=len(data),
        run_id=run_id,
    )
    # 首发批次即谱系根：标记每个分组的来源，便于后续版本区分重算与冻结。
    report["lineage"] = dict(
        root=run_id, prev=None, depth=0, affected=[], changed_records=[]
    )
    for case in report.get("cases", []):
        case["provenance"] = dict(state="ORIGINAL", source_run=run_id)
    registered = store.register(run_id, report)
    # 采信行登记为各自的 v1 来源版本，并保留源行号供矛盾定位与版本回读。
    seed_rows, line_of = accepted_rows_with_lines(data)
    seeded = [
        dict(deepcopy(row), _line=line_of.get(row["record"])) for row in seed_rows
    ]
    store.seed_lineage(run_id, seeded, report["manifest"]["created"])
    return registered


def review(store, run_id, values):
    if not isinstance(values, dict):
        raise DomainIssue("REVIEW_FIELDS")
    revision, status, note = (values.get(key) for key in ("revision", "status", "note"))
    if (
        type(revision) is not int
        or revision < 0
        or status not in ("APPROVED", "HELD")
        or not isinstance(note, str)
        or not (3 <= len(note.strip()) <= 200)
    ):
        raise DomainIssue("REVIEW_FIELDS")
    current = store.load(run_id)
    if revision != current["revision"]:
        raise DomainIssue("STALE_REVIEW")
    if current["status"] == "APPROVED":
        raise DomainIssue("RUN_FINALIZED")
    if status == "APPROVED" and not current["report"]["balance"]["releasable"]:
        raise DomainIssue("UNRESOLVED_ISSUES")
    return store.revise(run_id, revision, status, note.strip(), now())
