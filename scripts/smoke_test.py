#!/usr/bin/env python3
"""End-to-end smoke test for the experiment bucketing service (stdlib only).

Covers: layer/experiment CRUD, publish overlap validation, stable bucketing,
condition matching, group weights, assignment persistence across config
changes, experiment end semantics, exposure idempotency/conflict/rejection,
and the concurrency guarantees (parallel publish, parallel assign,
assign-vs-end race).

Usage:
    BASE_URL=http://127.0.0.1:8000 python3 scripts/smoke_test.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
PASS = 0
FAIL = 0


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL   {name}   {extra}")


def make_layer(tag):
    s, layer = req("POST", "/layers", {"name": f"{tag}-{uuid.uuid4().hex[:8]}"})
    assert s == 201, layer
    return layer


def make_experiment(layer_id, name, start, end, conditions=None, groups=None):
    body = {
        "layer_id": layer_id,
        "name": name,
        "bucket_start": start,
        "bucket_end": end,
        "conditions": conditions or {},
        "groups": groups or [{"name": "control", "weight": 5000},
                             {"name": "treatment", "weight": 5000}],
    }
    s, exp = req("POST", "/experiments", body)
    assert s == 201, exp
    return exp


def assign(layer_id, user_key, attributes=None):
    return req("POST", f"/layers/{layer_id}/assignments",
               {"user_key": user_key, "attributes": attributes or {}})


def find_user(layer_id, lo, hi, attributes=None, prefix="u"):
    """Find a user_key whose stable bucket falls in [lo, hi)."""
    for i in range(500):
        key = f"{prefix}-{uuid.uuid4().hex[:10]}-{i}"
        s, r = assign(layer_id, key, attributes)
        assert s == 200, r
        if lo <= r["bucket"] < hi:
            return key, r
    raise AssertionError(f"no user found in [{lo},{hi})")


def main():
    print(f"target: {BASE}")

    # ---- health / validation ---------------------------------------------
    s, r = req("GET", "/health")
    check("health", s == 200 and r["status"] == "ok")

    layer = make_layer("main")
    lid = layer["id"]
    check("layer has fixed salt", bool(layer.get("salt")))

    s, r = req("POST", "/experiments", {
        "layer_id": lid, "name": "bad-weights", "bucket_start": 0, "bucket_end": 100,
        "groups": [{"name": "a", "weight": 9000}]})
    check("weights must sum to 10000 -> 422", s == 422, r)

    s, r = req("POST", "/experiments", {
        "layer_id": lid, "name": "bad-range", "bucket_start": 500, "bucket_end": 500,
        "groups": [{"name": "a", "weight": 10000}]})
    check("empty range -> 422", s == 422, r)

    # ---- publish + atomic overlap validation ------------------------------
    e1 = make_experiment(lid, "E1", 0, 5000, conditions={"country": "US"})
    s, r = req("POST", f"/experiments/{e1['id']}/publish")
    check("publish E1", s == 200 and r["status"] == "active" and r["version"] == 1, r)

    e_overlap = make_experiment(lid, "E-overlap", 4000, 6000)
    s, r = req("POST", f"/experiments/{e_overlap['id']}/publish")
    check("overlapping publish -> 409", s == 409, r)

    s, r = req("PUT", f"/experiments/{e_overlap['id']}",
               {"bucket_start": 5000, "bucket_end": 10000})
    check("shrink draft range", s == 200, r)
    s, r = req("POST", f"/experiments/{e_overlap['id']}/publish")
    check("adjacent [5000,10000) publish ok", s == 200, r)
    e2 = r

    # ---- assignment: bucket, conditions, groups, snapshot ------------------
    us_attrs = {"country": "US"}
    user_a, r = find_user(lid, 0, 5000, us_attrs, "a")
    check("user A assigned to E1", r.get("assigned") and r["experiment_id"] == e1["id"], r)
    check("bucket inside snapshot range",
          r["snapshot"]["bucket_start"] <= r["bucket"] < r["snapshot"]["bucket_end"], r)
    check("group in snapshot groups",
          r["group"] in [g["name"] for g in r["snapshot"]["groups"]], r)
    check("snapshot carries version", r["experiment_version"] == 1 and r["snapshot"]["version"] == 1, r)
    asg_a = r["assignment_id"]

    user_b, r = find_user(lid, 5000, 10000, {}, "b")
    check("user B assigned to E2 (no conditions)", r.get("assigned") and r["experiment_id"] == e2["id"], r)

    user_c, r = find_user(lid, 0, 5000, {"country": "CN"}, "c")
    check("condition mismatch -> unassigned",
          r.get("assigned") is False and r["reason"] == "conditions_not_matched", r)

    s, r = assign(lid, user_a, us_attrs)
    check("re-assign is stable (same id/group)",
          r["assignment_id"] == asg_a and r["assigned"], r)

    # ---- config update only affects not-yet-assigned users -----------------
    s, r = req("PUT", f"/experiments/{e1['id']}",
               {"groups": [{"name": "control", "weight": 3000},
                           {"name": "treatment", "weight": 7000}]})
    check("update active experiment bumps version", s == 200 and r["version"] == 2, r)

    s, r = assign(lid, user_a, us_attrs)
    check("old user keeps old assignment & snapshot version",
          r["assignment_id"] == asg_a and r["experiment_version"] == 1, r)

    user_d, r = find_user(lid, 0, 5000, us_attrs, "d")
    check("new user gets new version", r.get("assigned") and r["experiment_version"] == 2, r)

    # ---- exposures ----------------------------------------------------------
    s, r = req("POST", "/exposures",
               {"assignment_id": asg_a, "event_no": "evt-1", "content": {"banner": "v1"}})
    check("exposure registered", s == 200 and r["deduplicated"] is False, r)
    s, r = req("POST", "/exposures",
               {"assignment_id": asg_a, "event_no": "evt-1", "content": {"banner": "v1"}})
    check("same event_no + same content -> idempotent", s == 200 and r["deduplicated"] is True, r)
    s, r = req("POST", "/exposures",
               {"assignment_id": asg_a, "event_no": "evt-1", "content": {"banner": "v2"}})
    check("same event_no + different content -> 409", s == 409, r)
    s, r = req("POST", "/exposures",
               {"assignment_id": str(uuid.uuid4()), "event_no": "evt-1", "content": {}})
    check("exposure on unknown assignment -> 404", s == 404, r)

    # ---- end experiment ------------------------------------------------------
    s, r = req("POST", f"/experiments/{e1['id']}/end")
    check("end E1", s == 200 and r["status"] == "ended", r)

    s, r = req("GET", f"/assignments/{asg_a}")
    check("old assignment still queryable (closed)",
          s == 200 and r["status"] == "closed" and r["group"], r)

    s, r = req("POST", "/exposures",
               {"assignment_id": asg_a, "event_no": "evt-2", "content": {}})
    check("exposure on ended assignment -> 409", s == 409, r)

    s, r = assign(lid, user_a, us_attrs)
    check("no new assignment after end (range uncovered)",
          r.get("assigned") is False and r["reason"] == "no_experiment_covers_bucket", r)

    e3 = make_experiment(lid, "E3", 0, 5000)
    s, r = req("POST", f"/experiments/{e3['id']}/publish")
    check("E3 publishes into freed range", s == 200, r)

    s, r = assign(lid, user_a, us_attrs)
    check("user enters another experiment after end",
          r.get("assigned") and r["experiment_id"] == e3["id"] and r["assignment_id"] != asg_a, r)

    s, r = req("GET", f"/layers/{lid}/assignments?user_key={user_a}")
    active = [a for a in r if a["status"] == "active"]
    check("user has exactly one active assignment", len(r) == 2 and len(active) == 1, r)

    # ---- concurrency: parallel publish must not overlap ----------------------
    layer2 = make_layer("race-publish")
    l2 = layer2["id"]
    f1 = make_experiment(l2, "F1", 0, 5000)
    f2 = make_experiment(l2, "F2", 2500, 7500)
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(
            lambda e: req("POST", f"/experiments/{e}/publish"), [f1["id"], f2["id"]]))
    codes = sorted(s for s, _ in results)
    check("concurrent overlapping publish: exactly one wins", codes == [200, 409], results)

    # ---- concurrency: parallel assign of same user ---------------------------
    layer3 = make_layer("race-assign")
    l3 = layer3["id"]
    g1 = make_experiment(l3, "G1", 0, 10000, groups=[{"name": "all", "weight": 10000}])
    req("POST", f"/experiments/{g1['id']}/publish")
    race_user = f"race-{uuid.uuid4().hex[:8]}"
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda _: assign(l3, race_user), range(16)))
    ids = {r[1].get("assignment_id") for r in results if r[1].get("assigned")}
    check("16 concurrent assigns -> single assignment", len(ids) == 1, ids)

    # ---- concurrency: assign vs end leaves no valid assignment ---------------
    layer4 = make_layer("race-end")
    l4 = layer4["id"]
    ok = True
    detail = ""
    for round_no in range(5):
        h = make_experiment(l4, f"H{round_no}", 0, 10000,
                            groups=[{"name": "all", "weight": 10000}])
        req("POST", f"/experiments/{h['id']}/publish")
        users = [f"endrace-{round_no}-{uuid.uuid4().hex[:6]}-{i}" for i in range(8)]
        same_user = f"endrace-same-{round_no}-{uuid.uuid4().hex[:6]}"
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(assign, l4, u) for u in users]
            futs += [ex.submit(assign, l4, same_user) for _ in range(4)]
            end_fut = ex.submit(req, "POST", f"/experiments/{h['id']}/end")
            assigns = [f.result() for f in futs]
            end_s, end_r = end_fut.result()
        if end_s != 200:
            ok, detail = False, f"end failed: {end_s} {end_r}"
            break
        for u in users + [same_user]:
            s, rows = req("GET", f"/layers/{l4}/assignments?user_key={u}")
            actives = [a for a in rows if a["status"] == "active"]
            bad = [a for a in rows if a["experiment_id"] == h["id"] and a["status"] != "closed"]
            if len(actives) > 1 or bad:
                ok, detail = False, f"user {u}: actives={len(actives)} bad={bad}"
                break
        if not ok:
            break
    check("assign-vs-end race: no user left with a valid assignment of ended experiment", ok, detail)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
