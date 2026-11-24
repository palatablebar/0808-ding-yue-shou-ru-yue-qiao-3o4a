"""来源版本谱系、增量重算与补录拦截规则。"""

import pytest

from app.business import DomainIssue
from app.review.service import execute, review
from conftest import csv_data


def row(record, case, kind, minute, mrr, sequence, note="来源"):
    return [record, case, kind, str(minute), str(mrr), str(sequence), note]


def opening(record, case, mrr):
    return row(record, case, "opening", 0, mrr, 0)


def close(record, case, mrr):
    return row(record, case, "close", 1440, mrr, 0)


def change(record, case, minute, mrr, sequence):
    return row(record, case, "change", minute, mrr, sequence)


def two_cases():
    return [
        opening("R101", "C1", 100),
        change("R102", "C1", 200, 120, 1),
        close("R103", "C1", 120),
        opening("R201", "C2", 50),
        change("R202", "C2", 300, 70, 1),
        close("R203", "C2", 70),
    ]


def new_basis_c1():
    # 新口径：变更事实与期末参考同时更新，重算后 C1 仍平衡
    return [
        change("R102", "C1", 200, 130, 1),
        close("R103", "C1", 130),
    ]


def approve(store, run_id, note="首发批次签发"):
    review(store, run_id, dict(revision=0, status="APPROVED", note=note))


# --- 谱系串联 -------------------------------------------------------------

def test_first_run_is_lineage_root_with_original_provenance(store):
    first = execute(store, csv_data(two_cases()))
    report = first["report"]
    assert report["lineage"] == dict(
        root=first["id"], prev=None, depth=0, affected=[], changed_records=[]
    )
    assert all(c["provenance"]["state"] == "ORIGINAL" for c in report["cases"])
    assert all(c["provenance"]["source_run"] == first["id"] for c in report["cases"])
    chain = store.lineage(first["id"])
    assert chain["root_run_id"] == first["id"]
    assert chain["versions"][0]["depth"] == 0 and chain["versions"][0]["affected"] == []


def test_seed_versions_keep_source_line_numbers(store):
    first = execute(store, csv_data(two_cases()))
    versions = store.record_versions(first["id"], "R102")
    assert versions[0]["version"] == 1 and versions[0]["source_line"] is not None
    assert versions[0]["source_line"] >= 2


def test_supplement_chains_new_run_after_approved_batch(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    outcome = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )
    new_run = outcome["run"]
    assert outcome["status"] == "ADVANCED"
    assert new_run["id"] != first["id"]
    assert new_run["report"]["lineage"]["root"] == first["id"]
    assert new_run["report"]["lineage"]["prev"] == first["id"]
    assert new_run["report"]["lineage"]["depth"] == 1
    old = store.load(first["id"])
    assert old["status"] == "APPROVED"
    assert old["report"] == first["report"]


def test_lineage_lists_every_version_in_order(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    second = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )["run"]
    chain = store.lineage(first["id"])
    assert [node["run_id"] for node in chain["versions"]] == [first["id"], second["id"]]
    assert chain["head_run_id"] == second["id"]
    assert chain["versions"][0]["status"] == "APPROVED"


def test_each_historical_version_is_independently_readable(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    store.supplement(first["id"], csv_data(new_basis_c1()))
    old = store.load(first["id"])
    c1 = next(c for c in old["report"]["cases"] if c["case"] == "C1")
    assert c1["measure"]["value"] == 120
    assert old["report"]["manifest"]["sha256"] == first["report"]["manifest"]["sha256"]


# --- 增量重算 -------------------------------------------------------------

def test_only_affected_business_group_is_recomputed(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    outcome = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )
    report = outcome["run"]["report"]
    assert report["lineage"]["affected"] == ["C1"]
    c1 = next(c for c in report["cases"] if c["case"] == "C1")
    c2 = next(c for c in report["cases"] if c["case"] == "C2")
    assert c1["provenance"]["state"] == "RECALCULATED"
    assert c1["measure"]["value"] == 130
    assert c2["provenance"]["state"] == "FROZEN"
    assert c2["provenance"]["source_run"] == first["id"]
    assert c2["measure"]["value"] == 70


def test_supplement_to_unapproved_batch_reuses_pending_lineage(store):
    first = execute(store, csv_data(two_cases()))
    outcome = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )
    assert outcome["run"]["report"]["lineage"]["root"] == first["id"]
    assert store.lineage(first["id"])["versions"][0]["status"] == "PENDING"


def test_repeated_supplements_extend_the_same_chain(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    second = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )["run"]
    third = store.supplement(
        second["id"],
        csv_data([change("R202", "C2", 300, 80, 1), close("R203", "C2", 80)]),
    )["run"]
    lineage = store.lineage(first["id"])
    assert [v["depth"] for v in lineage["versions"]] == [0, 1, 2]
    assert third["report"]["lineage"]["prev"] == second["id"]
    assert third["report"]["lineage"]["affected"] == ["C2"]
    c1 = next(c for c in third["report"]["cases"] if c["case"] == "C1")
    assert c1["provenance"]["state"] == "FROZEN"
    assert c1["provenance"]["source_run"] == second["id"]


# --- 无依据补录 -----------------------------------------------------------

def test_unbased_backfill_is_blocked_and_changes_nothing(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    with pytest.raises(DomainIssue, match="UNBASED_BACKFILL"):
        store.supplement(first["id"], csv_data([opening("R999", "C9", 500)]))
    assert [v["run_id"] for v in store.lineage(first["id"])["versions"]] == [first["id"]]
    old = store.load(first["id"])
    assert old["status"] == "APPROVED"
    assert "R999" not in {r["record"] for r in old["report"].get("rows", [])}


def test_unbased_backfill_locates_every_participating_record(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    payload = csv_data(
        [
            opening("R999", "C9", 500),
            change("R888", "C1", 100, 110, 1),
        ]
    )
    with pytest.raises(DomainIssue) as exc:
        store.supplement(first["id"], payload)
    assert exc.value.code == "UNBASED_BACKFILL"
    located = exc.value.details["blockers"]
    assert {item["record"] for item in located} == {"R999", "R888"}
    assert all(item["line"] >= 2 for item in located)


def test_blocked_supplement_is_kept_as_attempt_without_new_run(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    with pytest.raises(DomainIssue):
        store.supplement(first["id"], csv_data([opening("R999", "C9", 500)]))
    attempts = store.attempts(first["id"])
    assert len(attempts) == 1 and attempts[0]["result"] == "BLOCKED"
    assert len(store.list()) == 1


# --- 矛盾补录 -------------------------------------------------------------

def test_contradictory_supplement_blocks_release_and_locates_all(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    payload = csv_data(
        [
            change("R102", "C1", 200, 130, 1),
            change("R102", "C1", 200, 140, 1),
        ]
    )
    with pytest.raises(DomainIssue) as exc:
        store.supplement(first["id"], payload)
    assert exc.value.code == "VERSION_CONFLICT"
    participants = exc.value.details["blockers"][0]["participants"]
    keys = {(p["record"], p["line"], p["mrr_cents"]) for p in participants}
    assert ("R102", 2, 130) in keys and ("R102", 3, 140) in keys
    # 当前生效的旧版本（含其源行号）也必须被定位
    head = next(p for p in participants if p["version"] == 1)
    assert head["mrr_cents"] == 120 and head["line"] is not None
    assert [v["run_id"] for v in store.lineage(first["id"])["versions"]] == [first["id"]]


def test_contradiction_against_current_head_is_undecidable(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    # 与当前生效版本内容不同，且补录本身没有可判先后的次序信息
    with pytest.raises(DomainIssue, match="VERSION_CONFLICT"):
        store.supplement(
            first["id"],
            csv_data(
                [
                    change("R102", "C1", 200, 998, 1),
                    change("R102", "C1", 200, 999, 1),
                ]
            ),
        )


def test_identical_resubmission_is_replay_not_conflict(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    outcome = store.supplement(
        first["id"], csv_data([change("R102", "C1", 200, 120, 1)])
    )
    # 纯回放不产生新版本行，链头不前进
    assert outcome["status"] == "NO_CHANGE"
    assert [v["run_id"] for v in store.lineage(first["id"])["versions"]] == [first["id"]]


def test_blocked_lineage_cannot_advance_without_clean_supplement(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    with pytest.raises(DomainIssue):
        store.supplement(
            first["id"],
            csv_data([change("R102", "C1", 200, 130, 1), change("R102", "C1", 200, 140, 1)]),
        )
    # 干净补录仍可前进，证明阻断的是当次而不是整条链
    outcome = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )
    assert outcome["status"] == "ADVANCED"


# --- 版本回读 -------------------------------------------------------------

def test_version_history_exposes_every_revision_of_a_record(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    store.supplement(first["id"], csv_data(new_basis_c1()))
    versions = store.record_versions(first["id"], "R102")
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["mrr_cents"] for v in versions] == [120, 130]
    assert versions[0]["introduced_by"] == first["id"]


def test_supplement_keeps_balance_conservation(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    report = store.supplement(
        first["id"], csv_data(new_basis_c1())
    )["run"]["report"]
    balance = report["balance"]
    assert balance["balanced"] and balance["releasable"]
    assert sum(len(c["records"]) for c in report["cases"]) == balance["accepted"]


def test_supplement_against_unknown_run_is_rejected(store):
    with pytest.raises(DomainIssue, match="RUN_NOT_FOUND"):
        store.supplement("missing", csv_data(two_cases()))


# --- 签发闸门 -------------------------------------------------------------

def test_clean_recomputed_version_can_be_signed_and_old_version_stays_intact(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    # 先制造一次被阻断的尝试
    with pytest.raises(DomainIssue):
        store.supplement(first["id"], csv_data([opening("R999", "C9", 500)]))
    advanced = store.supplement(first["id"], csv_data(new_basis_c1()))["run"]
    assert advanced["report"]["balance"]["releasable"] is True
    signed = review(
        store,
        advanced["id"],
        dict(revision=0, status="APPROVED", note="新口径批次复核通过"),
    )
    assert signed["status"] == "APPROVED"
    # 首发批次的已签发结论与证据原样保留
    old = store.load(first["id"])
    assert old["status"] == "APPROVED"
    c1 = next(c for c in old["report"]["cases"] if c["case"] == "C1")
    assert c1["measure"]["value"] == 120


def test_recomputed_version_with_unresolved_difference_cannot_be_signed(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    # 只改变更事实、不更新期末参考 -> 重算后 MISMATCH，不可签发
    advanced = store.supplement(
        first["id"], csv_data([change("R102", "C1", 200, 130, 1)])
    )["run"]
    assert advanced["report"]["balance"]["releasable"] is False
    with pytest.raises(DomainIssue, match="UNRESOLVED_ISSUES"):
        review(
            store,
            advanced["id"],
            dict(revision=0, status="APPROVED", note="试图带病签发"),
        )
    assert store.load(advanced["id"])["revision"] == 0
    # 仍可暂缓并记录意见
    held = review(
        store,
        advanced["id"],
        dict(revision=0, status="HELD", note="新口径期末参考未更新"),
    )
    assert held["status"] == "HELD"


def test_blocked_supplement_never_creates_an_approvable_run(store):
    first = execute(store, csv_data(two_cases()))
    approve(store, first["id"])
    for payload, code in (
        (csv_data([opening("R999", "C9", 500)]), "UNBASED_BACKFILL"),
        (
            csv_data(
                [
                    change("R102", "C1", 200, 130, 1),
                    change("R102", "C1", 200, 140, 1),
                ]
            ),
            "VERSION_CONFLICT",
        ),
    ):
        with pytest.raises(DomainIssue, match=code):
            store.supplement(first["id"], payload)
    # 阻断全程没有产生任何新批次，可签发的只有首发批次
    runs = store.list()
    assert len(runs) == 1 and runs[0]["id"] == first["id"]
