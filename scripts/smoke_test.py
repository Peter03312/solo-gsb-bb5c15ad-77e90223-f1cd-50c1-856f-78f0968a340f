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

    # ---- batch reshuffle: validation ---------------------------------------
    layer5 = make_layer("reshuffle")
    l5 = layer5["id"]
    j1 = make_experiment(l5, "J1", 0, 4000, groups=[{"name": "all", "weight": 10000}])
    j2 = make_experiment(l5, "J2", 4000, 7000, groups=[{"name": "all", "weight": 10000}])
    j3 = make_experiment(l5, "J3", 7000, 10000, groups=[{"name": "all", "weight": 10000}])
    req("POST", f"/experiments/{j1['id']}/publish")
    req("POST", f"/experiments/{j2['id']}/publish")

    def reshuffle(items, layer=l5):
        return req("POST", f"/layers/{layer}/reshuffle",
                   {"layer_id": layer, "items": items})

    s, r = reshuffle([{"experiment_id": j1["id"], "expected_version": 1,
                       "bucket_start": 0, "bucket_end": 4000},
                      {"experiment_id": j1["id"], "expected_version": 1,
                       "bucket_start": 4000, "bucket_end": 7000}])
    check("reshuffle: duplicate experiment in batch -> 422", s == 422, r)

    s, r = reshuffle([{"experiment_id": j3["id"], "expected_version": 0,
                       "bucket_start": 5000, "bucket_end": 5000}])
    check("reshuffle: empty target range -> 422", s == 422, r)

    s, r = reshuffle([{"experiment_id": j3["id"], "expected_version": 0,
                       "bucket_start": 0, "bucket_end": 10001}])
    check("reshuffle: out-of-bounds range -> 422", s == 422, r)

    s, r = reshuffle([{"experiment_id": j3["id"], "expected_version": 0,
                       "bucket_start": 3000, "bucket_end": 8000}])
    check("reshuffle: overlap with unlisted active -> 409", s == 409, r)

    s, r = reshuffle([{"experiment_id": j1["id"], "expected_version": 1,
                       "bucket_start": 0, "bucket_end": 6000},
                      {"experiment_id": j2["id"], "expected_version": 1,
                       "bucket_start": 5000, "bucket_end": 9000}])
    check("reshuffle: in-batch overlap -> 409", s == 409, r)

    s, r = reshuffle([{"experiment_id": j1["id"], "expected_version": 99,
                       "bucket_start": 0, "bucket_end": 4000}])
    check("reshuffle: stale expected_version -> 409", s == 409, r)

    s, r = reshuffle([{"experiment_id": str(uuid.uuid4()), "expected_version": 0,
                       "bucket_start": 0, "bucket_end": 4000}])
    check("reshuffle: unknown experiment -> 404", s == 404, r)

    s, r = reshuffle([{"experiment_id": j1["id"], "expected_version": 1,
                       "bucket_start": 0, "bucket_end": 4000}], layer=l2)
    check("reshuffle: experiment from another layer -> 409", s == 409, r)

    # failed batches must leave no partial change
    got1 = req("GET", f"/experiments/{j1['id']}")[1]
    got3 = req("GET", f"/experiments/{j3['id']}")[1]
    check("failed reshuffle leaves state untouched",
          got1["version"] == 1 and (got1["bucket_start"], got1["bucket_end"]) == (0, 4000)
          and got3["status"] == "draft", (got1, got3))

    req("POST", f"/experiments/{j2['id']}/end")
    s, r = reshuffle([{"experiment_id": j2["id"], "expected_version": 1,
                       "bucket_start": 4000, "bucket_end": 7000}])
    check("reshuffle: ended experiment -> 409", s == 409, r)

    # ---- batch reshuffle: swap active ranges + release draft ---------------
    old_user, old_r = find_user(l5, 0, 4000, {}, "old")
    check("old user assigned to J1 v1",
          old_r["experiment_id"] == j1["id"] and old_r["experiment_version"] == 1, old_r)
    old_asg = old_r["assignment_id"]

    s, r = reshuffle([
        # J1 moves into the range J2 freed when it ended
        {"experiment_id": j1["id"], "expected_version": 1,
         "bucket_start": 4000, "bucket_end": 7000},
        # draft J3 released by the batch, first version = 1
        {"experiment_id": j3["id"], "expected_version": 0,
         "bucket_start": 7000, "bucket_end": 10000},
    ])
    check("reshuffle: swap + draft release succeeds", s == 200, r)
    res = {x["id"]: x for x in r["results"]}
    check("J1 v2 moved, changed=true",
          res[j1["id"]]["version"] == 2
          and (res[j1["id"]]["bucket_start"], res[j1["id"]]["bucket_end"]) == (4000, 7000)
          and res[j1["id"]]["changed"] is True, res[j1["id"]])
    check("draft J3 becomes active v1",
          res[j3["id"]]["status"] == "active" and res[j3["id"]]["version"] == 1
          and res[j3["id"]]["changed"] is True, res[j3["id"]])

    s, r = assign(l5, old_user)
    check("old user keeps original assignment + v1 snapshot after reshuffle",
          r["assignment_id"] == old_asg and r["experiment_version"] == 1
          and (r["snapshot"]["bucket_start"], r["snapshot"]["bucket_end"]) == (0, 4000), r)

    _, new_r = find_user(l5, 4000, 7000, {}, "new")
    check("new user routed by new layout (J1 v2)",
          new_r["experiment_id"] == j1["id"] and new_r["experiment_version"] == 2
          and 4000 <= new_r["bucket"] < 7000, new_r)
    _, c_r = find_user(l5, 7000, 10000, {}, "newc")
    check("new user reaches released draft J3", c_r["experiment_id"] == j3["id"], c_r)

    s, r = reshuffle([{"experiment_id": j1["id"], "expected_version": 2,
                       "bucket_start": 4000, "bucket_end": 7000}])
    check("reshuffle no-op: version unchanged, changed=false",
          s == 200 and r["results"][0]["version"] == 2
          and r["results"][0]["changed"] is False, r)

    # ---- concurrency: competing reshuffles, exactly one wins ---------------
    layer6 = make_layer("race-reshuffle")
    l6 = layer6["id"]
    k1 = make_experiment(l6, "K1", 0, 5000, groups=[{"name": "all", "weight": 10000}])
    k2 = make_experiment(l6, "K2", 5000, 10000, groups=[{"name": "all", "weight": 10000}])
    req("POST", f"/experiments/{k1['id']}/publish")
    req("POST", f"/experiments/{k2['id']}/publish")
    plan_a = [{"experiment_id": k1["id"], "expected_version": 1,
               "bucket_start": 0, "bucket_end": 3000},
              {"experiment_id": k2["id"], "expected_version": 1,
               "bucket_start": 3000, "bucket_end": 10000}]
    plan_b = [{"experiment_id": k1["id"], "expected_version": 1,
               "bucket_start": 0, "bucket_end": 7000},
              {"experiment_id": k2["id"], "expected_version": 1,
               "bucket_start": 7000, "bucket_end": 10000}]
    with ThreadPoolExecutor(max_workers=2) as ex:
        rr = list(ex.map(lambda plan: reshuffle(plan, layer=l6), [plan_a, plan_b]))
    codes = sorted(s for s, _ in rr)
    check("concurrent reshuffles: exactly one wins (stale loser -> 409)",
          codes == [200, 409], rr)
    s, rows = req("GET", f"/layers/{l6}/experiments")
    active = sorted((e["bucket_start"], e["bucket_end"]) for e in rows if e["status"] == "active")
    check("committed layout still partitions the layer without overlap",
          active == [(0, 3000), (3000, 10000)] or active == [(0, 7000), (7000, 10000)], active)

    # ---- concurrency: reshuffle vs assign never lands out of range ----------
    layer7 = make_layer("race-reshuffle-assign")
    l7 = layer7["id"]
    m1 = make_experiment(l7, "M1", 0, 5000, groups=[{"name": "all", "weight": 10000}])
    m2 = make_experiment(l7, "M2", 5000, 10000, groups=[{"name": "all", "weight": 10000}])
    req("POST", f"/experiments/{m1['id']}/publish")
    req("POST", f"/experiments/{m2['id']}/publish")
    move = [{"experiment_id": m1["id"], "expected_version": 1,
             "bucket_start": 5000, "bucket_end": 10000},
            {"experiment_id": m2["id"], "expected_version": 1,
             "bucket_start": 0, "bucket_end": 5000}]
    ok_swap = True
    detail = ""
    for round_no in range(5):
        users = [f"swap-{round_no}-{uuid.uuid4().hex[:8]}-{i}" for i in range(20)]
        with ThreadPoolExecutor(max_workers=12) as ex:
            assign_futs = [ex.submit(assign, l7, u) for u in users]
            swap_futs = [ex.submit(reshuffle, move, l7) for _ in range(2)]
            swap_results = [f.result() for f in swap_futs]
            [f.result() for f in assign_futs]
        # re-arm expected versions for another round (winner bumps, loser 409s)
        for sid, body in swap_results:
            if sid == 200:
                for item, got in zip(move, body["results"]):
                    item["expected_version"] = got["version"]
        for u in users:
            s, rows = req("GET", f"/layers/{l7}/assignments?user_key={u}")
            for a in rows:
                exp = req("GET", f"/experiments/{a['experiment_id']}")[1]
                snap = (a["snapshot"]["bucket_start"], a["snapshot"]["bucket_end"])
                # Every assignment is always valid against its own stored
                # snapshot (old users keep their pre-switch allocation)...
                if not (snap[0] <= a["bucket"] < snap[1]):
                    ok_swap = False
                    detail = f"user {u}: bucket {a['bucket']} outside its snapshot {snap}"
                # ...and a NEW assignment at the experiment's current version
                # must lie inside the post-switch range.
                if a["experiment_version"] == exp["version"] and not (
                    exp["bucket_start"] <= a["bucket"] < exp["bucket_end"]
                ):
                    ok_swap = False
                    detail = (f"user {u}: post-switch assignment v{a['experiment_version']} "
                              f"bucket {a['bucket']} outside current "
                              f"{exp['bucket_start']}-{exp['bucket_end']}")
        if not ok_swap:
            break
    check("reshuffle-vs-assign race: no assignment outside current range", ok_swap, detail)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
