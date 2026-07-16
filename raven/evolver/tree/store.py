"""Evolver tree persistence orchestrator (C2.2 + C2.3 + C2.4).

Decouples HarnessNode metadata (JSON files in ``evolver/nodes/``)
from physical code state (Git commits inside the host repo). The
linkage is :attr:`HarnessNode.git_commit_sha`.

This module is the **only** place that should:

- write / read node JSON metadata
- atomically tie a new git commit to a new HarnessNode

Spec reference: §12.3 storage layout, §12.2 node schema, §22.7
path_guard integration.

Storage layout (spec §12.3):

.. code-block::

    raven repo (Git)                   ← code state, white-listed via Git
        ├─ commits referenced by HarnessNode.git_commit_sha
        └─ optional refs (created by EvolverTreeStore.create_child_node
                          when caller passes a branch_name)

    <evolver_dir>/                     ← typically ``evolver/`` under repo
        ├─ nodes/
        │   ├─ root-<sha8>.json
        │   ├─ v1-<sha8>.json
        │   └─ ...
        └─ (other files like trajectories/, tree.json — out of scope here)

The store does **not** own trajectory files or the WHERE×WHY archive —
those are separate concerns landing in later modules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from raven.evolver.applier import assert_patch_allowed
from raven.evolver.tree import git_ops
from raven.evolver.tree.node import (
    AppliedPatch,
    HarnessNode,
    NodeStatus,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-only topology snapshot
# ---------------------------------------------------------------------------


@dataclass
class TreeView:
    """A read-only snapshot of the evolver tree topology.

    Built by :meth:`EvolverTreeStore.build_tree`. Subsequent saves do
    not update this view — call ``build_tree`` again to refresh.

    Invariants (enforced at construction time by ``build_tree``):

    - Exactly one root (``parent_id is None``) when ``root_id`` is set.
      If no nodes have ``parent_id=None``, ``root_id`` is None and
      ``orphans`` lists the dangling node IDs.
    - ``children_of[parent_id]`` lists every node whose ``parent_id``
      equals that parent.
    - Every key in ``children_of`` is a known node id, plus possibly
      ``None`` for the "children-of-root-set" entry (kept under the
      root's own id, not ``None``).
    """

    nodes: dict[str, HarnessNode]
    children_of: dict[str, list[str]]   # parent_id → child node_ids
    root_id: Optional[str]
    orphans: list[str] = field(default_factory=list)

    # ---- traversal helpers ---------------------------------------------------

    def descendants(self, node_id: str) -> list[str]:
        """Return all node ids in the subtree rooted at ``node_id``
        (depth-first, ``node_id`` itself not included)."""
        out: list[str] = []
        stack = list(self.children_of.get(node_id, []))
        while stack:
            nid = stack.pop()
            out.append(nid)
            stack.extend(self.children_of.get(nid, []))
        return out

    def ancestry(self, node_id: str) -> list[str]:
        """Return the chain of ancestor ids from ``node_id`` (exclusive)
        up to the root (inclusive), in walking order."""
        out: list[str] = []
        cur = self.nodes.get(node_id)
        if cur is None:
            return out
        while cur.parent_id is not None:
            out.append(cur.parent_id)
            cur = self.nodes.get(cur.parent_id)
            if cur is None:
                # parent_id points at an unknown node (dangling)
                break
        return out

    def __len__(self) -> int:
        return len(self.nodes)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class EvolverTreeStore:
    """File-system + Git-backed persistence for the evolver tree.

    Typical lifecycle:

    .. code-block:: python

        store = EvolverTreeStore(
            repo_root=Path("/path/to/raven"),
            nodes_dir=Path("/path/to/raven/evolver/nodes"),
        )
        store.save_node(root)
        child = store.create_child_node(
            parent_node_id=root.node_id,
            patch=applied_patch,
            iter_index=1,
            commit_message="[auto-evolver] hook_new: repetition_breaker",
        )
        view = store.build_tree()
        for d in view.descendants(root.node_id):
            print(d)
    """

    NODE_FILE_SUFFIX = ".json"

    def __init__(
        self,
        repo_root: Union[str, Path],
        nodes_dir: Union[str, Path],
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.nodes_dir = Path(nodes_dir).resolve()
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

    # ---- single-node IO ----------------------------------------------------

    def _node_path(self, node_id: str) -> Path:
        return self.nodes_dir / f"{node_id}{self.NODE_FILE_SUFFIX}"

    def save_node(self, node: HarnessNode) -> Path:
        """Write ``node``'s metadata JSON to ``nodes_dir/<node_id>.json``.

        Atomic on POSIX (temp + rename, inherited from
        :meth:`HarnessNode.save`). Idempotent — overwriting an existing
        file with the same node id is allowed.
        """
        path = self._node_path(node.node_id)
        node.save(path)
        return path

    def load_node(self, node_id: str) -> HarnessNode:
        """Load one node by id. Raises :class:`FileNotFoundError`
        if the JSON is missing."""
        path = self._node_path(node_id)
        if not path.exists():
            raise FileNotFoundError(
                f"node {node_id!r} not found in {self.nodes_dir}"
            )
        return HarnessNode.load(path)

    def has_node(self, node_id: str) -> bool:
        return self._node_path(node_id).exists()

    # ---- multi-node IO -----------------------------------------------------

    def load_all_nodes(self) -> list[HarnessNode]:
        """Load every node JSON in ``nodes_dir``. Sorted by file name
        for deterministic test ordering. Files that fail to parse are
        skipped with a warning log (they could be in-progress writes
        or schema-version mismatches)."""
        out: list[HarnessNode] = []
        for jpath in sorted(self.nodes_dir.glob(f"*{self.NODE_FILE_SUFFIX}")):
            try:
                out.append(HarnessNode.load(jpath))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Skipping unloadable node file %s: %s", jpath, exc
                )
        return out

    def build_tree(self) -> TreeView:
        """Load all nodes and assemble a :class:`TreeView`.

        Topology rules:

        - A node with ``parent_id=None`` is the root. If multiple roots
          exist (shouldn't happen but defensible) the *first by node_id*
          wins; the others are reported in ``orphans``.
        - A node whose ``parent_id`` is not in the loaded set is added
          to ``orphans`` (dangling parent pointer).
        - ``children_of`` is built only for known parents.
        """
        nodes_list = self.load_all_nodes()
        nodes = {n.node_id: n for n in nodes_list}
        children_of: dict[str, list[str]] = {nid: [] for nid in nodes}
        root_candidates: list[str] = []
        orphans: list[str] = []

        for n in sorted(nodes_list, key=lambda x: x.node_id):
            if n.parent_id is None:
                root_candidates.append(n.node_id)
                continue
            if n.parent_id not in nodes:
                orphans.append(n.node_id)
                continue
            children_of[n.parent_id].append(n.node_id)

        root_id: Optional[str] = None
        if root_candidates:
            root_id = root_candidates[0]
            # Any additional roots are reported as orphans-by-policy
            if len(root_candidates) > 1:
                logger.warning(
                    "Multiple root nodes detected; keeping %s, treating "
                    "%s as orphans",
                    root_id,
                    root_candidates[1:],
                )
                orphans.extend(root_candidates[1:])

        # Sort each children list for determinism
        for k in children_of:
            children_of[k].sort()

        return TreeView(
            nodes=nodes,
            children_of=children_of,
            root_id=root_id,
            orphans=sorted(orphans),
        )

    # ---- atomic child creation (C2.4, in next step) ------------------------
    # See create_child_node below for the apply-patch+commit+save atomic op.

    def create_child_node(
        self,
        parent_node_id: str,
        patch: AppliedPatch,
        iter_index: int,
        commit_message: str,
        *,
        git_branch: Optional[str] = None,
        create_branch_ref: bool = False,
        author_name: str = "evolver-bot",
        author_email: str = "evolver@raven.local",
        guard_immutable: bool = True,
    ) -> HarnessNode:
        """Atomic operation: apply ``patch`` on top of the parent's
        git commit, create a child :class:`HarnessNode`, and persist
        its JSON.

        On failure at any step, the file system + git ref state is
        left as it was before the call (caveat: a dangling commit
        without a JSON metadata file is unreachable by node id and
        will be garbage-collected by Git eventually).

        :param parent_node_id: id of the parent node (must already be saved)
        :param patch: the :class:`AppliedPatch` whose unified diff will
            be applied
        :param iter_index: evolver iteration number; folded into the
            generated child node_id
        :param commit_message: passed verbatim to git commit-tree
        :param git_branch: branch name to record on the child node.
            Inherited from the parent if not given.
        :param create_branch_ref: if True, also create a Git branch
            ref pointing at the new commit (useful for human-readable
            navigation; not required for evolver operation)
        :param author_name / author_email: git author / committer
            identity
        :param guard_immutable: if True (default), runs
            :func:`assert_patch_allowed` against the patch's target
            files before applying. Set False only in test code.

        :returns: the new child :class:`HarnessNode`, already saved
            to disk and ready for evaluation
        :raises ImmutablePathError: if the patch hits the immutable
            kernel (spec §22)
        :raises ValueError: parent missing, malformed patch
        :raises GitOpError: git command failed (typically because
            the patch didn't apply cleanly to the parent's tree)
        """
        # Step 0: load parent (raises FileNotFoundError if missing)
        parent = self.load_node(parent_node_id)

        # Step 0.5: gate against immutable kernel modification
        if guard_immutable:
            target_files = [c.target_file for c in patch.components]
            assert_patch_allowed(target_files)

        # Step 1: construct the child commit (does NOT touch working tree)
        child_sha = git_ops.apply_patch_as_commit(
            repo_root=self.repo_root,
            parent_sha=parent.git_commit_sha,
            unified_diff=patch.diff,
            message=commit_message,
            author_name=author_name,
            author_email=author_email,
        )

        # Step 2: assemble the HarnessNode metadata
        node_id = f"v{iter_index}-{child_sha[:8]}"
        # In the rare case the short-SHA collides with an existing
        # node id (~1 in 4 billion), retry with a longer suffix.
        if self.has_node(node_id):
            node_id = f"v{iter_index}-{child_sha[:12]}"

        branch_for_node = git_branch or parent.git_branch
        child = HarnessNode(
            node_id=node_id,
            parent_id=parent_node_id,
            git_commit_sha=child_sha,
            git_branch=branch_for_node,
            created_at=HarnessNode.utc_now(),
            created_at_iter=iter_index,
            core_version=HarnessNode.current_core_version(),
            status=NodeStatus.active,
            patch=patch,
        )

        # Step 3: persist JSON (atomic via HarnessNode.save → temp+rename)
        self.save_node(child)

        # Step 4 (optional): create a branch ref for human navigation
        if create_branch_ref:
            ref_name = f"evolver/{node_id}"
            if not git_ops.branch_exists(self.repo_root, ref_name):
                git_ops.create_branch(self.repo_root, ref_name, child_sha)

        return child


__all__ = [
    "EvolverTreeStore",
    "TreeView",
]
