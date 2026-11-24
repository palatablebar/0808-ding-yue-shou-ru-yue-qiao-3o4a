from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.business import DomainIssue
from app.review.service import execute, review
from conftest import csv_data
from test_lineage import change, new_basis_c1, two_cases


def test_parallel_rerun_commits_single_row(store, clean):
    start = Barrier(4)

    def load():
        start.wait(3)
        return execute(store, clean)

    with ThreadPoolExecutor(max_workers=4) as threads:
        results = list(threads.map(lambda _: load(), range(4)))
    assert all(result == results[0] for result in results)
    assert len(store.list()) == 1


def test_parallel_review_has_one_winner(store, dirty):
    run = execute(store, dirty)
    start = Barrier(2)

    def attempt(note):
        start.wait(3)
        try:
            return review(store, run["id"], dict(revision=0, status="HELD", note=note))[
                "status"
            ]
        except DomainIssue as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(attempt, ["来源待检查", "分组待处理"]))
    assert sorted(results) == ["HELD", "STALE_REVIEW"]
    assert store.load(run["id"])["revision"] == 1


def test_parallel_supplements_leave_one_consistent_chain(store):
    first = execute(store, csv_data(two_cases()))
    review(
        store,
        first["id"],
        dict(revision=0, status="APPROVED", note="首发批次完成签发"),
    )
    # 两份改不同记录键的补录，同时基于同一已签发批次，重试时仍会产生差异。
    payloads = {
        "c1": csv_data(new_basis_c1()),
        "c2": csv_data([change("R202", "C2", 300, 80, 1)]),
    }
    start = Barrier(2)

    def attempt(item):
        name, payload = item
        start.wait(3)
        try:
            return (name, "OK", store.supplement(first["id"], payload)["run"]["id"])
        except DomainIssue as error:
            return (name, error.code, None)

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(attempt, list(payloads.items())))
    assert sorted(code for _, code, _ in results) == ["LINEAGE_ADVANCED", "OK"]
    # 胜出的补录形成唯一链头，且没有孤儿批次。
    chain = store.lineage(first["id"])
    assert len(chain["versions"]) == 2
    winner_name, _, winner = next(
        (name, code, run_id) for name, code, run_id in results if code == "OK"
    )
    assert chain["head_run_id"] == winner
    assert len(store.list()) == 2
    # 失败者基于新链头干净重试，链深再进一层。
    lost_name = next(name for name, code, _ in results if code == "LINEAGE_ADVANCED")
    retried = store.supplement(winner, payloads[lost_name])
    assert retried["status"] == "ADVANCED"
    assert [v["depth"] for v in store.lineage(first["id"])["versions"]] == [0, 1, 2]
