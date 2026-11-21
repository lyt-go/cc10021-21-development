"""关系版本历史查询测试：覆盖时间相交判定、缺证留痕、补证重试与变更评审回写。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.lineage_graph import (  # noqa: E402
    CONCLUSION_DISJOINT,
    CONCLUSION_NEEDS_EVIDENCE,
    CONCLUSION_SIGNABLE,
    VERSION_COLLECTED,
    VERSION_MISSING,
    VERSION_RETRIED,
    VERSION_RETRY_FAILED,
    LineageGraphService,
)
from app.services.metadata_diff import MetadataDiffService  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def reset_store():
    """每个用例前重建种子数据，避免补证/回写污染其他链路测试。"""
    store.reset()
    yield
    store.reset()


def _by_conclusion(result):
    return {
        CONCLUSION_SIGNABLE: [p["终点表"] for p in result["可直接签发路径"]],
        CONCLUSION_NEEDS_EVIDENCE: [p["终点表"] for p in result["需补证路径"]],
        CONCLUSION_DISJOINT: [p["终点表"] for p in result["时间无交集路径"]],
    }


class TestRelationVersionQuery:
    """历史路径版本查询：只有时间范围相交的边才能组成有效路径。"""

    def test_intersecting_edges_form_signable_path(self):
        svc = LineageGraphService()
        # 2026-05-01 落在 LINE-H001(01-01起) 与 LINE-H002(02-01起) 的交集内
        result = svc.query_relation_versions("ods_order_info", target_date="2026-05-01")
        grouped = _by_conclusion(result)
        assert "ads_sale_report" in grouped[CONCLUSION_SIGNABLE]
        signable = next(p for p in result["可直接签发路径"] if p["终点表"] == "ads_sale_report")
        assert signable["时间相交"] is True
        assert signable["覆盖目标日期"] is True
        assert signable["缺证关系编号"] == []
        assert signable["路径交集窗口"][0] == "2026-02-01"

    def test_disjoint_edges_filtered_at_target_date(self):
        svc = LineageGraphService()
        # LINE-H003 04-01 才生效；LINE-H005 07-01 才生效。
        # 2026-05-01 时第二条边尚未上线，路径不能在该日期下发。
        result = svc.query_relation_versions("ods_order_info", target_date="2026-05-01")
        grouped = _by_conclusion(result)
        assert "ads_retention_board" in grouped[CONCLUSION_DISJOINT]
        disjoint = next(p for p in result["时间无交集路径"] if p["终点表"] == "ads_retention_board")
        assert disjoint["覆盖目标日期"] is False

    def test_boundary_before_and_after_second_edge_effective(self):
        """同一目标表在两次生效交界前后的依赖差异。"""
        svc = LineageGraphService()
        before = svc.query_relation_versions("ods_order_info", target_date="2026-06-15")
        after = svc.query_relation_versions("ods_order_info", target_date="2026-08-01")
        before_grouped = _by_conclusion(before)
        after_grouped = _by_conclusion(after)
        # 交界前：留存看板路径无交集；交界后：两条边窗口相交，可直接签发
        assert "ads_retention_board" in before_grouped[CONCLUSION_DISJOINT]
        assert "ads_retention_board" in after_grouped[CONCLUSION_SIGNABLE]
        # 销售报表路径两个时点都成立
        assert "ads_sale_report" in before_grouped[CONCLUSION_SIGNABLE]
        assert "ads_sale_report" in after_grouped[CONCLUSION_SIGNABLE]

    def test_missing_history_version_kept_as_evidence_gap(self):
        """缺少历史版本的边不能被视作无依赖，须保留为缺证片段。"""
        svc = LineageGraphService()
        result = svc.query_relation_versions("ods_order_info", target_date="2026-05-01")
        grouped = _by_conclusion(result)
        assert "ads_click_funnel" in grouped[CONCLUSION_NEEDS_EVIDENCE]
        gap = next(p for p in result["需补证路径"] if p["终点表"] == "ads_click_funnel")
        assert "LINE-H007" in gap["缺证关系编号"]
        # 缺证片段单独留痕
        fragment_nos = [f["关系编号"] for f in result["缺证片段"]]
        assert "LINE-H007" in fragment_nos
        assert "LINE-H009" in fragment_nos

    def test_range_query_uses_window_overlap(self):
        svc = LineageGraphService()
        # 区间查询：2026-03 内留存看板两条边仍无交集
        result = svc.query_relation_versions(
            "ods_order_info", range_start="2026-03-01", range_end="2026-03-31")
        grouped = _by_conclusion(result)
        assert "ads_retention_board" in grouped[CONCLUSION_DISJOINT]
        assert result["查询区间"] == ["2026-03-01", "2026-03-31"]

    def test_stopped_edge_excluded_from_paths(self):
        """已停用关系（LINE-H004）不参与历史路径拼装。"""
        svc = LineageGraphService()
        result = svc.query_relation_versions("ods_order_info", target_date="2026-02-15")
        all_paths = (result["可直接签发路径"] + result["需补证路径"]
                     + result["时间无交集路径"])
        for path in all_paths:
            for edge in path["边明细"]:
                assert edge["关系编号"] != "LINE-H004"

    def test_include_missing_false_hides_paths_but_keeps_fragments(self):
        """关闭缺证路径展示时，需补证路径被过滤，缺证片段仍单独留痕。"""
        svc = LineageGraphService()
        result = svc.query_relation_versions(
            "ods_order_info", target_date="2026-05-01", include_missing=False)
        assert result["需补证数"] == 0
        assert result["需补证路径"] == []
        # 缺证片段不随路径过滤而消失，避免被误判为无依赖
        fragment_nos = {f["关系编号"] for f in result["缺证片段"]}
        assert {"LINE-H007", "LINE-H009"}.issubset(fragment_nos)

    def test_upstream_direction_walks_reverse_dependencies(self):
        svc = LineageGraphService()
        result = svc.query_relation_versions(
            "ads_sale_report", target_date="2026-05-01", direction="upstream")
        assert result["查询方向"] == "上游依赖"
        endpoints = [p["终点表"] for p in result["可直接签发路径"]]
        assert "ods_order_info" in endpoints


class TestRetryMissingVersions:
    """补采失败后重试：缺证片段与已检验路径继续分开。"""

    def test_retry_success_fills_evidence_and_keeps_verified_intact(self):
        svc = LineageGraphService()
        # 重试前快照全部边的版本状态，验证只有缺证边会被重试流程改动
        snapshot_before = {
            r["关系编号"]: (r["版本校验状态"], r.get("补采次数", 0),
                         r.get("生效开始时间"), r.get("生效结束时间"))
            for r in store.rows("lineage_graph")
        }

        result = svc.retry_missing_versions("dwd_log_event", relation_no="LINE-H007")
        assert result["补证成功数"] == 1
        assert result["补证失败数"] == 0
        assert result["补证成功片段"][0]["关系编号"] == "LINE-H007"
        # 已检验路径边（含同表的已采集边 H006）原样保留
        verified_nos = [e["关系编号"] for e in result["已检验路径边"]]
        assert "LINE-H006" in verified_nos
        for edge in result["已检验路径边"]:
            if edge["关系编号"] != "LINE-H007":
                assert edge["版本校验状态"] == VERSION_COLLECTED
                assert edge["补采次数"] == 0

        snapshot_after = {
            r["关系编号"]: (r["版本校验状态"], r.get("补采次数", 0),
                         r.get("生效开始时间"), r.get("生效结束时间"))
            for r in store.rows("lineage_graph")
        }
        changed = [no for no in snapshot_before if snapshot_before[no] != snapshot_after[no]]
        assert changed == ["LINE-H007"]

        # 补证后重新查询：漏斗路径升级为可直接签发
        after = svc.query_relation_versions("ods_order_info", target_date="2026-05-01")
        grouped = _by_conclusion(after)
        assert "ads_click_funnel" in grouped[CONCLUSION_SIGNABLE]

    def test_retry_failure_preserves_gap_evidence(self):
        svc = LineageGraphService()
        result = svc.retry_missing_versions(
            "dim_product_sku", relation_no="LINE-H009", simulate_failure=True)
        assert result["补证失败数"] == 1
        assert result["补证成功数"] == 0
        edge = next(r for r in store.rows("lineage_graph") if r["关系编号"] == "LINE-H009")
        assert edge["版本校验状态"] == VERSION_RETRY_FAILED
        assert edge["补采次数"] == 2  # 种子里已失败 1 次，再失败累计到 2
        # 缺证证据仍在，路径继续归入需补证而非被当作无依赖
        query = svc.query_relation_versions("ods_order_info", target_date="2026-05-01")
        grouped = _by_conclusion(query)
        assert "ads_inventory_daily" in grouped[CONCLUSION_NEEDS_EVIDENCE]
        assert any(f["关系编号"] == "LINE-H009" for f in query["缺证片段"])

    def test_retry_only_touches_missing_edges(self):
        svc = LineageGraphService()
        result = svc.retry_missing_versions("ods_order_info")
        touched = [f["关系编号"] for f in result["补证成功片段"] + result["补证失败片段"]]
        # 已采集的边绝不进入重试候选
        assert "LINE-H001" not in touched
        assert "LINE-H006" not in touched
        for edge in result["已检验路径边"]:
            assert edge["版本校验状态"] in (VERSION_COLLECTED, VERSION_RETRIED)


class TestImpactWritebackAndReview:
    """历史查询结果回写变更影响清单，评审检索可证实下游。"""

    def test_writeback_persists_historical_detail_on_diff(self):
        diff_svc = MetadataDiffService()
        result = diff_svc.write_historical_impact(4, target_date="2026-05-01")
        assert result["ok"] is True
        assert result["影响清单状态"] == "待补证"
        diff = store.find("metadata_diff", 4)
        detail = diff["历史依赖明细"]
        assert detail["可直接签发下游"] == ["ads_sale_report"]
        assert "ads_click_funnel" in detail["需补证下游"]
        assert "ads_retention_board" in detail["时间无交集下游"]
        assert any(f["关系编号"] == "LINE-H007" for f in detail["缺证片段"])

    def test_review_returns_signable_and_gap_objects_with_related_diff(self):
        diff_svc = MetadataDiffService()
        review = diff_svc.review_verifiable_downstream(
            "ods_order_info", target_date="2026-05-01")
        signable_tables = [item["下游表"] for item in review["可直接签发对象"]]
        gap_tables = [item["下游表"] for item in review["需补证对象"]]
        filtered_tables = [item["下游表"] for item in review["已过滤无交集路径"]]
        assert "ads_sale_report" in signable_tables
        assert "ads_click_funnel" in gap_tables
        assert "ads_inventory_daily" in gap_tables
        assert "ads_retention_board" in filtered_tables
        # 关联变更结论可打开核查
        assert any(d["变更编号"] == "META-0004" for d in review["关联变更记录"])

    def test_review_after_retry_moves_object_to_signable(self):
        """补证回写后再次评审：需补证对象变为可直接签发。"""
        lineage_svc = LineageGraphService()
        lineage_svc.retry_missing_versions("dwd_log_event", relation_no="LINE-H007")
        diff_svc = MetadataDiffService()
        writeback = diff_svc.write_historical_impact(4, target_date="2026-05-01")
        assert writeback["影响清单状态"] == "待补证"  # 仍有 LINE-H009 缺证
        review = diff_svc.review_verifiable_downstream(
            "ods_order_info", target_date="2026-05-01")
        signable_tables = [item["下游表"] for item in review["可直接签发对象"]]
        assert "ads_click_funnel" in signable_tables
        gap_tables = [item["下游表"] for item in review["需补证对象"]]
        assert "ads_inventory_daily" in gap_tables
        # 全部补证后影响清单转为可评审
        lineage_svc.retry_missing_versions("dim_product_sku", relation_no="LINE-H009")
        final = diff_svc.write_historical_impact(4, target_date="2026-05-01")
        assert final["影响清单状态"] == "可评审"
        assert final["历史依赖明细"]["需补证数"] == 0

    def test_writeback_unknown_diff_returns_failure(self):
        diff_svc = MetadataDiffService()
        result = diff_svc.write_historical_impact(999, target_date="2026-05-01")
        assert result["ok"] is False
