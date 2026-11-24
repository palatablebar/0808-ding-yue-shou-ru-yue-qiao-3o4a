"""来源版本谱系：迟到补录、增量重算与签发前阻断。

设计要点：
- 每个批次的采信行在首次执行时登记为记录版本 v1，谱系根即首发批次。
- 补录按记录键挂到同一条谱系上；没有前序依据的键一律拒绝，不产生新版本。
- 同一记录在一次补录内出现互相矛盾且无法判定先后的副本时，连同当前生效
  版本一起定位，整批阻断、不写入任何新版本。
- 新版本只重算受影响的业务分组，其余分组冻结旧批次的结论与证据。
"""

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from time import monotonic

from app.business import DomainIssue
from app.config import RULE_VERSION
from app.intake.reader import records
from app.intake.resolve import partition
from app.intake.schema import record
from app.modules.grouping import case_groups
from app.modules.pipeline import evaluate
from app.modules.reconcile import balance
from app.review.service import now

PUBLIC_KEYS = (
    "record",
    "case",
    "kind",
    "minute",
    "mrr_cents",
    "sequence",
    "note",
)


def public(row):
    return {key: row[key] for key in PUBLIC_KEYS}


def _raw_scan(data):
    """在分区之前读取补录：保留同键全部副本用于矛盾判定，同时统计坏行。

    行号通过独立映射回传，不污染业务 row（否则会破坏重复回放的等值判断）。
    """
    from app.intake.schema import HEADER

    counted = repeated = blanks = 0
    candidates, malformed, line_of = [], [], {}
    serial = 0
    for line, values in records(data):
        counted += 1
        if not any(v.strip() for v in values):
            blanks += 1
            continue
        if tuple(values) == HEADER:
            repeated += 1
            continue
        try:
            parsed = record(values)
        except DomainIssue as error:
            malformed.append(
                dict(
                    line=line,
                    record=values[0][:36] if values else "",
                    case=values[1][:36] if len(values) > 1 else "",
                    reason=error.code,
                )
            )
            continue
        # 同键可能有多个副本，用候选序号区分，避免行号互相覆盖。
        serial += 1
        line_of[serial] = line
        candidates.append((serial, line, parsed))
    accepted, partition_rejects = partition(
        [(line, row) for _, line, row in candidates]
    )
    return dict(
        input=counted,
        repeated=repeated,
        blank=blanks,
        candidates=candidates,
        line_of=line_of,
        accepted=accepted,
        malformed=malformed,
        partition_rejects=partition_rejects,
    )


def _contradictions(candidates):
    """同键多副本且业务内容不一致即无法判定先后；纯回放不算矛盾。"""
    grouped = defaultdict(list)
    for serial, line, row in candidates:
        grouped[row["record"]].append((serial, line, row))
    findings = []
    for key, copies in grouped.items():
        distinct = {tuple(public(r)[k] for k in PUBLIC_KEYS) for _, _, r in copies}
        if len(distinct) > 1:
            findings.append((key, copies))
    return findings


def supplement(store, base_id, data):
    base = store.load(base_id)  # 未知批次直接 RUN_NOT_FOUND
    root_id = store.root_of(base_id)
    scan = _raw_scan(data)
    candidate_rows = scan["candidates"]

    # 规则一：矛盾补录——同键冲突副本无法排序，定位全部参与记录（含现版本）。
    contradictions = _contradictions(candidate_rows)
    if contradictions:
        current = {r["record"]: r for r in store.current_rows(root_id)}
        blockers = []
        for key, copies in contradictions:
            participants = [
                dict(
                    record=r["record"],
                    case=r["case"],
                    line=line,
                    mrr_cents=r["mrr_cents"],
                    version="incoming",
                )
                for _, line, r in copies
            ]
            head = current.get(key)
            if head is not None:
                participants.append(
                    dict(
                        record=key,
                        case=head["case"],
                        line=head.get("_line"),
                        mrr_cents=head["mrr_cents"],
                        version=head["_version"],
                    )
                )
            blockers.append(
                dict(record=key, reason="VERSION_CONFLICT", participants=participants)
            )
        raise _blocked(store, root_id, base_id, "VERSION_CONFLICT", blockers)

    accepted = scan["accepted"]
    line_by_key = {row["record"]: line for _, line, row in candidate_rows}

    # 规则二：无依据补录——前序批次从未采信过的键不得进入谱系。
    known = {r["record"]: r for r in store.current_rows(root_id)}
    incoming = {}
    for row in accepted:
        incoming.setdefault(row["record"], row)
    unbased = [
        dict(
            line=line_by_key.get(key),
            record=key,
            case=row["case"],
            reason="UNBASED_BACKFILL",
        )
        for key, row in incoming.items()
        if key not in known
    ]
    # 补录文件自身的坏行同样不具备前序依据，一并定位、拒绝整批。
    unbased.extend(
        dict(
            line=item["line"],
            record=item["record"],
            case=item["case"],
            reason="UNBASED_BACKFILL:" + item["reason"],
        )
        for item in scan["malformed"]
    )
    # 分区隔离（除回放外）也属于无法采信的补录，按无依据处理。
    unbased.extend(
        dict(
            line=item["line"],
            record=item["record"],
            case=item["case"],
            reason="UNBASED_BACKFILL:" + item["reason"],
        )
        for item in scan["partition_rejects"]
        if item["reason"] != "DUPLICATE_REPLAY"
    )
    if unbased:
        raise _blocked(
            store,
            root_id,
            base_id,
            "UNBASED_BACKFILL",
            sorted(unbased, key=lambda item: item["line"]),
        )

    # 与当前生效版本逐字段比对，纯回放不推进谱系。
    changed = {
        key: row
        for key, row in incoming.items()
        if any(public(row)[k] != public(known[key])[k] for k in PUBLIC_KEYS)
    }
    if not changed:
        store.record_attempt(root_id, base_id, "NO_CHANGE", "REPLAY", [], now())
        return dict(status="NO_CHANGE", run=base, changed=[])

    affected = sorted({row["case"] for row in changed.values()})

    # 用“当前生效版本 + 本次变更”重建全量采信集，只重算受影响分组。
    ledger = {r["record"]: public(r) for r in store.current_rows(root_id)}
    ledger.update({key: public(row) for key, row in changed.items()})
    merged_rows = list(ledger.values())

    # 幂等：同一基础批次 + 同一补录载荷只产生一个新版本批次。
    supplement_fingerprint = sha256(data).hexdigest()
    merged_fingerprint = sha256(
        f"{root_id}:{base_id}:{supplement_fingerprint}".encode()
    ).hexdigest()
    run_id = sha256(
        f"{RULE_VERSION}:lineage:{merged_fingerprint}".encode()
    ).hexdigest()
    try:
        existing = store.load(run_id)
        return dict(status="ADVANCED", run=existing, changed=sorted(changed))
    except DomainIssue as error:
        if error.code != "RUN_NOT_FOUND":
            raise

    start = monotonic()
    base_cases = {c["case"]: deepcopy(c) for c in base["report"]["cases"]}
    cases = []
    for case_id, rows in case_groups(merged_rows):
        if case_id not in affected:
            # 冻结：深拷贝旧批次对该分组的结论与证据，避免改写共享对象。
            frozen = base_cases[case_id]
            frozen["provenance"] = dict(state="FROZEN", source_run=base_id)
            cases.append(frozen)
            continue
        case = evaluate(rows, 5)
        case["provenance"] = dict(
            state="RECALCULATED",
            source_run=run_id,
            previous_run=base_id,
        )
        cases.append(case)

    isolation = scan["malformed"] + [
        item for item in scan["partition_rejects"] if item["reason"] != "DUPLICATE_REPLAY"
    ]
    reasons = defaultdict(int)
    for item in isolation:
        reasons[item["reason"]] += 1
    intake_summary = dict(
        input=scan["input"],
        accepted=len(merged_rows),
        repeated=scan["repeated"],
        blank=scan["blank"],
        rejected=len(isolation),
        reasons=dict(reasons),
        isolation=sorted(isolation, key=lambda item: item["line"]),
    )
    checks = balance(
        dict(
            input=len(merged_rows) + len(isolation) + scan["blank"] + scan["repeated"],
            accepted=len(merged_rows),
            rejected=len(isolation),
            blank=scan["blank"],
            repeated=scan["repeated"],
        ),
        cases,
    )
    lineage_info = base["report"].get("lineage") or dict(depth=0)
    report = dict(
        intake=intake_summary,
        cases=cases,
        balance=checks,
        lineage=dict(
            root=root_id,
            prev=base_id,
            depth=lineage_info["depth"] + 1,
            affected=affected,
            changed_records=sorted(changed),
        ),
        manifest=dict(
            sha256=merged_fingerprint,
            supplement_sha256=supplement_fingerprint,
            version=RULE_VERSION,
            created=now(),
            duration_ms=round((monotonic() - start) * 1000, 3),
            bytes=len(data),
            run_id=run_id,
        ),
    )
    store.register(run_id, report)
    next_numbers = store.next_version_numbers(root_id, list(changed))
    try:
        store.commit_version(
            root_id,
            run_id,
            base_id,
            report["lineage"]["depth"],
            affected,
            [
                dict(
                    row=dict(row, _line=line_by_key.get(key)),
                    version=next_numbers[key],
                )
                for key, row in changed.items()
            ],
            report["manifest"]["created"],
        )
    except DomainIssue as error:
        if error.code != "LINEAGE_ADVANCED":
            raise
        # 并发补录已推进谱系：回滚孤儿批次，记录尝试，要求调用方基于新链头重试。
        store.discard(run_id)
        store.record_attempt(
            root_id, base_id, "BLOCKED", "LINEAGE_ADVANCED", [], report["manifest"]["created"]
        )
        raise
    store.record_attempt(
        root_id, base_id, "ADVANCED", "SUPPLEMENT", [], report["manifest"]["created"]
    )
    return dict(status="ADVANCED", run=store.load(run_id), changed=sorted(changed))


def _blocked(store, root_id, base_id, code, blockers):
    store.record_attempt(root_id, base_id, "BLOCKED", code, blockers, now())
    return DomainIssue(code, dict(blockers=blockers))
