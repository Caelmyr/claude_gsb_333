"""Scene version management: snapshots, diffs, rollback and cloning.

Every mutating save of a scene funnels through :func:`record`, which appends an
immutable snapshot of the scene *content* (name / domain / model / description
/ config / interventions) to that scene's version chain.  The chain is
**append-only and linear**:

* each version has a monotonically increasing number ``n`` and a ``parent``
  pointer to the previous version;
* a rollback never rewrites or deletes history — it *appends* a new version
  whose content is copied from the target version (``source="rollback"``,
  ``restored_from=k``).  The pre-rollback state therefore stays on the chain
  and can itself be restored later, so "roll back, then edit, then save" just
  continues the same linear chain with no lost work and no branching
  ambiguity;
* a clone starts a *new* scene with its own fresh chain whose first version
  records where it was cloned from.

Storage is bounded two ways:

* **dedup** — saving content identical to the current head (by SHA-1 of the
  canonical JSON) records no new version;
* **retention** — beyond :data:`MAX_VERSIONS` snapshots the oldest snapshot
  files are pruned.  Pruned versions keep their index entry as a tombstone
  (``pruned: true``) so version numbers are never reused and the chain stays
  auditable; they simply can no longer be viewed, diffed or restored.

The diff engine (:func:`diff_content`) groups changes the way users think
about a scene: 基本信息 (name/description/domain/model), 地图与布局 and
初始布局 (both are config keys, classified via :data:`MAP_KEYS` /
:data:`INITIAL_KEYS`), 模型参数 (remaining config keys) and 干预措施
(added / removed / modified with param-level detail).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from . import catalog, storage, util

#: Maximum number of snapshots retained per scene before auto-pruning.
MAX_VERSIONS = 50

#: Versioned payload keys — everything that defines *what* the scene is.
CONTENT_KEYS = ("name", "domain", "model", "description", "config",
                "interventions")

#: Config keys that describe the map / spatial layout of the world.
MAP_KEYS = {"width", "height", "lanes", "length", "road_length"}

#: Config keys that describe the initial population / layout of individuals.
INITIAL_KEYS = {"n", "n_rabbits", "n_foxes", "n_boids", "n_predators",
                "density", "initial_infected", "vaccination_rate"}

SOURCE_LABEL = {"create": "创建", "edit": "编辑", "clone": "克隆",
                "rollback": "回滚"}


# --------------------------------------------------------------------------- #
# Content extraction & hashing
# --------------------------------------------------------------------------- #
def content_of(scene: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the versioned payload from a scene dict (canonical key order)."""
    return {k: scene.get(k) for k in CONTENT_KEYS}


def content_hash(content: Dict[str, Any]) -> str:
    """Stable SHA-1 over the canonical JSON of a versioned payload."""
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def summarize(content: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight per-version stats shown in the version list."""
    return {
        "config_keys": len(content.get("config") or {}),
        "interventions": len(content.get("interventions") or []),
    }


# --------------------------------------------------------------------------- #
# Index handling
# --------------------------------------------------------------------------- #
def _empty_index(scene_id: str) -> Dict[str, Any]:
    return {"scene_id": scene_id, "latest": 0, "versions": []}


def load_index(scene_id: str) -> Dict[str, Any]:
    return storage.load_version_index(scene_id) or _empty_index(scene_id)


def head_entry(index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    versions = index.get("versions") or []
    return versions[-1] if versions else None


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def record(scene: Dict[str, Any], source: str = "edit", message: str = "",
           restored_from: Optional[int] = None
           ) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Append a version for ``scene`` if its content differs from the head.

    Returns ``(entry, created)`` — ``created`` is False (and entry is the
    current head) when the save was a content no-op.
    """
    scene_id = scene["id"]
    content = content_of(scene)
    digest = content_hash(content)
    index = load_index(scene_id)
    head = head_entry(index)
    if head and head.get("hash") == digest:
        return head, False

    n = int(index.get("latest", 0)) + 1
    entry = {
        "n": n,
        "id": util.new_id("ver"),
        "created_at": util.now_iso(),
        "source": source,
        "message": message or SOURCE_LABEL.get(source, source),
        "parent": head["n"] if head else None,
        "restored_from": restored_from,
        "hash": digest,
        "summary": summarize(content),
        "pruned": False,
    }
    storage.save_version_snapshot(scene_id, n, content)
    index["latest"] = n
    index.setdefault("versions", []).append(entry)
    storage.save_version_index(scene_id, index)
    prune(scene_id, keep=MAX_VERSIONS)
    return entry, True


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def list_versions(scene_id: str, limit: Optional[int] = None,
                  offset: int = 0) -> Dict[str, Any]:
    """Version chain, newest first, with optional pagination."""
    index = load_index(scene_id)
    versions = list(reversed(index.get("versions") or []))
    total = len(versions)
    if offset:
        versions = versions[offset:]
    if limit is not None:
        versions = versions[:limit]
    return {"scene_id": scene_id, "latest": index.get("latest", 0),
            "total": total, "versions": versions}


def get_version(scene_id: str, n: int) -> Optional[Dict[str, Any]]:
    """A single version: its index entry plus the snapshot content."""
    index = load_index(scene_id)
    entry = next((v for v in index.get("versions", []) if v.get("n") == n),
                 None)
    if entry is None:
        return None
    content = storage.load_version_snapshot(scene_id, n)
    return {**entry, "content": content}


# --------------------------------------------------------------------------- #
# Rollback / clone
# --------------------------------------------------------------------------- #
def rollback(scene: Dict[str, Any], n: int) -> Dict[str, Any]:
    """Return a new scene dict whose content is restored from version ``n``.

    The caller persists the scene and records the new version; history is
    never rewritten.  Raises ``KeyError`` when the version is unknown and
    ``ValueError`` when its snapshot has been pruned.
    """
    version = get_version(scene["id"], n)
    if version is None:
        raise KeyError(f"version not found: v{n}")
    if version.get("content") is None:
        raise ValueError(f"版本 v{n} 的快照已被清理，无法回滚")
    restored = dict(scene)
    restored.update(version["content"])
    return restored


def clone_scene(scene: Dict[str, Any], new_name: Optional[str] = None
                ) -> Dict[str, Any]:
    """Build a brand-new scene dict copied from ``scene`` (fresh id & chain)."""
    now = util.now_iso()
    clone = content_of(scene)
    clone.update({
        "id": util.new_id("scene"),
        "name": new_name or f"{scene.get('name', '未命名场景')} · 副本",
        "created_at": now,
        "updated_at": now,
    })
    return clone


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #
def prune(scene_id: str, keep: int = MAX_VERSIONS) -> int:
    """Prune snapshot files beyond the newest ``keep`` versions.

    Index entries are kept as tombstones so version numbers stay stable and
    the chain remains auditable.  Returns the number of snapshots pruned.
    """
    index = load_index(scene_id)
    versions = index.get("versions") or []
    keep = max(1, int(keep))
    survivors = {v["n"] for v in versions[-keep:]}
    pruned = 0
    changed = False
    for entry in versions:
        if entry["n"] in survivors or entry.get("pruned"):
            continue
        if storage.delete_version_snapshot(scene_id, entry["n"]):
            pruned += 1
        entry["pruned"] = True
        changed = True
    if changed:
        storage.save_version_index(scene_id, index)
    return pruned


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def _param_labels(domain: str, model: str) -> Dict[str, str]:
    try:
        return {p["key"]: p["label"]
                for p in catalog.model_params(domain, model)}
    except KeyError:
        return {}


def _intervention_labels(domain: str) -> Dict[str, str]:
    try:
        return {i["type"]: i["label"] for i in catalog.interventions(domain)}
    except KeyError:
        return {}


def _diff_config(a: Dict[str, Any], b: Dict[str, Any],
                 labels: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    """Config diff grouped into 地图与布局 / 初始布局 / 模型参数."""
    groups: Dict[str, List[Dict[str, Any]]] = {"map": [], "initial": [],
                                               "params": []}
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va == vb:
            continue
        if key in MAP_KEYS:
            group = "map"
        elif key in INITIAL_KEYS:
            group = "initial"
        else:
            group = "params"
        change = "modified"
        if key not in a:
            change = "added"
        elif key not in b:
            change = "removed"
        groups[group].append({"key": key, "label": labels.get(key, key),
                              "from": va, "to": vb, "change": change})
    return groups


def _itv_key(itv: Dict[str, Any]) -> Tuple[str, int]:
    return (str(itv.get("type", "")), int(itv.get("at_step", 0) or 0))


def _diff_interventions(a: List[Dict[str, Any]], b: List[Dict[str, Any]],
                        labels: Dict[str, str]) -> Dict[str, List[Any]]:
    """Intervention diff: added / removed / modified (param-level detail).

    Interventions are matched by their ``(type, at_step)`` pair; matched pairs
    with differing params are reported as modified, unmatched ones as
    added/removed.
    """
    def describe(itv: Dict[str, Any]) -> Dict[str, Any]:
        t = str(itv.get("type", ""))
        return {"type": t, "label": labels.get(t, t),
                "at_step": int(itv.get("at_step", 0) or 0),
                "params": itv.get("params") or {}}

    unmatched_a = list(a)
    modified: List[Dict[str, Any]] = []
    added: List[Dict[str, Any]] = []
    for itv_b in b:
        match = next((x for x in unmatched_a
                      if _itv_key(x) == _itv_key(itv_b)), None)
        if match is None:
            added.append(describe(itv_b))
            continue
        unmatched_a.remove(match)
        if (match.get("params") or {}) != (itv_b.get("params") or {}):
            pa, pb = match.get("params") or {}, itv_b.get("params") or {}
            param_changes = [
                {"key": k, "from": pa.get(k), "to": pb.get(k)}
                for k in sorted(set(pa) | set(pb)) if pa.get(k) != pb.get(k)]
            modified.append({**describe(itv_b), "param_changes": param_changes})
    removed = [describe(x) for x in unmatched_a]
    return {"added": added, "removed": removed, "modified": modified}


def diff_content(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Structured diff between two versioned payloads."""
    domain = str(b.get("domain") or a.get("domain") or "")
    model = str(b.get("model") or a.get("model") or "")
    labels = _param_labels(domain, model)
    itv_labels = _intervention_labels(domain)

    meta = []
    for key, label in (("name", "名称"), ("description", "描述"),
                       ("domain", "领域"), ("model", "模型")):
        va, vb = a.get(key), b.get(key)
        if va != vb:
            meta.append({"key": key, "label": label, "from": va, "to": vb})

    config = _diff_config(a.get("config") or {}, b.get("config") or {}, labels)
    interventions = _diff_interventions(a.get("interventions") or [],
                                        b.get("interventions") or [],
                                        itv_labels)
    changed = bool(meta or any(config.values())
                   or any(interventions.values()))
    return {"meta": meta, "config": config, "interventions": interventions,
            "changed": changed}


def diff_versions(scene_id: str, from_n: int, to_n: int) -> Dict[str, Any]:
    """Diff two versions of a scene. Raises on unknown/pruned versions."""
    a = get_version(scene_id, from_n)
    b = get_version(scene_id, to_n)
    for ver, n in ((a, from_n), (b, to_n)):
        if ver is None:
            raise KeyError(f"version not found: v{n}")
        if ver.get("content") is None:
            raise ValueError(f"版本 v{n} 的快照已被清理，无法对比")
    result = diff_content(a["content"], b["content"])
    result["from"] = {"n": a["n"], "created_at": a["created_at"]}
    result["to"] = {"n": b["n"], "created_at": b["created_at"]}
    return result
