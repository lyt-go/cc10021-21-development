"""元数据变更对比服务：检测和记录表结构变更，驱动血缘图谱更新。

domain_chain: 元数据变更对比 (metadata_diff)
  └─ 校验 → 血缘图谱更新 (lineage_graph)
  └─ 关联 → 质量事件 (dq_incident)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.lineage_graph import (
    CONCLUSION_DISJOINT,
    CONCLUSION_NEEDS_EVIDENCE,
    CONCLUSION_SIGNABLE,
    LineageGraphService,
)
from app.store import store


class MetadataDiffService(BaseModuleService):
    MODULE = "metadata_diff"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["变更编号", "变更对象", "变更类型", "旧值", "新值", "变更批次",
                   "影响评估", "变更时间", "历史依赖明细", "影响清单状态"]
    REQUIRED_FIELDS = ["变更编号", "变更对象", "变更类型"]
    STATUS_ORDER = ["草稿", "待审核", "已确认", "已回滚"]
    ACTION_RULES = {"提交审核": "待审核", "确认变更": "已确认", "执行回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    def compare_schemas(self, old_columns: list[dict], new_columns: list[dict],
                         table_name: str) -> dict[str, Any]:
        """对比两次元数据采集的表结构差异，生成变更记录。

        解析新老字段列表，识别新增、删除、类型变更等差异，
        校验变更影响范围并写入变更记录，触发血缘图谱的更新。
        """
        old_map = {c.get("字段名称"): c for c in old_columns if c.get("字段名称")}
        new_map = {c.get("字段名称"): c for c in new_columns if c.get("字段名称")}
        old_names = set(old_map.keys())
        new_names = set(new_map.keys())

        additions = new_names - old_names
        deletions = old_names - new_names
        common = old_names & new_names

        changes = []
        # 识别新增字段
        for col_name in additions:
            changes.append({"字段": col_name, "变更类型": "新增",
                            "旧值": "N/A",
                            "新值": f"类型:{new_map[col_name].get('数据类型', 'unknown')}"})
        # 识别删除字段
        for col_name in deletions:
            changes.append({"字段": col_name, "变更类型": "删除",
                            "旧值": f"类型:{old_map[col_name].get('数据类型', 'unknown')}",
                            "新值": "N/A"})
        # 识别类型变更
        for col_name in common:
            old_type = old_map[col_name].get("数据类型", "")
            new_type = new_map[col_name].get("数据类型", "")
            if old_type != new_type:
                changes.append({"字段": col_name, "变更类型": "类型变更",
                                "旧值": old_type, "新值": new_type})

        batch_hash = hashlib.md5(f"{table_name}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:8]

        # 写入变更记录
        diff_rows = store.rows(self.MODULE)
        for change in changes:
            new_did = max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1
            diff_rows.append({
                "id": new_did,
                "变更编号": f"META-{new_did:04d}",
                "变更对象": f"{table_name}.{change['字段']}",
                "变更类型": change["变更类型"],
                "旧值": change["旧值"],
                "新值": change["新值"],
                "变更批次": batch_hash,
                "影响评估": self._assess_impact(change["变更类型"], table_name, change["字段"]),
                "变更时间": datetime.now(timezone.utc).isoformat(),
                "status": "已确认" if change["变更类型"] != "删除" else "待审核",
                "pending": change["变更类型"] == "删除",
                "abnormal": change["变更类型"] == "删除",
            })

        # 检测到字段删除：校验血缘完整性并触发更新
        if deletions:
            self._validate_lineage_impact(table_name, list(deletions))

        return {
            "表名称": table_name,
            "旧字段数": len(old_columns),
            "新字段数": len(new_columns),
            "变更总数": len(changes),
            "新增字段": list(additions),
            "删除字段": list(deletions),
            "类型变更": [c for c in changes if c["变更类型"] == "类型变更"],
            "变更批次": batch_hash,
        }

    def _assess_impact(self, change_type: str, table_name: str, field_name: str) -> str:
        """评估变更影响范围。"""
        if change_type == "删除":
            return f"高风险：可能影响下游依赖{table_name}的表，需校验血缘完整性"
        elif change_type == "类型变更":
            return f"中风险：可能影响ETL转换逻辑和数据写入"
        else:
            return "低风险：新增字段不影响已有逻辑"

    def _validate_lineage_impact(self, table_name: str, deleted_fields: list[str]) -> None:
        """校验血缘完整性：检测删除字段是否影响下游表依赖。"""
        lineage_rows = store.rows("lineage_graph")
        affected = [
            r for r in lineage_rows
            if r.get("上游表名") == table_name and r.get("status") == "已生效"
        ]
        for lr in affected:
            lr["abnormal"] = True
            lr["status"] = "待校验"

    # ------------------------------------------------------------------
    # 历史依赖：变更影响清单回写与评审检索
    # ------------------------------------------------------------------
    def write_historical_impact(
        self,
        diff_id: int,
        target_date: str | None = None,
        range_start: str | None = None,
        range_end: str | None = None,
        max_depth: int = 4,
    ) -> dict[str, Any]:
        """历史查询结果回写元数据变更影响清单。

        以变更对象对应的表为起点做关系版本历史查询，把可直接签发、需补证、
        时间无交集三类路径连同缺证片段固化到变更记录的「历史依赖明细」字段，
        使评审结论可以随变更审查保存、复核和审计。
        """
        diff = store.find(self.MODULE, diff_id)
        if diff is None:
            return {"ok": False, "message": f"变更记录 {diff_id} 不存在或已归档"}

        table_name = str(diff.get("变更对象", "")).split(".", 1)[0]
        lineage_svc = LineageGraphService()
        detail = lineage_svc.query_relation_versions(
            table_name,
            target_date=target_date,
            range_start=range_start,
            range_end=range_end,
            direction="downstream",
            max_depth=max_depth,
            include_missing=True,
        )

        impact_summary = {
            "回写时间": datetime.now(timezone.utc).isoformat(),
            "目标日期": target_date,
            "查询区间": detail["查询区间"],
            "路径总数": detail["路径总数"],
            "可直接签发数": detail["可直接签发数"],
            "需补证数": detail["需补证数"],
            "时间无交集数": detail["时间无交集数"],
            "可直接签发下游": [p["终点表"] for p in detail["可直接签发路径"]],
            "需补证下游": [p["终点表"] for p in detail["需补证路径"]],
            "时间无交集下游": [p["终点表"] for p in detail["时间无交集路径"]],
            "缺证片段": detail["缺证片段"],
        }
        diff["历史依赖明细"] = impact_summary
        # 存在需补证对象时影响清单保持待审核，全部可证实时转为可评审
        if detail["需补证数"] > 0:
            diff["影响清单状态"] = "待补证"
            diff["pending"] = True
            diff["abnormal"] = True
        elif detail["路径总数"] > 0:
            diff["影响清单状态"] = "可评审"
        else:
            diff["影响清单状态"] = "无下游依赖"
        return {"ok": True, "message": "历史依赖明细已回写变更影响清单",
                "变更编号": diff.get("变更编号"), "影响清单状态": diff["影响清单状态"],
                "历史依赖明细": impact_summary}

    def review_verifiable_downstream(
        self,
        table_name: str,
        target_date: str | None = None,
        range_start: str | None = None,
        range_end: str | None = None,
    ) -> dict[str, Any]:
        """变更评审：用历史依赖明细检索可证实的下游，过滤时间没有交集的路径。

        返回评审视图：
        - 可直接签发：路径上每条边在查询时点都有交集且历史版本证据齐全；
        - 需补证：路径时间相交但存在缺少历史版本的边，不能在目标日期直接下发改表；
        - 已过滤：时间范围不相交或不覆盖目标日期的路径，不作为有效依赖参与签发。
        同时关联该表已有变更记录的回写结论，便于打开对应变更结论核查。
        """
        lineage_svc = LineageGraphService()
        detail = lineage_svc.query_relation_versions(
            table_name,
            target_date=target_date,
            range_start=range_start,
            range_end=range_end,
            direction="downstream",
            include_missing=True,
        )

        related_diffs = []
        for diff in store.rows(self.MODULE):
            diff_table = str(diff.get("变更对象", "")).split(".", 1)[0]
            if diff_table == table_name:
                related_diffs.append({
                    "变更编号": diff.get("变更编号"),
                    "变更对象": diff.get("变更对象"),
                    "变更类型": diff.get("变更类型"),
                    "status": diff.get("status"),
                    "影响清单状态": diff.get("影响清单状态", "未回写"),
                })

        verifiable = [
            {
                "下游表": path["终点表"],
                "路径": path["路径"],
                "路径交集窗口": path["路径交集窗口"],
                "评审结论": CONCLUSION_SIGNABLE,
                "证据关系": [e["关系编号"] for e in path["边明细"]],
            }
            for path in detail["可直接签发路径"]
        ]
        evidence_gaps = [
            {
                "下游表": path["终点表"],
                "路径": path["路径"],
                "缺证关系编号": path["缺证关系编号"],
                "评审结论": CONCLUSION_NEEDS_EVIDENCE,
            }
            for path in detail["需补证路径"]
        ]
        filtered = [
            {
                "下游表": path["终点表"],
                "路径": path["路径"],
                "评审结论": CONCLUSION_DISJOINT,
                "路径交集窗口": path["路径交集窗口"],
            }
            for path in detail["时间无交集路径"]
        ]
        return {
            "评审表": table_name,
            "目标日期": target_date,
            "查询区间": detail["查询区间"],
            "可直接签发对象": verifiable,
            "需补证对象": evidence_gaps,
            "已过滤无交集路径": filtered,
            "缺证片段": detail["缺证片段"],
            "关联变更记录": related_diffs,
            "评审说明": "仅可直接签发对象允许在目标日期下发改表；需补证对象须等待补采重试回证。",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "校验"
        snapshot["desc"] = "对比元数据schema差异并校验血缘影响"
        snapshot["artifact"] = "变更对比记录数据集"
        snapshot["artifact_desc"] = "含影响评估的变更对比记录数据集"
        return snapshot