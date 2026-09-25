"""对照样品独立来源筛选接口端到端测试。

覆盖:取样根反查与合并路径去重、与参考样品共享根不可选、候选两两互斥的
最大入选集合与下标字典序 tie-break、未入选样品的共享根明细、输入校验
(1~16 个、不重复、均须已存在)、分装/合并后按当前谱系实时重算。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app, get_conn


@pytest.fixture()
def client():
    mem = connect(":memory:")
    init_db(mem)

    def _override():
        yield mem

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        c.db = mem  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    mem.close()


def _sampling(client, op_no, sid, amount=100, unit="mL"):
    r = client.post("/api/v1/operations/sampling", json={
        "type": "sampling", "op_no": op_no, "sample_id": sid,
        "material": "血清", "amount": amount, "unit": unit,
    })
    assert r.status_code == 200, r.text


def _aliquot(client, op_no, parent, outputs):
    r = client.post("/api/v1/operations/aliquot", json={
        "type": "aliquot", "op_no": op_no, "parent_id": parent,
        "outputs": [{"sample_id": s, "amount": a} for s, a in outputs],
    })
    assert r.status_code == 200, r.text


def _merge(client, op_no, inputs, output, material=None):
    body = {
        "type": "merge", "op_no": op_no,
        "inputs": [{"sample_id": s, "amount": a} for s, a in inputs],
        "output_id": output,
    }
    if material:
        body["material"] = material
    r = client.post("/api/v1/operations/merge", json=body)
    assert r.status_code == 200, r.text


def _screen(client, ref, candidates):
    return client.post("/api/v1/controls/screen", json={
        "reference_sample_id": ref,
        "candidate_sample_ids": candidates,
    })


def _three_independent_lineages(client):
    """S0 -> A1;S1 -> B1;S2 -> C1(三条互不相干的谱系)。"""
    _sampling(client, "OP-S0", "S0")
    _aliquot(client, "OP-A1", "S0", [("A1", 50)])
    _sampling(client, "OP-S1", "S1")
    _aliquot(client, "OP-B1", "S1", [("B1", 50)])
    _sampling(client, "OP-S2", "S2")
    _aliquot(client, "OP-C1", "S2", [("C1", 50)])


# ---------------------------------------------------------------- 基本筛选

def test_basic_selection_and_reference_shared_excluded(client):
    _three_independent_lineages(client)
    r = _screen(client, "A1", ["B1", "C1", "S0"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference_sample_id"] == "A1"
    assert body["reference_roots"] == ["S0"]
    assert body["selected_count"] == 2
    # B1、C1 来源互相独立,全部入选(按候选下标升序)
    assert [(x["sample_id"], x["input_index"]) for x in body["selected"]] == [
        ("B1", 0), ("C1", 1),
    ]
    assert body["selected"][0]["roots"] == ["S1"]
    assert body["selected"][1]["roots"] == ["S2"]
    # S0 与参考样品 A1 共享根 S0,不可选
    assert len(body["excluded"]) == 1
    ex = body["excluded"][0]
    assert ex["sample_id"] == "S0" and ex["input_index"] == 2
    assert ex["shared_with_reference"] == ["S0"]
    assert ex["shared_with_selected"] == []
    assert ex["shared_roots"] == ["S0"]


def test_merge_paths_dedup_roots(client):
    # S0 -> A1/A2;A1+A2 合流成 B1:B1 沿两条路径到达 S0,根只计一次
    _sampling(client, "OP-1", "S0")
    _aliquot(client, "OP-2", "S0", [("A1", 40), ("A2", 30)])
    _merge(client, "OP-3", [("A1", 10), ("A2", 10)], "B1", material="混合血清")
    _sampling(client, "OP-4", "S1")
    _aliquot(client, "OP-5", "S1", [("D1", 20)])

    r = _screen(client, "B1", ["A1", "D1"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference_roots"] == ["S0"]  # 合流去重,只出现一次
    assert [x["sample_id"] for x in body["selected"]] == ["D1"]
    ex = body["excluded"][0]
    assert ex["sample_id"] == "A1"
    assert ex["shared_with_reference"] == ["S0"]
    assert ex["shared_roots"] == ["S0"]


def test_root_sample_itself_as_reference_and_candidate(client):
    _three_independent_lineages(client)
    # 参考样品本身是取样根:根集合即自身
    r = _screen(client, "S0", ["S0", "B1"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference_roots"] == ["S0"]
    # 候选即参考样品本身:共享全部根,不可选
    ex = body["excluded"][0]
    assert ex["sample_id"] == "S0"
    assert ex["shared_with_reference"] == ["S0"]
    assert [x["sample_id"] for x in body["selected"]] == ["B1"]


# ---------------------------------------------------------------- 最大集合与字典序

def test_pairwise_conflicts_max_independent_set(client):
    # R1 -> P1、X1;R2 -> P2、X3;P1+P2 -> X2(根 {R1,R2});R3 -> X4
    _sampling(client, "OP-R1", "R1")
    _sampling(client, "OP-R2", "R2")
    _sampling(client, "OP-R3", "R3")
    _aliquot(client, "OP-P1", "R1", [("P1", 50)])
    _aliquot(client, "OP-P2", "R2", [("P2", 50)])
    _aliquot(client, "OP-X1", "R1", [("X1", 10)])
    _aliquot(client, "OP-X3", "R2", [("X3", 10)])
    _aliquot(client, "OP-X4", "R3", [("X4", 10)])
    _merge(client, "OP-X2", [("P1", 10), ("P2", 10)], "X2", material="混合")
    _sampling(client, "OP-REF", "REF")

    r = _screen(client, "REF", ["X1", "X2", "X3", "X4"])
    assert r.status_code == 200, r.text
    body = r.json()
    # X2 与 X1(共 R1)、X3(共 R2)互斥;最大集合为 {X1, X3, X4}
    assert body["selected_count"] == 3
    assert [(x["sample_id"], x["input_index"]) for x in body["selected"]] == [
        ("X1", 0), ("X3", 2), ("X4", 3),
    ]
    assert len(body["excluded"]) == 1
    ex = body["excluded"][0]
    assert ex["sample_id"] == "X2" and ex["input_index"] == 1
    assert ex["shared_with_reference"] == []
    assert ex["shared_with_selected"] == [
        {"sample_id": "X1", "input_index": 0, "shared_roots": ["R1"]},
        {"sample_id": "X3", "input_index": 2, "shared_roots": ["R2"]},
    ]
    assert ex["shared_roots"] == ["R1", "R2"]


def test_tie_break_lexicographic_input_index(client):
    # Y1、Y2 同根互斥:最大集合大小为 1,取候选输入下标字典序最小者
    _sampling(client, "OP-R", "R")
    _aliquot(client, "OP-Y", "R", [("Y1", 10), ("Y2", 10)])
    _sampling(client, "OP-REF", "REF")

    body = _screen(client, "REF", ["Y1", "Y2"]).json()
    assert [x["sample_id"] for x in body["selected"]] == ["Y1"]
    ex = body["excluded"][0]
    assert ex["sample_id"] == "Y2"
    assert ex["shared_with_selected"] == [
        {"sample_id": "Y1", "input_index": 0, "shared_roots": ["R"]},
    ]

    # 交换输入顺序,结果跟随下标而非样品号
    body = _screen(client, "REF", ["Y2", "Y1"]).json()
    assert [x["sample_id"] for x in body["selected"]] == ["Y2"]
    assert body["excluded"][0]["sample_id"] == "Y1"


def test_tie_break_prefers_smaller_index_list(client):
    # A、B 同根互斥,C 独立:最大集合 {A,C}=[0,2] 与 {B,C}=[1,2] 并列,取 [0,2]
    _sampling(client, "OP-R1", "R1")
    _aliquot(client, "OP-AB", "R1", [("A", 10), ("B", 10)])
    _sampling(client, "OP-R2", "R2")
    _aliquot(client, "OP-C", "R2", [("C", 10)])
    _sampling(client, "OP-REF", "REF")

    body = _screen(client, "REF", ["A", "B", "C"]).json()
    assert body["selected_count"] == 2
    assert [(x["sample_id"], x["input_index"]) for x in body["selected"]] == [
        ("A", 0), ("C", 2),
    ]
    ex = body["excluded"][0]
    assert ex["sample_id"] == "B"
    assert ex["shared_with_selected"] == [
        {"sample_id": "A", "input_index": 0, "shared_roots": ["R1"]},
    ]


def test_all_candidates_share_reference_root_selects_none(client):
    _sampling(client, "OP-1", "S0")
    _aliquot(client, "OP-2", "S0", [("A1", 40), ("A2", 30)])
    body = _screen(client, "S0", ["A1", "A2"]).json()
    assert body["selected_count"] == 0
    assert body["selected"] == []
    assert [x["sample_id"] for x in body["excluded"]] == ["A1", "A2"]
    assert all(x["shared_with_reference"] == ["S0"] for x in body["excluded"])


def test_sixteen_candidates_all_selected(client):
    _sampling(client, "OP-REF", "REF")
    candidates = []
    for i in range(16):
        root, child = f"R{i:02d}", f"C{i:02d}"
        _sampling(client, f"OP-R{i:02d}", root)
        _aliquot(client, f"OP-C{i:02d}", root, [(child, 10)])
        candidates.append(child)
    body = _screen(client, "REF", candidates).json()
    assert body["selected_count"] == 16
    assert [x["sample_id"] for x in body["selected"]] == candidates
    assert body["excluded"] == []


# ---------------------------------------------------------------- 输入校验

def test_reference_not_found(client):
    _three_independent_lineages(client)
    r = _screen(client, "GHOST", ["B1"])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert conflicts[0]["code"] == "sample_not_found"
    assert conflicts[0]["sample_id"] == "GHOST"


def test_candidates_not_found_collected(client):
    _three_independent_lineages(client)
    r = _screen(client, "A1", ["B1", "G1", "C1", "G2"])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert [c["code"] for c in conflicts] == ["sample_not_found"] * 2
    # 候选冲突带候选下标,且一次收集全部缺失候选
    assert [(c["sample_id"], c["batch_index"]) for c in conflicts] == [
        ("G1", 1), ("G2", 3),
    ]


def test_reference_and_candidates_not_found_together(client):
    r = _screen(client, "GREF", ["G1"])
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert [c["sample_id"] for c in conflicts] == ["GREF", "G1"]


def test_candidate_count_bounds(client):
    _sampling(client, "OP-REF", "REF")
    assert _screen(client, "REF", []).status_code == 422
    assert _screen(client, "REF", [f"C{i}" for i in range(17)]).status_code == 422


def test_duplicate_candidates_rejected(client):
    _sampling(client, "OP-REF", "REF")
    _sampling(client, "OP-C", "C1")
    r = _screen(client, "REF", ["C1", "C1"])
    assert r.status_code == 422


def test_empty_ids_rejected(client):
    _sampling(client, "OP-REF", "REF")
    assert _screen(client, "", ["C1"]).status_code == 422
    assert _screen(client, "REF", [""]).status_code == 422


# ---------------------------------------------------------------- 实时重算

def test_requery_reflects_current_lineage(client):
    # S0 -> R(参考);S1 -> C1(候选):初始来源独立,C1 入选
    _sampling(client, "OP-1", "S0")
    _aliquot(client, "OP-2", "S0", [("R", 50)])
    _sampling(client, "OP-3", "S1")
    _aliquot(client, "OP-4", "S1", [("C1", 50)])

    body = _screen(client, "R", ["C1"]).json()
    assert [x["sample_id"] for x in body["selected"]] == ["C1"]
    assert body["excluded"] == []

    # 把 R 并入已存在的 C1 后,C1 的根变为 {S0, S1},与参考共享 S0
    _merge(client, "OP-5", [("R", 10)], "C1")
    body = _screen(client, "R", ["C1"]).json()
    assert body["selected"] == []
    ex = body["excluded"][0]
    assert ex["sample_id"] == "C1"
    assert ex["shared_with_reference"] == ["S0"]
    assert ex["shared_roots"] == ["S0"]


def test_requery_after_new_aliquot_from_reference(client):
    _three_independent_lineages(client)
    # 初次查询:B1 入选
    body = _screen(client, "A1", ["B1"]).json()
    assert body["selected_count"] == 1
    # 参考下游新分装出 A2,作为新候选与参考共享根 S0,不可选
    _aliquot(client, "OP-NEW", "A1", [("A2", 5)])
    body = _screen(client, "A1", ["B1", "A2"]).json()
    assert [x["sample_id"] for x in body["selected"]] == ["B1"]
    ex = body["excluded"][0]
    assert ex["sample_id"] == "A2"
    assert ex["shared_with_reference"] == ["S0"]


def test_screen_is_read_only(client):
    _three_independent_lineages(client)
    before = client.get("/api/v1/checks/balances").json()
    assert before["consistent"] is True
    _screen(client, "A1", ["B1", "C1", "S0"])
    after = client.get("/api/v1/checks/balances").json()
    assert after["consistent"] is True
    assert [s["sample_id"] for s in after["samples"]] == [
        s["sample_id"] for s in before["samples"]
    ]
