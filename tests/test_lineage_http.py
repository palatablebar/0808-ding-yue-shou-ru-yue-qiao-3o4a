"""补录与谱系在 HTTP 工作台上端到端可用。"""

import io

from conftest import csv_data
from test_lineage import new_basis_c1, two_cases

def approve(client, run_id):
    response = client.patch(
        f"/api/runs/{run_id}",
        json=dict(revision=0, status="APPROVED", note="首发批次完成签发"),
    )
    assert response.status_code == 200


def upload_supplement(client, run_id, payload):
    return client.post(
        f"/api/runs/{run_id}/supplements",
        data={"file": (io.BytesIO(payload), "late.csv")},
    )


def test_supplement_advances_lineage_and_keeps_old_run_readable(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    approve(client, first["id"])
    response = upload_supplement(client, first["id"], csv_data(new_basis_c1()))
    assert response.status_code == 200
    advanced = response.json
    assert advanced["status"] == "ADVANCED"
    new_id = advanced["run"]["id"]

    chain = client.get(f"/api/runs/{first['id']}/lineage").json
    assert [v["run_id"] for v in chain["versions"]] == [first["id"], new_id]
    assert chain["head_run_id"] == new_id

    old = client.get(f"/api/runs/{first['id']}").json
    assert old["status"] == "APPROVED"
    c1 = next(c for c in old["report"]["cases"] if c["case"] == "C1")
    assert c1["measure"]["value"] == 120
    new = client.get(f"/api/runs/{new_id}").json
    c1_new = next(c for c in new["report"]["cases"] if c["case"] == "C1")
    assert c1_new["provenance"]["state"] == "RECALCULATED"


def test_unbased_backfill_returns_blockers_without_touching_signed_run(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    approve(client, first["id"])
    payload = csv_data([["R999", "C9", "opening", "0", "500", "0", "无依据"]])
    response = upload_supplement(client, first["id"], payload)
    assert response.status_code == 409
    body = response.json
    assert body["code"] == "UNBASED_BACKFILL"
    assert body["blockers"][0]["record"] == "R999"
    old = client.get(f"/api/runs/{first['id']}").json
    assert old["status"] == "APPROVED"
    attempts = client.get(f"/api/runs/{first['id']}/attempts").json["items"]
    assert attempts and attempts[-1]["result"] == "BLOCKED"


def test_contradictory_supplement_lists_all_participating_records(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    approve(client, first["id"])
    payload = csv_data(
        [
            ["R102", "C1", "change", "200", "130", "1", "补录甲"],
            ["R102", "C1", "change", "200", "140", "1", "补录乙"],
        ]
    )
    response = upload_supplement(client, first["id"], payload)
    assert response.status_code == 409 and response.json["code"] == "VERSION_CONFLICT"
    participants = response.json["blockers"][0]["participants"]
    assert {p["mrr_cents"] for p in participants} == {130, 140, 120}
    assert {p["line"] for p in participants if p["version"] == "incoming"} == {2, 3}


def test_record_version_history_endpoint(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    approve(client, first["id"])
    upload_supplement(client, first["id"], csv_data(new_basis_c1()))
    response = client.get(f"/api/runs/{first['id']}/records/R102/versions")
    assert response.status_code == 200
    versions = response.json["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["mrr_cents"] for v in versions] == [120, 130]


def test_unknown_record_and_run_are_404(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    assert (
        client.get(f"/api/runs/{first['id']}/records/NOPE/versions").status_code
        == 404
    )
    assert client.get("/api/runs/missing/lineage").status_code == 404


def test_supplement_evidence_contains_lineage_and_provenance(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    approve(client, first["id"])
    new_id = upload_supplement(
        client, first["id"], csv_data(new_basis_c1())
    ).json["run"]["id"]
    packet = client.get(f"/api/runs/{new_id}/evidence").json
    assert packet["schema"] == "review-evidence/2"
    assert packet["lineage"]["prev"] == first["id"]
    states = {c["case"]: c["provenance"]["state"] for c in packet["cases"]}
    assert states == {"C1": "RECALCULATED", "C2": "FROZEN"}


def test_replay_supplement_does_not_create_a_run(client):
    first = client.post(
        "/api/runs", data={"file": (io.BytesIO(csv_data(two_cases())), "src.csv")}
    ).json
    approve(client, first["id"])
    replay = csv_data([row for row in two_cases() if row[0] == "R102"])
    response = upload_supplement(client, first["id"], replay)
    assert response.status_code == 200 and response.json["status"] == "NO_CHANGE"
    assert len(client.get("/api/runs").json["items"]) == 1


def test_supplement_on_unknown_run_is_404(client):
    response = upload_supplement(client, "missing", csv_data(two_cases()))
    assert response.status_code == 404


def test_bundled_late_sample_advances_ready_batch(client):
    sample = client.get("/examples/late")
    assert sample.status_code == 200
    first = client.post("/api/runs", data={"sample": "clean"}).json
    client.patch(
        f"/api/runs/{first['id']}",
        json=dict(revision=0, status="APPROVED", note="首发批次完成签发"),
    )
    response = client.post(
        f"/api/runs/{first['id']}/supplements",
        data={"file": (io.BytesIO(sample.data), "late.csv")},
    )
    assert response.status_code == 200
    body = response.json
    assert body["status"] == "ADVANCED"
    assert body["run"]["report"]["lineage"]["affected"] == ["0001"]
    assert body["run"]["report"]["balance"]["releasable"] is True
