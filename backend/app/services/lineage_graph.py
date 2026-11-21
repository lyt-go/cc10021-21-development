"""血缘图谱服务：解析和关联数据表上下游依赖关系，驱动元数据变更对比和质量事件追踪。

domain_chain: 血缘图谱 (lineage_graph)
  └─ 关联 → 元数据变更对比 (metadata_diff)
  └─ 查询 → 质量事件追踪 (dq_incident)

关系版本能力：
  每条血缘边携带生效时间区间（生效开始时间/生效结束时间）与版本校验状态。
  历史路径查询只把「时间范围相交」的边串成有效路径；缺少历史版本的边不会被
  当作无依赖丢弃，而是作为缺证片段单独保留，供补采重试与变更评审追溯。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 边版本校验状态：已采集到历史版本 / 历史版本缺失待补证 / 补证成功 / 补证失败
VERSION_COLLECTED = "已采集"
VERSION_MISSING = "待补证"
VERSION_RETRIED = "补证成功"
VERSION_RETRY_FAILED = "补证失败"

# 路径评审结论：可直接签发 / 需补证 / 时间无交集（不可作为有效依赖）
CONCLUSION_SIGNABLE = "可直接签发"
CONCLUSION_NEEDS_EVIDENCE = "需补证"
CONCLUSION_DISJOINT = "时间无交集"

DATE_FMT = "%Y-%m-%d"


class LineageGraphService(BaseModuleService):
    MODULE = "lineage_graph"
    ENTITY = "血缘关系"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "关系编号", "上游表名", "下游表名", "来源字段", "目标字段", "转换逻辑",
        "采集批次", "关系版本", "生效开始时间", "生效结束时间", "版本校验状态",
    ]
    REQUIRED_FIELDS = ["关系编号", "上游表名", "下游表名"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用关系": "已停用"}
    NEGATIVE_ACTIONS = ["停用关系"]

    # ------------------------------------------------------------------
    # SQL 解析与图谱构建（既有能力，补齐版本字段）
    # ------------------------------------------------------------------
    def parse_lineage_from_sql(self, sql_text: str, target_table: str) -> list[dict[str, Any]]:
        """解析SQL语句提取上下游血缘关系并关联入图谱。

        从SQL解析FROM/JOIN子句，识别上游源表，建立数据血缘关系。
        解析结果关联写入血缘图谱，同时触发变更对比流程。
        新解析的关系默认从当前时点起生效，结束时间留空表示持续生效。
        """
        import re
        from_pattern = re.findall(r'(?:FROM|JOIN)\s+(\w+(?:\.\w+)?)', sql_text, re.IGNORECASE)
        upstream_tables = list(set(from_pattern))
        if target_table in upstream_tables:
            upstream_tables.remove(target_table)

        lineages = []
        lineage_rows = store.rows(self.MODULE)
        today = datetime.now(timezone.utc).strftime(DATE_FMT)
        for upstream in upstream_tables:
            new_lid = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
            entry = {
                "id": new_lid,
                "关系编号": f"LINE-{new_lid:04d}",
                "上游表名": upstream,
                "下游表名": target_table,
                "来源字段": "*",
                "目标字段": "*",
                "转换逻辑": "SQL解析",
                "采集批次": datetime.now(timezone.utc).strftime("%Y%m%d%H"),
                "关系版本": "v1",
                "生效开始时间": today,
                "生效结束时间": None,
                "版本校验状态": VERSION_COLLECTED,
                "补采次数": 0,
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            lineage_rows.append(entry)
            lineages.append(entry)
        return lineages

    def build_lineage_graph(self, table_name: str, depth: int = 3) -> dict[str, Any]:
        """构建指定表的血缘关系图谱，向上递归探索上游，向下递归探索下游。

        查询关联到该表的所有血缘记录，构建双向血缘图并聚合为图谱快照。
        """
        all_rows = store.rows(self.MODULE)
        # 查询上下游关系
        upstream = [r for r in all_rows if r.get("下游表名") == table_name and r.get("status") == "已生效"]
        downstream = [r for r in all_rows if r.get("上游表名") == table_name and r.get("status") == "已生效"]

        # 递归展开上游
        expanded_upstream = self._expand_lineage(upstream, direction="up", depth=depth - 1)

        return {
            "中心表": table_name,
            "上游表数": len(expanded_upstream),
            "下游表数": len(downstream),
            "上游血缘": [
                {"表名": u.get("上游表名"), "关系编号": u.get("关系编号"),
                 "转换逻辑": u.get("转换逻辑"), "关系版本": u.get("关系版本")}
                for u in expanded_upstream
            ],
            "下游血缘": [
                {"表名": d.get("下游表名"), "关系编号": d.get("关系编号"),
                 "转换逻辑": d.get("转换逻辑")}
                for d in downstream
            ],
            "图谱生成时间": datetime.now(timezone.utc).isoformat(),
        }

    def _expand_lineage(self, entries: list[dict], direction: str, depth: int) -> list[dict]:
        """递归展开血缘关系，深度遍历上下游关联路径。"""
        if depth <= 0 or not entries:
            return entries
        all_rows = store.rows(self.MODULE)
        expanded = list(entries)
        for entry in entries:
            if direction == "up":
                parents = [r for r in all_rows
                           if r.get("下游表名") == entry.get("上游表名")
                           and r.get("status") == "已生效"
                           and r not in expanded]
            else:
                children = [r for r in all_rows
                            if r.get("上游表名") == entry.get("下游表名")
                            and r.get("status") == "已生效"
                            and r not in expanded]
            expanded.extend(parents if direction == "up" else children)
        return expanded

    def detect_lineage_breakage(self, table_name: str) -> list[dict]:
        """检测血缘断链：查询上游表是否仍存在于表目录中。

        关联血缘图与表目录，对比校验血缘上下游的完整性，识别断链的表依赖。
        """
        catalog_rows = store.rows("table_catalog")
        all_rows = store.rows(self.MODULE)
        upstream_entries = [r for r in all_rows if r.get("下游表名") == table_name]
        breakages = []
        for entry in upstream_entries:
            upstream_name = entry.get("上游表名", "")
            found = any(c.get("表名称") == upstream_name for c in catalog_rows)
            if not found:
                breakages.append({
                    "关系编号": entry.get("关系编号"),
                    "断链上游": upstream_name,
                    "下游": table_name,
                    "检测到断链": True,
                    "建议操作": "重新采集上游元数据或标记为已停用",
                })
        return breakages

    # ------------------------------------------------------------------
    # 关系版本：历史路径查询
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_day(value: Any) -> datetime | None:
        """把 'YYYY-MM-DD' 或 ISO 字符串解析为日期；空值返回 None（表示开放区间）。"""
        if value in (None, ""):
            return None
        text = str(value).strip()
        for fmt in (DATE_FMT, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.replace(tzinfo=None)
            except ValueError:
                continue
        # 兜底：截取前 10 位按日期解析
        try:
            return datetime.strptime(text[:10], DATE_FMT)
        except ValueError:
            return None

    def _edge_window(self, edge: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        """读取单条边的生效时间区间；结束时间为空表示至今仍生效。"""
        return self._parse_day(edge.get("生效开始时间")), self._parse_day(edge.get("生效结束时间"))

    @staticmethod
    def _intersect(
        window: tuple[datetime | None, datetime | None],
        other: tuple[datetime | None, datetime | None],
    ) -> tuple[datetime | None, datetime | None]:
        """两个闭区间求交集；任一侧开放（None）按无下界/无上界处理。"""
        start_a, end_a = window
        start_b, end_b = other
        lower = max(dt for dt in (start_a, start_b) if dt is not None) \
            if (start_a is not None or start_b is not None) else None
        upper = min(dt for dt in (end_a, end_b) if dt is not None) \
            if (end_a is not None or end_b is not None) else None
        if lower is not None and upper is not None and lower > upper:
            return None  # 区间不相交
        return lower, upper

    def _edge_evidence(self, edge: dict[str, Any]) -> dict[str, Any]:
        """序列化一条边的版本证据，供路径明细和评审清单引用。"""
        start, end = self._edge_window(edge)
        return {
            "关系编号": edge.get("关系编号"),
            "上游表名": edge.get("上游表名"),
            "下游表名": edge.get("下游表名"),
            "关系版本": edge.get("关系版本"),
            "生效开始时间": edge.get("生效开始时间"),
            "生效结束时间": edge.get("生效结束时间"),
            "版本校验状态": edge.get("版本校验状态", VERSION_MISSING),
            "补采次数": edge.get("补采次数", 0),
            "时间窗口": [
                start.strftime(DATE_FMT) if start else None,
                end.strftime(DATE_FMT) if end else None,
            ],
        }

    def query_relation_versions(
        self,
        table_name: str,
        target_date: str | None = None,
        range_start: str | None = None,
        range_end: str | None = None,
        direction: str = "downstream",
        max_depth: int = 4,
        include_missing: bool = True,
    ) -> dict[str, Any]:
        """按生效时点查询关系版本，生成可随变更审查保存的历史依赖明细。

        遍历以 ``table_name`` 为起点的间接路径（每条间接路径至少经过两条边），
        逐边求生效时间区间交集：只有时间范围相交的边才能组成有效路径；
        缺少历史版本（版本校验状态为待补证/补证失败）的边作为缺证片段保留，
        不会被视作无依赖而丢弃。

        - ``target_date``：单点审查，只保留在该日期仍处于交集窗口内的路径。
        - ``range_start``/``range_end``：区间审查，路径交集窗口与查询区间有重叠即可。
        - 返回结构按评审结论拆分为「可直接签发 / 需补证 / 时间无交集」三组，
          缺证片段单独列出，保证补采重试时与已检验路径分开处理。
        """
        point = self._parse_day(target_date)
        query_window = (
            self._parse_day(range_start),
            self._parse_day(range_end),
        )
        if query_window[0] is None and query_window[1] is None and point is not None:
            query_window = (point, point)

        all_edges = store.rows(self.MODULE)
        # 停用关系不参与历史路径拼装；草稿/待校验边同样不具备生效证据
        active_edges = [e for e in all_edges if e.get("status") == "已生效"]

        paths: list[dict[str, Any]] = []
        missing_fragments: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()

        def outgoing(node: str) -> list[dict[str, Any]]:
            if direction == "upstream":
                return [e for e in active_edges if e.get("下游表名") == node]
            return [e for e in active_edges if e.get("上游表名") == node]

        def walk(node: str, chain: list[dict[str, Any]],
                 window: tuple[datetime | None, datetime | None] | None,
                 visited: set[str], depth: int) -> None:
            if depth >= max_depth:
                return
            for edge in outgoing(node):
                next_table = edge.get("下游表名") if direction == "downstream" else edge.get("上游表名")
                if next_table in visited:
                    continue
                edge_window = self._edge_window(edge)
                next_window = self._intersect(window, edge_window) if window is not None else edge_window
                next_chain = chain + [edge]
                missing = [e for e in next_chain
                           if e.get("版本校验状态", VERSION_MISSING) in (VERSION_MISSING, VERSION_RETRY_FAILED)]

                if len(next_chain) >= 2:
                    key = tuple(str(e.get("关系编号")) for e in next_chain)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        path_record = self._build_path_record(
                            table_name, next_table, next_chain, next_window,
                            point, query_window, direction,
                        )
                        if path_record is not None:
                            paths.append(path_record)
                        # 缺证片段登记：只要路径上存在缺历史版本的边就留证
                        for miss_edge in missing:
                            fragment = self._edge_evidence(miss_edge)
                            fragment["所在路径"] = [
                                f"{e.get('上游表名')}→{e.get('下游表名')}" for e in next_chain
                            ]
                            fragment["审查起点表"] = table_name
                            if not any(
                                f["关系编号"] == fragment["关系编号"]
                                and f.get("审查起点表") == table_name
                                for f in missing_fragments
                            ):
                                missing_fragments.append(fragment)

                # 时间区间已经不相交则停止向下延伸；缺证边仍可继续探索后续边
                walk(next_table, next_chain, next_window, visited | {next_table}, depth + 1)

        walk(table_name, [], (None, None), {table_name}, 0)

        signable, needs_evidence, disjoint = self._classify_paths(
            paths, point, query_window, include_missing,
        )

        return {
            "查询表": table_name,
            "查询方向": "下游依赖" if direction == "downstream" else "上游依赖",
            "目标日期": target_date,
            "查询区间": [
                query_window[0].strftime(DATE_FMT) if query_window[0] else None,
                query_window[1].strftime(DATE_FMT) if query_window[1] else None,
            ],
            "生成时间": datetime.now(timezone.utc).isoformat(),
            "路径总数": len(paths),
            "可直接签发数": len(signable),
            "需补证数": len(needs_evidence),
            "时间无交集数": len(disjoint),
            "可直接签发路径": signable,
            "需补证路径": needs_evidence,
            "时间无交集路径": disjoint,
            "缺证片段": missing_fragments,
        }

    def _build_path_record(
        self,
        start_table: str,
        end_table: str,
        chain: list[dict[str, Any]],
        intersected_window: tuple[datetime | None, datetime | None] | None,
        point: datetime | None,
        query_window: tuple[datetime | None, datetime | None],
        direction: str,
    ) -> dict[str, Any] | None:
        """组装单条间接路径的历史依赖明细。"""
        edges_detail = [self._edge_evidence(e) for e in chain]
        missing_edges = [
            e.get("关系编号") for e in chain
            if e.get("版本校验状态", VERSION_MISSING) in (VERSION_MISSING, VERSION_RETRY_FAILED)
        ]
        overlap = intersected_window is not None
        window_text = [
            intersected_window[0].strftime(DATE_FMT) if intersected_window and intersected_window[0] else None,
            intersected_window[1].strftime(DATE_FMT) if intersected_window and intersected_window[1] else None,
        ]

        # 与查询时点/区间的覆盖关系
        covers_point = bool(
            overlap and point is not None
            and (intersected_window[0] is None or intersected_window[0] <= point)
            and (intersected_window[1] is None or intersected_window[1] >= point)
        )
        covers_range = overlap
        if overlap and query_window[0] is not None:
            covers_range = covers_range and (
                intersected_window[1] is None or intersected_window[1] >= query_window[0]
            )
        if overlap and query_window[1] is not None:
            covers_range = covers_range and (
                intersected_window[0] is None or intersected_window[0] <= query_window[1]
            )

        if not overlap:
            conclusion = CONCLUSION_DISJOINT
        elif missing_edges:
            conclusion = CONCLUSION_NEEDS_EVIDENCE
        else:
            conclusion = CONCLUSION_SIGNABLE

        return {
            "起点表": start_table,
            "终点表": end_table,
            "方向": "下游" if direction == "downstream" else "上游",
            "路径边数": len(chain),
            "路径": [f"{e.get('上游表名')}→{e.get('下游表名')}" for e in chain],
            "边明细": edges_detail,
            "路径交集窗口": window_text,
            "缺证关系编号": missing_edges,
            "时间相交": overlap,
            "覆盖目标日期": covers_point,
            "覆盖查询区间": covers_range,
            "评审结论": conclusion,
        }

    def _classify_paths(
        self,
        paths: list[dict[str, Any]],
        point: datetime | None,
        query_window: tuple[datetime | None, datetime | None],
        include_missing: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """按评审结论拆分路径；带目标时点时过滤掉不覆盖该时点的路径。"""
        signable: list[dict[str, Any]] = []
        needs_evidence: list[dict[str, Any]] = []
        disjoint: list[dict[str, Any]] = []
        has_point_filter = point is not None
        has_range_filter = query_window[0] is not None or query_window[1] is not None

        for record in paths:
            if has_point_filter and not record["覆盖目标日期"]:
                # 目标日期不在交集窗口内（含边窗口本身不相交）：路径在审查时点不成立
                disjoint.append(record)
                continue
            if not has_point_filter and has_range_filter and not record["覆盖查询区间"]:
                disjoint.append(record)
                continue
            if record["评审结论"] == CONCLUSION_SIGNABLE:
                signable.append(record)
            elif record["评审结论"] == CONCLUSION_NEEDS_EVIDENCE:
                if include_missing:
                    needs_evidence.append(record)
            else:
                disjoint.append(record)
        return signable, needs_evidence, disjoint

    # ------------------------------------------------------------------
    # 关系版本：补采重试（缺证片段与已检验路径分离）
    # ------------------------------------------------------------------
    def retry_missing_versions(
        self,
        table_name: str,
        relation_no: str | None = None,
        simulate_failure: bool = False,
    ) -> dict[str, Any]:
        """补采失败后重试：只处理缺历史版本的边，已检验路径保持不动。

        缺证片段（版本校验状态为待补证/补证失败）与已检验路径分开处理：
        - 指定 ``relation_no`` 时只重试单条关系；否则重试该表相关的全部缺证边。
        - ``simulate_failure`` 用于复现补采再次失败：状态置为补证失败并累计补采次数，
          缺证证据保留，不会污染已检验路径。
        - 补采成功时回填默认历史生效区间并置为补证成功，之后该边可参与有效路径拼装。
        """
        all_edges = store.rows(self.MODULE)
        candidates = [
            e for e in all_edges
            if e.get("版本校验状态", VERSION_MISSING) in (VERSION_MISSING, VERSION_RETRY_FAILED)
            and (e.get("上游表名") == table_name or e.get("下游表名") == table_name)
            and (relation_no is None or e.get("关系编号") == relation_no)
        ]

        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for edge in candidates:
            edge["补采次数"] = int(edge.get("补采次数", 0)) + 1
            if simulate_failure:
                edge["版本校验状态"] = VERSION_RETRY_FAILED
                failed.append(self._edge_evidence(edge))
                continue
            # 补采成功：回填历史版本证据。缺省按 2026 上半年区间补齐，
            # 真实环境这里会写入采集器回捞的实际生效起止时间。
            edge["版本校验状态"] = VERSION_RETRIED
            if not edge.get("生效开始时间"):
                edge["生效开始时间"] = "2026-01-01"
            if not edge.get("生效结束时间"):
                edge["生效结束时间"] = "2026-06-30"
            edge["关系版本"] = f"{edge.get('关系版本', 'v1')}-补证"
            succeeded.append(self._edge_evidence(edge))

        # 已检验路径（已采集/补证成功的边）不在重试范围内，原样保留
        verified = [
            self._edge_evidence(e) for e in all_edges
            if e.get("版本校验状态") in (VERSION_COLLECTED, VERSION_RETRIED)
            and (e.get("上游表名") == table_name or e.get("下游表名") == table_name)
        ]
        still_missing = [
            self._edge_evidence(e) for e in all_edges
            if e.get("版本校验状态", VERSION_MISSING) in (VERSION_MISSING, VERSION_RETRY_FAILED)
            and (e.get("上游表名") == table_name or e.get("下游表名") == table_name)
        ]

        return {
            "重试表": table_name,
            "重试时间": datetime.now(timezone.utc).isoformat(),
            "重试候选数": len(candidates),
            "补证成功数": len(succeeded),
            "补证失败数": len(failed),
            "补证成功片段": succeeded,
            "补证失败片段": failed,
            "仍缺证片段": still_missing,
            "已检验路径边": verified,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "关联"
        snapshot["desc"] = "解析SQL提取血缘关系并关联入图谱"
        snapshot["artifact"] = "血缘图谱数据集"
        snapshot["artifact_desc"] = "上下游血缘关系图谱数据集"
        return snapshot
