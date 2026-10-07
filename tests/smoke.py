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
            base = {"id": "s1", "name": "疫情基线", "domain": "epidemic",
                    "model": "ca", "description": "v1",
                    "config": {"width": 50, "height": 50, "beta": 0.4,
                               "initial_infected": 5},
                    "interventions": []}

            # create -> v1; identical save is a no-op (dedup)
            e1, created = versions.record(base, source="create")
            assert created and e1["n"] == 1 and e1["parent"] is None
            e1b, created = versions.record(base, source="edit")
            assert not created and e1b["n"] == 1
            assert versions.list_versions("s1")["total"] == 1

            # edit -> v2 with parent pointer; diff groups the changes
            v2 = dict(base, config={**base["config"], "width": 80, "beta": 0.5},
                      interventions=[{"type": "lockdown", "at_step": 10,
                                      "params": {"scale": 0.5}}])
            e2, created = versions.record(v2, source="edit")
            assert created and e2["n"] == 2 and e2["parent"] == 1
            d = versions.diff_versions("s1", 1, 2)
            assert d["changed"]
            assert [c["key"] for c in d["config"]["map"]] == ["width"]
            assert [c["key"] for c in d["config"]["params"]] == ["beta"]
            assert len(d["interventions"]["added"]) == 1

            # rollback to v1 -> new head v3 (history kept, chain continues)
            restored = versions.rollback(v2, 1)
            assert restored["config"] == base["config"]
            e3, created = versions.record(restored, source="rollback",
                                          restored_from=1)
            assert created and e3["n"] == 3 and e3["parent"] == 2
            assert e3["restored_from"] == 1
            # pre-rollback state (v2) is still intact and restorable
            assert versions.rollback(restored, 2)["config"]["beta"] == 0.5

            # editing after rollback simply extends the linear chain
            v4 = dict(restored, description="调整后")
            e4, _ = versions.record(v4, source="edit")
            assert e4["n"] == 4 and e4["parent"] == 3
            d = versions.diff_versions("s1", 3, 4)
            assert [m["key"] for m in d["meta"]] == ["description"]

            # clone -> fresh scene with its own chain starting at v1
            clone = versions.clone_scene(v4)
            assert clone["id"] != "s1" and clone["config"] == v4["config"]
            ec, created = versions.record(clone, source="clone")
            assert created and ec["n"] == 1 and ec["parent"] is None

            # pruning keeps tombstones + the newest snapshots
            for i in range(versions.MAX_VERSIONS + 5):
                versions.record(dict(v4, description=f"rev {i}"),
                                source="edit")
            info = versions.list_versions("s1")
            snaps = [v for v in info["versions"] if not v["pruned"]]
            assert len(snaps) == versions.MAX_VERSIONS
            assert info["latest"] == info["versions"][0]["n"]
            tombstones = [v for v in info["versions"] if v["pruned"]]
            assert tombstones and all(v["n"] for v in tombstones)
            try:
                versions.diff_versions("s1", tombstones[-1]["n"],
                                       info["latest"])
                raise AssertionError("expected pruned diff to fail")
            except ValueError:
                pass
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
