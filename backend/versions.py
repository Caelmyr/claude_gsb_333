"""Scene version management: snapshots, clones, diffs and rollback.

Every save of a scene is recorded as an immutable *version* in a per-scene
linear chain.  The design follows three rules that together make the history
fully controllable and impossible to confuse:

1. **Append-only.**  Nothing ever rewrites or deletes a recorded state
   (except explicit pruning).  Rolling back to an old version does *not* erase
   the versions that came after it — it appends a new "rollback" version whose
   content is the old snapshot and whose ``parent_id`` still points at the
   previous (newer) head.  Editing and saving afterwards simply extends the
   chain again, so new work can never be lost and the full evolution stays
   readable in order.

2. **Content-addressed storage.**  Version payloads live in a shared blob
   store keyed by a SHA-256 of their canonical JSON; the per-scene index holds
   metadata only.  Saving identical content twice, cloning and rolling back
   never duplicate bytes — 100 versions that differ in one parameter cost
   ~100 tiny JSON files.  Old auto-snapshots are pruned automatically (named
   checkpoints, create/clone/rollback markers and the head are always kept),
   and unreferenced blobs are garbage-collected.

3. **Diff before you act.**  Any two versions can be compared; the result is
   structured into 基本信息 / 地图 / 初始布局 / 参数 / 定时干预 sections so the
   user can see exactly which parameter, intervention, map or initial-layout
   choice changed.

On-disk layout (managed together with :mod:`backend.storage`)::

    data/scene_versions/<scene_id>.json     # {head, seq, versions: [meta...]}
    data/scene_versions/blobs/<sha>.json    # distinct scene payloads (shared)
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import catalog, storage, util

# Auto-snapshot retention: when a scene accumulates more than AUTO_MAX plain
# auto versions the chain is compacted to AUTO_KEEP newest ones.  Protected
# versions (create/clone/backfill/checkpoint, head, rollback targets) never
# count against the limit and are never removed.
AUTO_MAX = 100
AUTO_KEEP = 50

# Config-key classification for structured diffs.
MAP_KEYS = {"width", "height", "lanes", "length", "road_length"}
INITIAL_KEYS = {"density", "n", "n_rabbits", "n_foxes", "n_boids",
                "n_predators", "initial_infected", "vaccination_rate"}

#: Kinds that must survive automatic pruning.
PROTECTED_KINDS = {"create", "clone", "backfill", "checkpoint", "rollback"}

_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.RLock] = {}


def _lock_for(scene_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(scene_id)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[scene_id] = lk
        return lk


# --------------------------------------------------------------------------- #
# Payloads / hashing
# --------------------------------------------------------------------------- #
def payload_of(scene: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the versioned (editable) content of a scene dict."""
    return {
        "name": str(scene.get("name", "未命名场景")),
        "description": str(scene.get("description", "")),
        "domain": str(scene.get("domain", "")),
        "model": str(scene.get("model", "")),
        "config": dict(scene.get("config") or {}),
        "interventions": [dict(i) for i in (scene.get("interventions") or [])],
    }


def hash_payload(payload: Dict[str, Any]) -> str:
    """Stable SHA-256 of a payload (key order / formatting independent)."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Index helpers
# --------------------------------------------------------------------------- #
def _find(index: Dict[str, Any], version_id: str) -> Optional[Dict[str, Any]]:
    for rec in index["versions"]:
        if rec["id"] == version_id:
            return rec
    return None


def _head(index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _find(index, index.get("head") or "")


def _public_record(rec: Dict[str, Any],
                   index: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(rec)
    out["is_head"] = rec["id"] == index.get("head")
    out["protected"] = rec.get("kind") in PROTECTED_KINDS
    out["short_id"] = rec["id"][-6:]
    return out


def ensure_backfill(scene_id: str,
                    scene: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Create a baseline version for a scene that predates versioning.

    Scenes seeded or created before this feature have no index; the first time
    their history is touched we snapshot whatever is currently on disk as a
    protected ``backfill`` version, so an old scene is never left without a
    recoverable baseline.
    """
    with _lock_for(scene_id):
        index = storage.load_version_index(scene_id)
        if index["versions"]:
            return index["versions"][0], False
        rec, _ = record_version(
            scene_id, scene, kind="backfill",
            name="历史版本（自动补录）")
        return rec, True


def record_version(scene_id: str, scene: Dict[str, Any], kind: str = "auto",
                   name: Optional[str] = None,
                   source: Optional[Dict[str, Any]] = None,
                   force: bool = False,
                   payload: Optional[Dict[str, Any]] = None,
                   blob_id: Optional[str] = None
                   ) -> Tuple[Dict[str, Any], bool]:
    """Append a version for ``scene``'s current content.

    Returns ``(record, created)``.  When the current head already holds the
    exact same content the existing head version is returned with
    ``created=False`` (re-saving an unchanged scene never pollutes history);
    pass ``force=True`` for explicit markers (named checkpoints) that should be
    recorded even if identical.

    ``payload``/``blob_id`` override the content hashed from ``scene`` — used
    by rollback/clone so the new record shares the exact source snapshot blob
    even though the live scene's name (clone) differs.
    """
    if payload is None:
        payload = payload_of(scene)
    if blob_id is None:
        blob_id = hash_payload(payload)
    storage.save_version_blob(blob_id, payload)

    with _lock_for(scene_id):
        index = storage.load_version_index(scene_id)
        head = _head(index)
        if head is not None and head.get("blob") == blob_id and not force:
            return head, False

        seq = int(index.get("seq", 0)) + 1
        rec = {
            "id": util.new_id("ver"),
            "seq": seq,
            "kind": kind,
            "name": name or "",
            "created_at": util.now_iso(),
            "parent_id": head["id"] if head else None,
            "blob": blob_id,
            "source": source or {},
        }
        index["versions"].append(rec)
        index["head"] = rec["id"]
        index["seq"] = seq
        storage.save_version_index(scene_id, index)

        if kind == "auto":
            autos = [r for r in index["versions"] if r.get("kind") == "auto"]
            if len(autos) > AUTO_MAX:
                # Re-read the compacted index so stats stay consistent.
                prune_versions(scene_id, AUTO_KEEP)
        return rec, True


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_versions(scene_id: str, limit: int = 30,
                  offset: int = 0) -> Dict[str, Any]:
    """Versions newest-first, paged, each annotated with a change summary."""
    index = storage.load_version_index(scene_id)
    total = len(index["versions"])
    rows_desc = list(reversed(index["versions"]))
    page = rows_desc[offset:offset + limit] if limit else rows_desc[offset:]

    by_id = {r["id"]: r for r in index["versions"]}
    out = []
    for rec in page:
        row = _public_record(rec, index)
        parent = by_id.get(rec.get("parent_id") or "")
        if parent is not None:
            d = diff_payloads(
                storage.load_version_blob(parent["blob"]),
                storage.load_version_blob(rec["blob"]))
            row["changes"] = d["summary"]
        else:
            row["changes"] = None
        out.append(row)
    return {"head": index.get("head"), "total": total,
            "limit": limit, "offset": offset, "versions": out}


def get_version(scene_id: str, version_id: str) -> Dict[str, Any]:
    index = storage.load_version_index(scene_id)
    rec = _find(index, version_id)
    if rec is None:
        raise KeyError(f"version not found: {version_id}")
    payload = storage.load_version_blob(rec["blob"])
    if payload is None:
        raise KeyError(f"version snapshot missing on disk: {version_id}")
    return {"version": _public_record(rec, index), "scene": payload}


def rename_version(scene_id: str, version_id: str, name: str) -> Dict[str, Any]:
    """Attach/edit a human note on a version (e.g. pin a milestone)."""
    with _lock_for(scene_id):
        index = storage.load_version_index(scene_id)
        rec = _find(index, version_id)
        if rec is None:
            raise KeyError(f"version not found: {version_id}")
        rec["name"] = (name or "").strip()[:100]
        storage.save_version_index(scene_id, index)
        return _public_record(rec, index)


# --------------------------------------------------------------------------- #
# Rollback (append-only — see module docstring rule 1)
# --------------------------------------------------------------------------- #
def rollback(scene_id: str, version_id: str,
             note: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Restore a historical version, appending a new rollback version.

    Returns ``(new_scene, rollback_record, created)``.  If the requested
    version is already the current content (``created=False``) nothing is
    written.
    """
    with _lock_for(scene_id):
        index = storage.load_version_index(scene_id)
        target = _find(index, version_id)
        if target is None:
            raise KeyError(f"version not found: {version_id}")
        payload = storage.load_version_blob(target["blob"])
        if payload is None:
            raise KeyError(f"version snapshot missing on disk: {version_id}")

        old_scene = storage.load_scene(scene_id) or {}
        now = util.now_iso()
        new_scene = {
            "id": scene_id,
            **payload,
            "created_at": old_scene.get("created_at") or now,
            "updated_at": now,
        }
        storage.save_scene(new_scene)
        # Share the target's blob exactly: the restored scene content *is* the
        # historical snapshot, so dedup and storage reuse stay consistent.
        rec, created = record_version(
            scene_id, new_scene, kind="rollback",
            name=note or f"回滚到 v{target['seq']}",
            source={"rollback_from": target["id"],
                    "rollback_seq": target["seq"]},
            payload=payload, blob_id=target["blob"])
        return new_scene, rec, created


# --------------------------------------------------------------------------- #
# Clone (one-click duplicate as an independent scene with its own chain)
# --------------------------------------------------------------------------- #
def clone_scene(scene_id: str, new_name: Optional[str] = None,
                version_id: Optional[str] = None) -> Dict[str, Any]:
    """Clone a scene (or one of its versions) into a brand-new independent scene.

    The clone starts its own version chain at a single ``clone`` version that
    records the source scene + source version as provenance; the source scene
    and its history are left untouched.
    """
    with _lock_for(scene_id):
        source_scene = storage.load_scene(scene_id)
        if source_scene is None:
            raise KeyError(f"scene not found: {scene_id}")
        index = storage.load_version_index(scene_id)
        target = _find(index, version_id) if version_id else _head(index)
        if version_id and target is None:
            raise KeyError(f"version not found: {version_id}")
        if target is not None:
            payload = storage.load_version_blob(target["blob"])
            if payload is None:
                raise KeyError("source version snapshot missing on disk")
        else:
            payload = payload_of(source_scene)

    new_id = util.new_id("scene")
    now = util.now_iso()
    clone = {
        "id": new_id,
        **payload,
        "name": new_name or f"{payload['name']}（副本）",
        "created_at": now,
        "updated_at": now,
    }
    storage.save_scene(clone)
    record_version(
        new_id, clone, kind="clone",
        name=f"克隆自《{source_scene.get('name', scene_id)}》"
             + (f" v{target['seq']}" if target else ""),
        source={"scene_id": scene_id,
                "scene_name": source_scene.get("name", ""),
                "version_id": target["id"] if target else None,
                "version_seq": target["seq"] if target else None},
        payload=payload, blob_id=hash_payload(payload))
    return clone


# --------------------------------------------------------------------------- #
# Pruning + garbage collection (storage growth handling)
# --------------------------------------------------------------------------- #
def _protected_ids(index: Dict[str, Any]) -> set:
    """Version ids that pruning must never remove."""
    keep = set()
    for rec in index["versions"]:
        if rec.get("kind") in PROTECTED_KINDS:
            keep.add(rec["id"])
        src = rec.get("source") or {}
        if src.get("rollback_from"):
            keep.add(src["rollback_from"])
    if index.get("head"):
        keep.add(index["head"])
    return keep


def prune_versions(scene_id: str, keep: int = AUTO_KEEP) -> Dict[str, Any]:
    """Compact a scene's chain to its newest ``keep`` removable versions.

    Protected versions (see :data:`PROTECTED_KINDS`, the head and every
    rollback target) are always retained on top of ``keep``.  After removal
    the surviving records' ``parent_id`` links are re-wired to the nearest
    surviving ancestor, so the chain never dangles; sequence numbers of
    surviving versions are preserved (they are permanent labels) and the next
    save simply continues from ``max(seq) + 1``.  Unreferenced content blobs
    are then garbage-collected across all scenes.
    """
    keep = max(1, int(keep))
    with _lock_for(scene_id):
        index = storage.load_version_index(scene_id)
        records = index["versions"]
        if not records:
            return {"removed": 0, "deleted_blobs": 0,
                    "kept": 0, "protected": 0}

        protected = _protected_ids(index)
        # Only plain auto-snapshots are eligible for automatic compaction;
        # every event kind in PROTECTED_KINDS is retained forever.
        removable = [r for r in records
                     if r.get("kind") == "auto" and r["id"] not in protected]
        # Newest `keep` removable records survive; older ones are dropped.
        drop = {r["id"] for r in removable[:-keep]} if len(removable) > keep else set()
        if not drop:
            deleted_blobs = gc_blobs()
            return {"removed": 0, "deleted_blobs": deleted_blobs,
                    "kept": len(records), "protected": len(protected)}

        parent_of = {r["id"]: r.get("parent_id") for r in records}
        dropped_blobs = {r.get("blob") for r in records if r["id"] in drop}
        survivors = [r for r in records if r["id"] not in drop]
        survivor_ids = {r["id"] for r in survivors}
        for rec in survivors:
            parent = rec.get("parent_id")
            while parent and parent not in survivor_ids:
                parent = parent_of.get(parent)
            rec["parent_id"] = parent
        index["versions"] = survivors
        # Head is protected, but stay defensive.
        if index.get("head") not in survivor_ids:
            index["head"] = survivors[-1]["id"]
        storage.save_version_index(scene_id, index)

        # The new index is now atomically committed, so blobs the drop just
        # orphaned are safe to reclaim immediately even inside the grace window
        # (which exists only to protect blobs whose index is mid-write).
        dropped_blobs.discard(None)
        deleted_blobs = gc_blobs(force=dropped_blobs)
        return {"removed": len(drop), "deleted_blobs": deleted_blobs,
                "kept": len(survivors), "protected": len(protected)}


def gc_blobs(grace_seconds: int = 30,
             force: Optional[set] = None) -> int:
    """Delete content blobs not referenced by any scene version index.

    Blobs younger than ``grace_seconds`` are spared so a blob written just
    before its (atomic) index update can never be collected mid-save.  Blobs in
    ``force`` are collected regardless of age: callers pass the exact blob set
    orphaned by an already-committed prune, which is provably safe.
    """
    force = force or set()
    referenced: set = set()
    for sid in storage.list_version_indexes():
        index = storage.load_version_index(sid)
        for rec in index.get("versions", []):
            if rec.get("blob"):
                referenced.add(rec["blob"])

    removed = 0
    cutoff = time.time() - grace_seconds
    for hash_id in storage.list_version_blobs():
        if hash_id in referenced:
            continue
        path = storage.version_blob_path(hash_id)
        try:
            if hash_id not in force and os.path.getmtime(path) > cutoff:
                continue
            storage.delete_version_blob(hash_id)
            removed += 1
        except OSError:
            continue
    return removed


def version_stats() -> Dict[str, Any]:
    """Storage overview: per-scene version counts/bytes plus blob totals."""
    blob_sizes: Dict[str, int] = {}
    for hash_id in storage.list_version_blobs():
        try:
            blob_sizes[hash_id] = os.path.getsize(
                storage.version_blob_path(hash_id))
        except OSError:
            blob_sizes[hash_id] = 0

    per_scene: List[Dict[str, Any]] = []
    scenes = {s["id"]: s for s in storage.list_scenes()}
    for sid in storage.list_version_indexes():
        index = storage.load_version_index(sid)
        recs = index.get("versions", [])
        bytes_used = sum(blob_sizes.get(r.get("blob"), 0) for r in recs)
        protected = len([r for r in recs if r.get("kind") in PROTECTED_KINDS])
        per_scene.append({
            "scene_id": sid,
            "name": scenes.get(sid, {}).get("name", "(已删除场景)"),
            "exists": sid in scenes,
            "versions": len(recs),
            "auto": len([r for r in recs if r.get("kind") == "auto"]),
            "protected": protected,
            "bytes": bytes_used,
        })
    per_scene.sort(key=lambda x: x["bytes"], reverse=True)
    return {
        "scenes": per_scene,
        "total_versions": sum(s["versions"] for s in per_scene),
        "total_blobs": len(blob_sizes),
        "total_bytes": sum(blob_sizes.values()),
        "auto_keep": AUTO_KEEP,
        "auto_max": AUTO_MAX,
    }


# --------------------------------------------------------------------------- #
# Structured diffs (基本信息 / 地图 / 初始布局 / 参数 / 定时干预)
# --------------------------------------------------------------------------- #
def _param_specs(domain: str, model: str) -> Dict[str, Dict[str, Any]]:
    try:
        return {p["key"]: p for p in catalog.model_params(domain, model)}
    except (KeyError, TypeError):
        return {}


def _itv_specs(domain: str) -> Dict[str, Dict[str, Any]]:
    try:
        return {i["type"]: i for i in catalog.interventions(domain)}
    except (KeyError, TypeError):
        return {}


def _config_section(key: str) -> str:
    if key in MAP_KEYS:
        return "map"
    if key in INITIAL_KEYS or "initial" in key:
        return "initial"
    return "params"


SECTION_LABELS = {
    "meta": "基本信息",
    "map": "地图",
    "initial": "初始布局",
    "params": "参数",
    "interventions": "定时干预",
}


def _scalar_change(key: str, old: Any, new: Any) -> Optional[Dict[str, Any]]:
    if old == new:
        return None
    if old is None:
        kind, old_v, new_v = "added", None, new
    elif new is None:
        kind, old_v, new_v = "removed", old, None
    else:
        kind, old_v, new_v = "changed", old, new
    return {"key": key, "kind": kind, "old": old_v, "new": new_v}


def _diff_config(a: Dict[str, Any], b: Dict[str, Any],
                 domain_a: str, model_a: str,
                 domain_b: str, model_b: str) -> Dict[str, List[Dict[str, Any]]]:
    specs_a = _param_specs(domain_a, model_a)
    specs_b = _param_specs(domain_b, model_b)
    sections: Dict[str, List[Dict[str, Any]]] = {
        "map": [], "initial": [], "params": []}
    for key in sorted(set(a) | set(b)):
        chg = _scalar_change(key, a.get(key), b.get(key))
        if chg is None:
            continue
        spec = specs_b.get(key) or specs_a.get(key) or {}
        chg["label"] = spec.get("label", key)
        sections[_config_section(key)].append(chg)
    return sections


def _itv_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (a.get("type") == b.get("type")
            and (a.get("at_step") or 0) == (b.get("at_step") or 0)
            and (a.get("params") or {}) == (b.get("params") or {}))


def _itv_view(itv: Dict[str, Any], specs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    spec = specs.get(itv.get("type", ""), {})
    param_labels = {p["key"]: p.get("label", p["key"])
                    for p in spec.get("params", [])}
    return {
        "type": itv.get("type", ""),
        "label": spec.get("label", itv.get("type", "")),
        "at_step": itv.get("at_step") or 0,
        "note": itv.get("label", ""),
        "params": itv.get("params") or {},
        "param_labels": param_labels,
    }


def _diff_interventions(a: List[Dict[str, Any]], b: List[Dict[str, Any]],
                        domain_a: str, domain_b: str) -> List[Dict[str, Any]]:
    """Align two intervention lists with a classic LCS, then describe edits.

    A plain dynamic-programming LCS is used instead of difflib: difflib's
    "longest contiguous junk-free block" heuristic produces unintuitive
    alignments for reordered entries (e.g. one element moved to the front).
    LCS minimises the edit script and pairs surviving entries by position.
    """
    specs_a = _itv_specs(domain_a)
    specs_b = _itv_specs(domain_b)
    va = [_itv_view(item, specs_a) for item in a]
    vb = [_itv_view(item, specs_b) for item in b]
    na, nb = len(va), len(vb)

    # LCS length table.
    lcs = [[0] * (nb + 1) for _ in range(na + 1)]
    for x in range(1, na + 1):
        for y in range(1, nb + 1):
            if _itv_equal(a[x - 1], b[y - 1]):
                lcs[x][y] = lcs[x - 1][y - 1] + 1
            else:
                lcs[x][y] = max(lcs[x - 1][y], lcs[x][y - 1])

    # Canonical backtrack into paired/inserted/deleted indices.  A pair is
    # taken only when the diagonal genuinely accounts for the current LCS
    # length; a plain equal-but-off-diagonal entry must not steal the match.
    paired: List[Tuple[int, int]] = []
    x, y = na, nb
    while x > 0 and y > 0:
        if _itv_equal(a[x - 1], b[y - 1]) \
                and lcs[x][y] == lcs[x - 1][y - 1] + 1:
            paired.append((x - 1, y - 1))
            x -= 1
            y -= 1
        elif lcs[x - 1][y] == lcs[x][y]:
            x -= 1            # LCS survives without b entry: consume a
        else:
            y -= 1            # LCS survives without a entry: consume b
    paired.reverse()

    # Non-paired entries become an in-place replacement only when their
    # intervention *type* matches (a step/param edit).  Type changes or moved
    # entries stay as explicit remove + add, so a reorder is never described
    # misleadingly as "疫苗接种 变成 特效药".
    paired_old = {oi for oi, _ in paired}
    paired_new = {nj for _, nj in paired}
    free_old = [oi for oi in range(na) if oi not in paired_old]
    free_new = [nj for nj in range(nb) if nj not in paired_new]

    def replaced_entry(oi: int, nj: int) -> Dict[str, Any]:
        old, new = va[oi], vb[nj]
        param_changes = []
        for pk in sorted(set(old["params"]) | set(new["params"])):
            pc = _scalar_change(pk, old["params"].get(pk),
                                new["params"].get(pk))
            if pc:
                pc["label"] = new["param_labels"].get(pk) or \
                    old["param_labels"].get(pk) or pk
                param_changes.append(pc)
        meta_changes = []
        if old["at_step"] != new["at_step"]:
            meta_changes.append({"key": "at_step", "label": "触发步",
                                 "kind": "changed",
                                 "old": old["at_step"], "new": new["at_step"]})
        if old["type"] != new["type"]:
            meta_changes.append({"key": "type", "label": "干预类型",
                                 "kind": "changed",
                                 "old": old["label"] or old["type"],
                                 "new": new["label"] or new["type"]})
        return {"kind": "replaced", "index": oi, "old": old, "new": new,
                "param_changes": param_changes, "meta_changes": meta_changes}

    changes: List[Dict[str, Any]] = []
    used_new: set = set()
    for oi in free_old:
        # Only a same-index, same-type free pair reads as an in-place edit.
        # Same-type entries elsewhere were left unmatched by LCS because of a
        # reorder, not an edit, so they stay remove+add.  When a same-index
        # pair is byte-identical it merely shifted anchors — report nothing.
        nj = oi
        if nj < nb and nj not in paired_new and nj not in used_new \
                and a[oi].get("type") == b[nj].get("type"):
            used_new.add(nj)
            if not _itv_equal(a[oi], b[nj]):
                changes.append(replaced_entry(oi, nj))
        else:
            changes.append({"kind": "removed", "index": oi, "old": va[oi]})
    for nj in free_new:
        if nj not in used_new:
            changes.append({"kind": "added", "index": nj, "new": vb[nj]})
    changes.sort(key=lambda c: c["index"])
    return changes


def diff_payloads(a: Optional[Dict[str, Any]],
                  b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Structured diff between two version payloads (a -> b)."""
    a = a or {"name": "", "description": "", "domain": "", "model": "",
              "config": {}, "interventions": []}
    b = b or {"name": "", "description": "", "domain": "", "model": "",
              "config": {}, "interventions": []}

    meta: List[Dict[str, Any]] = []
    for key, label in (("name", "名称"), ("description", "描述"),
                       ("domain", "领域"), ("model", "模型")):
        chg = _scalar_change(key, a.get(key), b.get(key))
        if chg:
            chg["label"] = label
            meta.append(chg)

    cfg_sections = _diff_config(a.get("config") or {}, b.get("config") or {},
                                a.get("domain", ""), a.get("model", ""),
                                b.get("domain", ""), b.get("model", ""))
    itv_changes = _diff_interventions(a.get("interventions") or [],
                                      b.get("interventions") or [],
                                      a.get("domain", ""), b.get("domain", ""))

    sections = [
        {"key": "meta", "label": SECTION_LABELS["meta"], "changes": meta},
        {"key": "map", "label": SECTION_LABELS["map"],
         "changes": cfg_sections["map"]},
        {"key": "initial", "label": SECTION_LABELS["initial"],
         "changes": cfg_sections["initial"]},
        {"key": "params", "label": SECTION_LABELS["params"],
         "changes": cfg_sections["params"]},
        {"key": "interventions", "label": SECTION_LABELS["interventions"],
         "changes": itv_changes},
    ]
    summary = {s["key"]: len(s["changes"]) for s in sections}
    summary["total"] = sum(summary.values())
    return {"sections": sections, "summary": summary}


def diff_versions(scene_id: str, version_a: Optional[str],
                  version_b: str) -> Dict[str, Any]:
    """Diff two stored versions.  ``version_a`` defaults to b's parent."""
    index = storage.load_version_index(scene_id)
    rec_b = _find(index, version_b)
    if rec_b is None:
        raise KeyError(f"version not found: {version_b}")
    if version_a:
        rec_a = _find(index, version_a)
        if rec_a is None:
            raise KeyError(f"version not found: {version_a}")
    else:
        rec_a = _find(index, rec_b.get("parent_id") or "")
    payload_a = storage.load_version_blob(rec_a["blob"]) if rec_a else None
    payload_b = storage.load_version_blob(rec_b["blob"])
    if payload_b is None:
        raise KeyError("version snapshot missing on disk")

    result = diff_payloads(payload_a, payload_b)
    result["a"] = _public_record(rec_a, index) if rec_a else None
    result["b"] = _public_record(rec_b, index)
    return result
