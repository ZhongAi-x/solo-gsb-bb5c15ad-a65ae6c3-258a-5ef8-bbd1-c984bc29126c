"""对照样品独立来源筛选:为参考(对照)样品筛出来源互不相同的候选集合。

规则
----
- 输入一个参考样品号与 1~16 个不重复的候选样品号,均须为已存在的样品;
  参考或候选不存在返回 `409 sample_not_found`(候选冲突带候选下标)。
- 沿当前谱系边(`lineage_edges` 父->子)反查每个样品的全部取样根:
  沿父边向上走到无父边的取样源头;合并/合流路径去重,同一根只计一次。
- 与参考样品共享任一根的候选不可选;其余候选两两之间也不得共享根。
- 在可选候选中求数量最多的入选集合;数量并列时按候选输入顺序的下标列表
  (升序)字典序最小确定唯一结果。
- 返回入选样品,并为每个未入选候选列出其与参考样品或入选样品共享的根。

实时性
------
筛选不持久化任何结果,每次请求都按当前 `lineage_edges` 实时重算;
请求之后新做的分装/合并(含并入已有样品)会在下一次查询中体现。
"""
from __future__ import annotations

import itertools
import sqlite3

from . import schemas as s
from . import service


def _parents_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """当前全部谱系边的 子 -> [父] 邻接表。"""
    by_child: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT parent_id, child_id FROM lineage_edges WHERE parent_id IS NOT NULL"
    ):
        by_child.setdefault(row["child_id"], []).append(row["parent_id"])
    return by_child


def _sampling_roots(by_child: dict[str, list[str]], sample_id: str) -> set[str]:
    """沿父边反查 sample_id 的全部取样根(无父边的祖先;自身无父边时即自身)。

    合并/合流路径去重:同一根经多条路径到达只计一次。
    """
    roots: set[str] = set()
    seen: set[str] = set()
    stack = [sample_id]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        parents = by_child.get(node)
        if not parents:
            roots.add(node)
        else:
            stack.extend(parents)
    return roots


def _max_independent_positions(masks: list[int]) -> list[int]:
    """在两两根集合不相交的约束下求数量最多的下标集合。

    masks[i] 为第 i 个可选候选的根集合位掩码;数量并列时返回下标列表
    (升序)字典序最小的集合(combinations 本身按字典序枚举,先中即最优)。
    """
    n = len(masks)
    for size in range(n, 0, -1):
        for combo in itertools.combinations(range(n), size):
            acc = 0
            for pos in combo:
                if masks[pos] & acc:
                    break
                acc |= masks[pos]
            else:
                return list(combo)
    return []


def screen_independent_sources(
    conn: sqlite3.Connection, req: s.ControlScreenRequest
) -> s.ControlScreenResponse:
    """按当前谱系实时筛选(只读,不写库);样品不存在返回 409。"""
    candidates = list(req.candidate_sample_ids)
    all_ids = [req.reference_sample_id, *candidates]
    found = {
        r["sample_id"]
        for r in conn.execute(
            f"SELECT sample_id FROM samples"
            f" WHERE sample_id IN ({','.join('?' * len(all_ids))})",
            all_ids,
        ).fetchall()
    }
    conflicts: list[s.Conflict] = []
    if req.reference_sample_id not in found:
        conflicts.append(s.Conflict(
            code="sample_not_found",
            message=f"参考样品 {req.reference_sample_id} 不存在",
            sample_id=req.reference_sample_id,
        ))
    conflicts.extend(
        s.Conflict(
            code="sample_not_found",
            message=f"候选样品 {sid} 不存在(候选下标 {idx})",
            sample_id=sid,
            batch_index=idx,
        )
        for idx, sid in enumerate(candidates)
        if sid not in found
    )
    if conflicts:
        raise service.ConflictError(conflicts)

    by_child = _parents_map(conn)
    ref_roots = _sampling_roots(by_child, req.reference_sample_id)
    cand_roots = [_sampling_roots(by_child, sid) for sid in candidates]

    # 与参考样品共享任一根的候选不可选,其余进入两两互斥筛选
    eligible = [i for i in range(len(candidates)) if not (cand_roots[i] & ref_roots)]

    # 根集合映射为位掩码(Python 整数任意精度),便于两两判交
    universe = sorted({r for i in eligible for r in cand_roots[i]})
    bit = {root: 1 << i for i, root in enumerate(universe)}
    masks = []
    for i in eligible:
        mask = 0
        for root in cand_roots[i]:
            mask |= bit[root]
        masks.append(mask)

    selected_idx = [eligible[p] for p in _max_independent_positions(masks)]
    selected_set = set(selected_idx)

    selected_out = [
        s.ControlScreenSelectedOut(
            sample_id=candidates[i], input_index=i, roots=sorted(cand_roots[i])
        )
        for i in selected_idx
    ]
    excluded_out: list[s.ControlScreenExcludedOut] = []
    for idx in range(len(candidates)):
        if idx in selected_set:
            continue
        shared_ref = sorted(cand_roots[idx] & ref_roots)
        shared_sel = [
            s.ControlScreenSharedWithSelectedOut(
                sample_id=candidates[j],
                input_index=j,
                shared_roots=sorted(cand_roots[idx] & cand_roots[j]),
            )
            for j in selected_idx
            if cand_roots[idx] & cand_roots[j]
        ]
        union = set(shared_ref)
        for item in shared_sel:
            union.update(item.shared_roots)
        excluded_out.append(s.ControlScreenExcludedOut(
            sample_id=candidates[idx],
            input_index=idx,
            shared_with_reference=shared_ref,
            shared_with_selected=shared_sel,
            shared_roots=sorted(union),
        ))

    return s.ControlScreenResponse(
        reference_sample_id=req.reference_sample_id,
        reference_roots=sorted(ref_roots),
        selected_count=len(selected_out),
        selected=selected_out,
        excluded=excluded_out,
    )
