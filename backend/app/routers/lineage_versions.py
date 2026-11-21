"""血缘关系版本与历史依赖评审路由。

在通用资源路由之外，提供按生效时点筛选历史路径、补采重试、
变更影响清单回写和评审检索的专用接口。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services.lineage_graph import LineageGraphService
from app.services.metadata_diff import MetadataDiffService
from app.store import store

router = APIRouter(prefix="/api/lineage", tags=["血缘关系版本"])


@router.get("/versions")
def query_relation_versions(
    table: str = Query(..., description="查询起点表名"),
    target_date: str | None = Query(default=None, description="生效时点 YYYY-MM-DD"),
    range_start: str | None = Query(default=None, description="查询区间开始 YYYY-MM-DD"),
    range_end: str | None = Query(default=None, description="查询区间结束 YYYY-MM-DD"),
    direction: str = Query(default="downstream", pattern="^(downstream|upstream)$"),
    max_depth: int = Query(default=4, ge=1, le=8),
    include_missing: bool = Query(default=True, description="是否保留缺证路径"),
) -> dict:
    """按生效时点查询关系版本，返回历史依赖明细（含缺证片段与评审结论分组）。"""
    service = LineageGraphService()
    return service.query_relation_versions(
        table,
        target_date=target_date,
        range_start=range_start,
        range_end=range_end,
        direction=direction,
        max_depth=max_depth,
        include_missing=include_missing,
    )


@router.post("/retry-missing", response_model=ActionResult)
def retry_missing_versions(
    table: str = Query(...),
    relation_no: str | None = Query(default=None),
    simulate_failure: bool = Query(default=False),
) -> ActionResult:
    """补采失败后重试：缺证片段与已检验路径分开处理。"""
    service = LineageGraphService()
    result = service.retry_missing_versions(
        table, relation_no=relation_no, simulate_failure=simulate_failure)
    return ActionResult(
        ok=result["补证失败数"] == 0,
        message=(
            f"补证成功 {result['补证成功数']} 条，失败 {result['补证失败数']} 条，"
            f"仍缺证 {len(result['仍缺证片段'])} 条"
        ),
        entry=result,
    )


@router.post("/diff/{diff_id}/impact", response_model=ActionResult)
def write_historical_impact(
    diff_id: int,
    target_date: str | None = Query(default=None),
    range_start: str | None = Query(default=None),
    range_end: str | None = Query(default=None),
) -> ActionResult:
    """历史查询结果回写元数据变更影响清单。"""
    if store.find("metadata_diff", diff_id) is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {diff_id} 不存在或已归档")
    service = MetadataDiffService()
    result = service.write_historical_impact(
        diff_id, target_date=target_date,
        range_start=range_start, range_end=range_end)
    return ActionResult(ok=bool(result.get("ok")), message=result.get("message", ""),
                        entry=result)


@router.get("/review")
def review_verifiable_downstream(
    table: str = Query(...),
    target_date: str | None = Query(default=None),
    range_start: str | None = Query(default=None),
    range_end: str | None = Query(default=None),
) -> dict:
    """变更评审：检索可证实下游，过滤时间无交集路径，核查可签发与需补证对象。"""
    service = MetadataDiffService()
    return service.review_verifiable_downstream(
        table, target_date=target_date,
        range_start=range_start, range_end=range_end)
