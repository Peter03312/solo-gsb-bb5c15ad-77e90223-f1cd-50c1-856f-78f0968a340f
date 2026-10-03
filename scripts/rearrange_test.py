#!/usr/bin/env python3
"""End-to-end test for batch bucket rearrangement (stdlib only).

Covers POST /layers/{id}/rearrangements:

  * swap two active ranges in one batch; drafts included (published) in a batch;
  * response carries new status / version / range; only actually-changed
    experiments get a version increment (no-op range keeps the version);
  * already-assigned users keep their old assignment + snapshot; new users
    follow the post-switch layout;
  * atomic failures (no partial change): duplicate experiment in the batch,
    illegal range, stale expected_version (expired), experiment of another
    layer, ended experiment, listed-listed overlap, overlap with an unlisted
    active experiment;
  * concurrency: parallel rearrangement vs assign never produces an
    assignment outside the experiment's (old or new) range; two overlapping
    concurrent batches cannot both win.

Usage:
    BASE_URL=http://127.0.0.1:8000 python3 scripts/rearrange_test.py
"""
import hashlib
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
TOTAL_BUCKETS = 10000


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


def publish(eid):
    s, r = req("POST", f"/experiments/{eid}/publish")
    assert s == 200, r
    return r


def rearrange(layer_id, items):
    return req("POST", f"/layers/{layer_id}/rearrangements", {"items": items})


def assign(layer_id, user_key, attributes=None):
    return req("POST", f"/layers/{layer_id}/assignments",
               {"user_key": user_key, "attributes": attributes or {}})


def user_bucket(layer_salt, user_key):
    digest = hashlib.sha256("|".join([layer_salt, "bucket", user_key]).encode()).hexdigest()
    return int(digest, 16) % TOTAL_BUCKETS


def find_user(layer_salt, lo, hi, used, prefix):
    for i in range(2000):
        key = f"{prefix}-{uuid.uuid4().hex[:10]}-{i}"
        b = user_bucket(layer_salt, key)
        if lo <= b < hi and key not in used:
            return key, b
    raise AssertionError(f"no free user in [{lo},{hi})")


def by_id(resp, eid):
    return next(e for e in resp["experiments"] if e["id"] == eid)


def main():
    print(f"target: {BASE}")

    # ---------- 1. swap + draft in one atomic batch -----------------------
    layer = make_layer("swap")
    lid, salt = layer["id"], layer["salt"]

    e_a = publish(make_experiment(lid, "A", 0, 2500)["id"])
    e_b = publish(make_experiment(lid, "B", 2500, 5000)["id"])
    e_c = make_experiment(lid, "C-draft", 7500, 10000)  # still a draft

    used: set[str] = set()
    # an old user of A (in [0,2500)) and one of B (in [2500,5000))
    ua, ba = find_user(salt, 0, 2500, used, "ua")
    ub, bb = find_user(salt, 2500, 5000, used, "ub")
    used.update([ua, ub])
    s, ra = assign(lid, ua)
    assert s == 200 and ra["assigned"] and ra["experiment_id"] == e_a["id"], ra
    asg_a = ra["assignment_id"]
    s, rb = assign(lid, ub)
    assert s == 200 and rb["assigned"] and rb["experiment_id"] == e_b["id"], rb
    asg_b = rb["assignment_id"]

    # Batch: A -> B's old range, B -> A's old range (swap), draft C published.
    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 1,
         "bucket_start": 2500, "bucket_end": 5000},
        {"experiment_id": e_b["id"], "expected_version": 1,
         "bucket_start": 0, "bucket_end": 2500},
        {"experiment_id": e_c["id"], "expected_version": 0,
         "bucket_start": 7500, "bucket_end": 10000},
    ])
    check("batch swap+draft -> 200", s == 200, r)
    if s == 200:
        a, b, c = by_id(r, e_a["id"]), by_id(r, e_b["id"]), by_id(r, e_c["id"])
        check("A moved [2500,5000), version 2, changed",
              a["bucket_start"] == 2500 and a["bucket_end"] == 5000
              and a["version"] == 2 and a["status"] == "active" and a["changed"] is True, a)
        check("B moved [0,2500), version 2, changed",
              b["bucket_start"] == 0 and b["bucket_end"] == 2500
              and b["version"] == 2 and b["changed"] is True, b)
        check("C draft -> active, version 1, changed",
              c["status"] == "active" and c["version"] == 1 and c["changed"] is True, c)

    # old users keep their old assignment + old snapshot + old version
    s, ga = req("GET", f"/assignments/{asg_a}")
    s2, gb = req("GET", f"/assignments/{asg_b}")
    check("old A user keeps old assignment/snapshot (version 1, old range)",
          ga["experiment_id"] == e_a["id"] and ga["experiment_version"] == 1
          and ga["snapshot"]["bucket_start"] == 0 and ga["snapshot"]["bucket_end"] == 2500
          and ga["status"] == "active", ga)
    check("old B user keeps old assignment/snapshot (version 1, old range)",
          gb["experiment_id"] == e_b["id"] and gb["experiment_version"] == 1
          and gb["snapshot"]["bucket_start"] == 2500 and gb["snapshot"]["bucket_end"] == 5000, gb)

    # a NEW user in [0,2500) now lands in B (new layout); one in [7500,10000) in C
    un0, _ = find_user(salt, 0, 2500, used, "un0")
    s, rn = assign(lid, un0)
    check("new user in [0,2500) -> B (new layout), version 2",
          rn.get("assigned") and rn["experiment_id"] == e_b["id"]
          and rn["experiment_version"] == 2, rn)
    un1, _ = find_user(salt, 7500, 10000, used, "un1")
    s, rn1 = assign(lid, un1)
    check("new user in [7500,10000) -> freshly published C, version 1",
          rn1.get("assigned") and rn1["experiment_id"] == e_c["id"]
          and rn1["experiment_version"] == 1, rn1)
    # [5000,7500) is uncovered -> no assignment
    un2, _ = find_user(salt, 5000, 7500, used, "un2")
    s, rn2 = assign(lid, un2)
    check("uncovered gap -> assigned=false",
          rn2.get("assigned") is False
          and rn2["reason"] == "no_experiment_covers_bucket", rn2)

    # ---------- 2. no-op range keeps version --------------------------------
    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 2,
         "bucket_start": 2500, "bucket_end": 5000},
    ])
    check("same range -> 200, no version bump, changed=false",
          s == 200 and by_id(r, e_a["id"])["version"] == 2
          and by_id(r, e_a["id"])["changed"] is False, r)

    # ---------- 3. atomic failures (no partial change) ---------------------
    before = {x["id"]: x for x in req("GET", f"/layers/{lid}/experiments")[1]}

    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 2,
         "bucket_start": 5000, "bucket_end": 7500},
        {"experiment_id": e_a["id"], "expected_version": 2,  # duplicate in batch
         "bucket_start": 100, "bucket_end": 200},
    ])
    check("duplicate experiment in batch -> 422", s == 422, r)

    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 2,
         "bucket_start": 9000, "bucket_end": 9000},  # empty range
    ])
    check("illegal empty range -> 422", s == 422, r)

    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 99,  # stale/expired version
         "bucket_start": 5000, "bucket_end": 7500},
    ])
    check("expected_version mismatch -> 409", s == 409, r)

    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 2,
         "bucket_start": 0, "bucket_end": 2500},  # overlaps B (active, listed too?)
        {"experiment_id": e_b["id"], "expected_version": 2,
         "bucket_start": 100, "bucket_end": 200},
    ])
    check("listed ranges overlap each other -> 409", s == 409, r)

    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 2,
         "bucket_start": 0, "bucket_end": 1000},  # B occupies [0,2500)
    ])
    check("overlap with unlisted active experiment -> 409", s == 409, r)

    # a multi-item batch where only one item is bad -> nothing changes at all
    s, r = rearrange(lid, [
        {"experiment_id": e_a["id"], "expected_version": 2,
         "bucket_start": 5000, "bucket_end": 7500},
        {"experiment_id": e_b["id"], "expected_version": 2,
         "bucket_start": 0, "bucket_end": 100},   # collides with... nothing free?
        {"experiment_id": e_c["id"], "expected_version": 1,
         "bucket_start": 0, "bucket_end": 50},    # overlaps e_b's target above
    ])
    check("mixed bad batch -> 409 and zero partial changes", s == 409, r)

    after = {x["id"]: x for x in req("GET", f"/layers/{lid}/experiments")[1]}
    same = all(
        before[i]["version"] == after[i]["version"]
        and before[i]["bucket_start"] == after[i]["bucket_start"]
        and before[i]["bucket_end"] == after[i]["bucket_end"]
        and before[i]["status"] == after[i]["status"]
        for i in before
    )
    check("layout unchanged after all failed batches", same,
          {i: (before[i]["version"], before[i]["bucket_start"], before[i]["bucket_end"],
               after[i]["version"], after[i]["bucket_start"], after[i]["bucket_end"])
           for i in before if (before[i]["version"], before[i]["bucket_start"],
                               before[i]["bucket_end"]) !=
           (after[i]["version"], after[i]["bucket_start"], after[i]["bucket_end"])})

    # ---------- 4. cross-layer & ended experiments rejected ----------------
    other = make_layer("other")
    foreign = publish(make_experiment(other["id"], "X", 0, 1000)["id"])
    s, r = rearrange(lid, [
        {"experiment_id": foreign["id"], "expected_version": 1,
         "bucket_start": 5000, "bucket_end": 6000},
    ])
    check("experiment of another layer -> 409", s == 409, r)

    s, r = rearrange(other["id"], [
        {"experiment_id": foreign["id"], "expected_version": 1,
         "bucket_start": 0, "bucket_end": 1000},
    ])
    check("own layer + correct version -> 200", s == 200, r)
    req("POST", f"/experiments/{foreign['id']}/end")
    s, r = rearrange(other["id"], [
        {"experiment_id": foreign["id"], "expected_version": 1,
         "bucket_start": 1000, "bucket_end": 2000},
    ])
    check("ended experiment -> 409", s == 409, r)

    s, r = rearrange(lid, [
        {"experiment_id": str(uuid.uuid4()), "expected_version": 0,
         "bucket_start": 0, "bucket_end": 100},
    ])
    check("unknown experiment -> 404", s == 404, r)

    s, r = req("POST", f"/layers/{uuid.uuid4()}/rearrangements",
               {"items": [{"experiment_id": str(uuid.uuid4()), "expected_version": 0,
                           "bucket_start": 0, "bucket_end": 100}]})
    check("unknown layer -> 404", s == 404, r)

    # ---------- 5. concurrency: assign vs rearrangement --------------------
    layer5 = make_layer("race-move")
    l5, salt5 = layer5["id"], layer5["salt"]
    e_m = publish(make_experiment(l5, "M", 0, 5000)["id"])

    # hammer assignments for many fresh users while M moves [0,5000)->[5000,10000)
    fresh = [f"mv-{uuid.uuid4().hex[:10]}-{i}" for i in range(400)]
    violations: list = []

    def one_assign(key):
        s, r = assign(l5, key)
        if s != 200 or not r.get("assigned"):
            return
        b = r["bucket"]
        ver = r["experiment_version"]
        # version 1  => old range [0,5000); version 2 => new range [5000,10000)
        if ver == 1 and not (0 <= b < 5000):
            violations.append((key, b, ver, "v1 outside old range"))
        if ver == 2 and not (5000 <= b < 10000):
            violations.append((key, b, ver, "v2 outside new range"))

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(one_assign, k) for k in fresh]
        # move halfway through the storm
        move_fut = ex.submit(rearrange, l5, [
            {"experiment_id": e_m["id"], "expected_version": 1,
             "bucket_start": 5000, "bucket_end": 10000}])
        move_s, move_r = move_fut.result()
        [f.result() for f in futs]
    check("rearrangement commits during assignment storm", move_s == 200, move_r)
    check("no assignment outside old/new layout", not violations, violations[:5])

    # ---------- 6. concurrency: two overlapping batches, one must fail ------
    layer6 = make_layer("race-batch")
    l6 = layer6["id"]
    p = publish(make_experiment(l6, "P", 0, 3000)["id"])
    q = publish(make_experiment(l6, "Q", 3000, 6000)["id"])

    def batch(q_target):
        return rearrange(l6, [
            {"experiment_id": p["id"], "expected_version": 1,
             "bucket_start": 6000, "bucket_end": 9000},
            {"experiment_id": q["id"], "expected_version": 1,
             "bucket_start": q_target[0], "bucket_end": q_target[1]},
        ])

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(batch, [(0, 3000), (7000, 9000)]))
    codes = sorted(s for s, _ in results)
    check("concurrent conflicting batches: one 200 one 409", codes == [200, 409], results)

    # resulting layout still non-overlapping
    s, exps = req("GET", f"/layers/{l6}/experiments")
    active = sorted((e["bucket_start"], e["bucket_end"]) for e in exps if e["status"] == "active")
    ok = all(active[i][1] <= active[i + 1][0] for i in range(len(active) - 1))
    check("post-race active ranges non-overlapping", ok, active)

    # winner's version token is now current; retrying with old version fails,
    # retrying with the fresh version (same ranges) succeeds without bump.
    win = [r for s, r in results if s == 200][0]
    pv = by_id(win, p["id"])["version"]
    qv = by_id(win, q["id"])["version"]
    s, r = rearrange(l6, [
        {"experiment_id": p["id"], "expected_version": 1,
         "bucket_start": 6000, "bucket_end": 9000}])
    check("retry with stale version after race -> 409", s == 409, r)
    s, r = rearrange(l6, [
        {"experiment_id": p["id"], "expected_version": pv,
         "bucket_start": 6000, "bucket_end": 9000},
        {"experiment_id": q["id"], "expected_version": qv,
         "bucket_start": by_id(win, q["id"])["bucket_start"],
         "bucket_end": by_id(win, q["id"])["bucket_end"]}])
    check("retry with current versions -> 200, versions unchanged",
          s == 200 and by_id(r, p["id"])["version"] == pv
          and by_id(r, q["id"])["version"] == qv, r)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
