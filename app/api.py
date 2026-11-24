from flask import Blueprint, Response, current_app, jsonify, request

from app.business import DomainIssue
from app.evidence.render import artifact
from app.inputs import read_input
from app.modules.pipeline import run
from app.review.service import execute, review

api = Blueprint("api", __name__, url_prefix="/api")
STAGES = {"intake": 1, "prepare": 2, "calculate": 3, "policy": 4, "balance": 5}


@api.post("/stages/<stage>")
def preview(stage):
    if stage not in STAGES:
        raise DomainIssue("STAGE_UNKNOWN")
    return jsonify(run(read_input(), STAGES[stage]))


@api.route("/runs", methods=["GET", "POST"])
def runs():
    store = current_app.extensions["runs"]
    if request.method == "GET":
        return jsonify(items=store.list())
    return jsonify(execute(store, read_input()))


@api.post("/runs/<run_id>/supplements")
def supplement(run_id):
    """把迟到来源记录作为补录挂到指定批次的谱系上。"""
    store = current_app.extensions["runs"]
    outcome = store.supplement(run_id, read_input())
    return jsonify(outcome)


@api.get("/runs/<run_id>/lineage")
def lineage(run_id):
    store = current_app.extensions["runs"]
    store.load(run_id)  # 未知批次返回 404
    return jsonify(store.lineage(store.root_of(run_id)))


@api.get("/runs/<run_id>/records/<record_key>/versions")
def record_versions(run_id, record_key):
    store = current_app.extensions["runs"]
    store.load(run_id)
    return jsonify(
        record=record_key,
        versions=store.record_versions(store.root_of(run_id), record_key),
    )


@api.get("/runs/<run_id>/attempts")
def attempts(run_id):
    store = current_app.extensions["runs"]
    store.load(run_id)
    return jsonify(items=store.attempts(store.root_of(run_id)))


@api.route("/runs/<run_id>", methods=["GET", "PATCH"])
def batch(run_id):
    store = current_app.extensions["runs"]
    if request.method == "GET":
        return jsonify(store.load(run_id))
    return jsonify(review(store, run_id, request.get_json(silent=True)))


@api.get("/runs/<run_id>/evidence")
def export(run_id):
    report = current_app.extensions["runs"].load(run_id)
    return Response(
        artifact(report),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="review-{report["id"][:12]}.json"',
            "Cache-Control": "no-store",
        },
    )
