"""The local API: who may call it, what it may touch, and the approval pause.

The server runs generated code, so the security tests come first: another
web page in the same browser, a DNS-rebinding page, or a path outside the
workspace roots must all be refused. The job tests use an injected runner,
so they exercise the API without a model or a sandbox; the one end-to-end
build through HTTP is marked slow.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from codecrew.agents import Spec
from codecrew.server import JobManager, create_server
from codecrew.server.jobs import JobError

SPEC = {
    "summary": "Clamp a number.", "module": "clamp",
    "functions": [{"signature": "clamp(value: float, low: float, high: float) -> float",
                   "description": "Limit value.",
                   "examples": [{"call": "clamp(5, 0, 10)", "result": "5"}]}],
    "assumptions": ["bounds are inclusive"],
}
REPLAYS = Path(__file__).resolve().parents[1] / "samples" / "replays"


def approving_runner(job, manager):
    """Stands in for the pipeline: asks for one spec approval, reports it."""
    decision = job.request_approval("spec", SPEC, manager.approval_timeout,
                                    module="clamp")
    job.add_event({"kind": "gate_progress", "phase": "end", "gate": "G1",
                   "status": "PASS"})
    if decision is None:
        return {"verdict": "FAILED TO BUILD", "failure": "spec rejected"}
    return {"verdict": "VERIFIED", "module": decision.module,
            "summary": decision.summary}


def wait_for(job, statuses, timeout=10.0):
    deadline = time.monotonic() + timeout
    while job.status not in statuses:
        if time.monotonic() > deadline:
            raise AssertionError(f"job stuck in {job.status}")
        time.sleep(0.02)


@pytest.fixture
def manager(tmp_path: Path):
    m = JobManager([tmp_path / "ws"], runner=approving_runner,
                   approval_timeout=5.0, replays_dir=REPLAYS)
    yield m
    m.shutdown()


@pytest.fixture
def server(manager, tmp_path: Path):
    ui = tmp_path / "ui"
    (ui / "job").mkdir(parents=True)
    (ui / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (ui / "job" / "index.html").write_text("<h1>job</h1>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve", encoding="utf-8")
    srv = create_server(port=0, manager=manager, ui_dir=ui)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    hdrs = {"Host": f"127.0.0.1:{srv.server_address[1]}"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    payload = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw.decode("utf-8", "replace")
    return resp.status, dict(resp.getheaders()), data


# -- who may call it -------------------------------------------------------------

def test_health_reports_providers_replays_and_roots(server):
    status, _, data = request(server, "GET", "/api/health")
    assert status == 200
    assert any(p["name"] == "scripted" and p["ready"] for p in data["providers"])
    assert "clamp_multi.json" in data["replays"]
    assert data["roots"]


def test_a_rebinding_host_header_is_refused(server):
    status, _, data = request(server, "GET", "/api/health",
                              headers={"Host": "evil.example:8765"})
    assert status == 403 and "Host" in data["error"]


def test_another_sites_origin_is_refused(server):
    status, headers, _ = request(server, "POST", "/api/jobs",
                                 {"kind": "build", "requirement": "x"},
                                 headers={"Origin": "https://evil.example"})
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers


def test_the_dev_ui_origin_gets_cors_headers(server):
    status, headers, _ = request(server, "OPTIONS", "/api/jobs",
                                 headers={"Origin": "http://localhost:3000"})
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "http://localhost:3000"


def test_writes_must_be_json(server):
    status, _, _ = request(server, "POST", "/api/jobs", body=b"kind=build",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415


# -- what it may touch -------------------------------------------------------------------

def test_verify_outside_the_roots_is_refused(server, tmp_path):
    status, _, data = request(server, "POST", "/api/jobs",
                              {"kind": "verify", "workspace": str(tmp_path)})
    assert status == 403 and "outside" in data["error"]


def test_scripted_runs_only_replay_bundled_files(server):
    status, _, data = request(server, "POST", "/api/jobs", {
        "kind": "build", "requirement": "x", "provider": "scripted",
        "script": "../../secret.json"})
    assert status == 400 and "bundled replay" in data["error"]


@pytest.mark.parametrize("body,fragment", [
    ({"kind": "build", "requirement": ""}, "requirement"),
    ({"kind": "build", "requirement": "x", "mode": "swarm"}, "mode"),
    ({"kind": "build", "requirement": "x", "repair": 99}, "repair"),
    ({"kind": "build", "requirement": "x", "provider": "openai"}, "provider"),
    ({"kind": "deploy"}, "kind"),
])
def test_bad_job_requests_are_explained(server, body, fragment):
    status, _, data = request(server, "POST", "/api/jobs", body)
    assert status == 400 and fragment in data["error"]


def test_static_files_are_served_and_traversal_is_not(server):
    assert request(server, "GET", "/")[2] == "<h1>home</h1>"
    assert request(server, "GET", "/job/")[2] == "<h1>job</h1>"
    status, _, body = request(server, "GET", "/../secret.txt")
    assert status == 404 and "do not serve" not in str(body)
    status, _, body = request(server, "GET", "/%2e%2e/secret.txt")
    assert "do not serve" not in str(body)


# -- the approval pause --------------------------------------------------------------------

def test_a_build_pauses_for_approval_and_continues_on_approve(server, manager):
    status, _, created = request(server, "POST", "/api/jobs",
                                 {"kind": "build", "requirement": "clamp it"})
    assert status == 201
    job = manager.get(created["id"])
    wait_for(job, {"awaiting_approval"})

    _, _, detail = request(server, "GET", f"/api/jobs/{job.id}")
    assert detail["pending"]["type"] == "spec"
    assert detail["pending"]["payload"]["assumptions"] == ["bounds are inclusive"]

    edited = {**SPEC, "summary": "Clamp a number, edited by a person."}
    status, _, _ = request(server, "POST", f"/api/jobs/{job.id}/approval",
                           {"approve": True, "edited": edited})
    assert status == 200
    wait_for(job, {"done"})
    assert job.result["summary"] == "Clamp a number, edited by a person."

    _, _, detail = request(server, "GET", f"/api/jobs/{job.id}?since=0")
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds.index("approval_requested") < kinds.index("approval_decided")
    assert detail["next"] == len(detail["events"])


def test_an_invalid_edit_is_refused_and_the_job_keeps_waiting(server, manager):
    _, _, created = request(server, "POST", "/api/jobs",
                            {"kind": "build", "requirement": "clamp it"})
    job = manager.get(created["id"])
    wait_for(job, {"awaiting_approval"})
    status, _, data = request(server, "POST", f"/api/jobs/{job.id}/approval",
                              {"approve": True, "edited": {**SPEC, "module": "Bad Name"}})
    assert status == 400 and "module" in data["error"]
    assert job.status == "awaiting_approval"
    request(server, "POST", f"/api/jobs/{job.id}/approval", {"approve": False})
    wait_for(job, {"done"})
    assert job.result["failure"] == "spec rejected"


def test_an_unanswered_approval_times_out_as_a_rejection(tmp_path):
    manager = JobManager([tmp_path], runner=approving_runner, approval_timeout=0.2)
    job = manager.submit({"kind": "build", "requirement": "x"})
    wait_for(job, {"done"})
    assert job.result["failure"] == "spec rejected"
    assert any(e["kind"] == "approval_timeout" for e in job.events)
    manager.shutdown()


def test_approving_a_job_that_is_not_waiting_is_a_conflict(manager):
    job = manager.submit({"kind": "build", "requirement": "x"})
    wait_for(job, {"awaiting_approval"})
    job.decide(True, None)
    wait_for(job, {"done"})
    with pytest.raises(JobError) as info:
        job.decide(True, None)
    assert info.value.status == 409


def test_a_failing_runner_is_reported_not_swallowed(tmp_path):
    def broken(job, manager):
        raise RuntimeError("sandbox exploded")

    manager = JobManager([tmp_path], runner=broken)
    job = manager.submit({"kind": "build", "requirement": "x"})
    wait_for(job, {"failed"})
    assert job.error == "RuntimeError: sandbox exploded"
    manager.shutdown()


def test_jobs_run_one_at_a_time_and_queued_ones_can_be_cancelled(manager):
    first = manager.submit({"kind": "build", "requirement": "first"})
    second = manager.submit({"kind": "build", "requirement": "second"})
    wait_for(first, {"awaiting_approval"})
    assert second.status == "queued"
    manager.cancel(second.id)
    first.decide(True, None)
    wait_for(first, {"done"})
    time.sleep(0.1)
    assert second.status == "cancelled" and second.result is None


def test_events_are_size_capped(manager):
    job = manager.submit({"kind": "build", "requirement": "x"})
    job.add_event({"kind": "llm_call", "prompt": "x" * 50_000})
    assert len(job.events[-1]["prompt"]) < 2_100
    wait_for(job, {"awaiting_approval"})
    manager.cancel(job.id)


# -- end to end ------------------------------------------------------------------------------

@pytest.mark.slow
def test_a_real_build_through_the_api_reaches_a_verdict(tmp_path):
    """Scripted model, real gates, the approval answered over HTTP."""
    manager = JobManager([tmp_path / "ws"], replays_dir=REPLAYS, approval_timeout=60)
    srv = create_server(port=0, manager=manager)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, _, created = request(srv, "POST", "/api/jobs", {
            "kind": "build", "requirement": "Clamp a number into a range",
            "mode": "multi", "provider": "scripted", "script": "clamp_multi.json",
            "repair": 0, "approval": "review"})
        job = manager.get(created["id"])
        wait_for(job, {"awaiting_approval"}, timeout=60)
        assert Spec.from_dict(job.pending["payload"]).module == "clamp"
        request(srv, "POST", f"/api/jobs/{job.id}/approval", {"approve": True})
        wait_for(job, {"done", "failed"}, timeout=600)
        assert job.status == "done", job.error

        result = job.result
        assert result["verdict"] == "VERIFIED"
        assert result["verdict_note"] is None  # a person approved the spec
        assert [b["path"] for b in result["badges"]] == ["clamp.py"]
        assert "Every example below was executed" in result["usage"]
        gates = [e["gate"] for e in job.events
                 if e.get("kind") == "gate_progress" and e.get("phase") == "end"]
        assert gates[:2] == ["G1", "G8"]

        _, _, listing = request(srv, "GET", "/api/workspaces")
        assert listing["workspaces"][0]["verdict"] == "VERIFIED"
        _, _, again = request(srv, "GET", "/api/workspace?path="
                              + listing["workspaces"][0]["path"])
        assert again["verdict"] == "VERIFIED" and again["badges"]
    finally:
        srv.shutdown()
        srv.server_close()
        manager.shutdown()
