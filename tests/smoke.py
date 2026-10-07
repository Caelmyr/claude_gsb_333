"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import models, report, storage, versions  # noqa: E402
from backend.engine import make_engine  # noqa: E402
from backend.run_manager import manager  # noqa: E402

_ENGINES = ["traffic/ca", "traffic/abm", "ecology/ca", "ecology/abm",
            "epidemic/ca", "epidemic/abm"]


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)


def engines_step() -> None:
    for key in _ENGINES:
        d, m = key.split("/")
        eng = make_engine(d, m, seed=42)
        for _ in range(5):
            eng.step()
        assert eng.step_count == 5, key
        assert len(eng.individuals()) > 0, f"{key} has no individuals"
        stats = eng.stats()
        assert stats, f"{key} produced empty stats"
        snap = eng.snapshot()
        assert snap["step"] == 5
        assert snap["stats"] == stats


def interventions_apply() -> None:
    eng = make_engine("epidemic", "abm", seed=1)
    before = eng.stats()["susceptible"]
    res = eng.apply_intervention({"type": "vaccinate", "params": {"fraction": 1.0}})
    assert res["applied"], res
    assert eng.stats()["susceptible"] == 0
    assert before > 0


def storage_atomic_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        # Redirect the module's DATA_DIR for this isolated check.
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene({"id": "x", "name": "t", "updated_at": "z"})
            assert storage.load_scene("x")["name"] == "t"
            storage.save_step("r1", 0, {"step": 0, "v": 1})
            storage.save_step("r1", 7, {"step": 7, "v": 2})
            assert storage.load_step("r1", 7)["v"] == 2
            assert storage.list_steps("r1") == [0, 7]
        finally:
            storage.DATA_DIR = old


def run_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 200, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        r = manager.step(rid, 6)
        assert r["step"] == 6
        series = manager.get_series(rid)
        assert series[0]["step"] == 0 and series[-1]["step"] == 6
        assert len(manager.get_individuals(rid, 6)) == 200
        # snapshot_interval=2 -> full snapshots persisted at 0,2,4,6
        steps = storage.list_steps(rid)
        assert steps == [0, 2, 4, 6], steps
        rpt = report.generate_report(rid)
        assert rpt["steps"] == 7
    finally:
        manager.delete_run(rid)


def scene_versioning() -> None:
    with tempfile.TemporaryDirectory() as td:
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            sid = "scene_ver"
            scene = {"id": sid, "name": "版本场景", "domain": "epidemic",
                     "model": "abm", "description": "",
                     "config": {"n": 800, "beta": 0.3, "width": 400,
                                "initial_infected": 10, "vaccination_rate": 0.0},
                     "interventions": [
                         {"type": "vaccinate", "at_step": 30,
                          "params": {"fraction": 0.5}}]}
            storage.save_scene(scene)
            v1, c1 = versions.record_version(sid, scene, kind="create")
            assert c1

            # Saving identical content never creates a version.
            _, c_dup = versions.record_version(sid, scene, kind="auto")
            assert not c_dup

            # A normal edit appends a new version.
            s2 = {**scene,
                  "config": {**scene["config"], "beta": 0.12, "width": 500,
                             "initial_infected": 20},
                  "interventions": [
                      {"type": "vaccinate", "at_step": 50,
                       "params": {"fraction": 0.7}},
                      {"type": "lockdown", "at_step": 80,
                       "params": {"scale": 0.5}}]}
            storage.save_scene(s2)
            v2, _ = versions.record_version(sid, s2, kind="auto")

            # Structured diff covers params / map / initial / interventions.
            d = versions.diff_versions(sid, v1["id"], v2["id"])
            counts = d["summary"]
            assert counts["map"] >= 1 and counts["initial"] >= 1
            assert counts["params"] >= 1 and counts["interventions"] >= 1

            # Rollback appends a version (never erases history), shares the
            # target blob, and restores the live scene.
            restored, vr, created = versions.rollback(sid, v1["id"])
            assert created and vr["blob"] == v1["blob"]
            assert vr["parent_id"] == v2["id"]
            assert restored["config"]["beta"] == 0.3
            assert restored["interventions"][0]["at_step"] == 30

            # Editing after rollback extends the chain from the rollback node.
            s4 = {**restored, "config": {**restored["config"], "beta": 0.9}}
            storage.save_scene(s4)
            v4, _ = versions.record_version(sid, s4, kind="auto")
            assert v4["parent_id"] == vr["id"]

            # Clone is independent and keeps provenance; source is untouched.
            clone = versions.clone_scene(sid, version_id=v1["id"])
            assert clone["id"] != sid
            assert clone["config"]["beta"] == 0.3
            cidx = storage.load_version_index(clone["id"])
            assert cidx["versions"][0]["kind"] == "clone"
            assert cidx["versions"][0]["source"]["version_id"] == v1["id"]
            assert storage.load_scene(sid) is not None

            # Auto-pruning bounds storage while protecting key events and
            # preserving a connected chain and the live head content.
            for i in range(versions.AUTO_MAX + 10):
                cur = {**storage.load_scene(sid),
                       "config": {"beta": round(0.001 * i, 4)}}
                storage.save_scene(cur)
                versions.record_version(sid, cur, kind="auto")
            idx = storage.load_version_index(sid)
            by_id = {r["id"]: r for r in idx["versions"]}
            assert v1["id"] in by_id and vr["id"] in by_id  # protected
            node = by_id[idx["head"]]
            depth = 0
            while node:
                depth += 1
                node = by_id.get(node["parent_id"])
            assert depth == len(idx["versions"])
            head_blob = storage.load_version_blob(by_id[idx["head"]]["blob"])
            assert head_blob["config"] == storage.load_scene(sid)["config"]
        finally:
            storage.DATA_DIR = old


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("scene versioning: snapshot/diff/rollback/clone/prune",
          scene_versioning)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
