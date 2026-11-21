import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 血缘关系版本：历史依赖明细查询、补采重试、变更影响回写与评审
// ---------------------------------------------------------------------------

export type VersionPath = {
  起点表: string
  终点表: string
  方向: string
  路径边数: number
  路径: string[]
  边明细: Record<string, string | number | null>[]
  路径交集窗口: (string | null)[]
  缺证关系编号: string[]
  时间相交: boolean
  覆盖目标日期: boolean
  覆盖查询区间: boolean
  评审结论: string
}

export type MissingFragment = {
  关系编号: string
  上游表名: string
  下游表名: string
  关系版本: string
  生效开始时间: string | null
  生效结束时间: string | null
  版本校验状态: string
  补采次数: number
  时间窗口: (string | null)[]
  所在路径?: string[]
  审查起点表?: string
}

export type RelationVersionResult = {
  查询表: string
  查询方向: string
  目标日期: string | null
  查询区间: (string | null)[]
  生成时间: string
  路径总数: number
  可直接签发数: number
  需补证数: number
  时间无交集数: number
  可直接签发路径: VersionPath[]
  需补证路径: VersionPath[]
  时间无交集路径: VersionPath[]
  缺证片段: MissingFragment[]
}

export type ReviewResult = {
  评审表: string
  目标日期: string | null
  查询区间: (string | null)[]
  可直接签发对象: Record<string, unknown>[]
  需补证对象: Record<string, unknown>[]
  已过滤无交集路径: Record<string, unknown>[]
  缺证片段: MissingFragment[]
  关联变更记录: {
    变更编号: string
    变更对象: string
    变更类型: string
    status: string
    影响清单状态: string
  }[]
  评审说明: string
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error('血缘关系版本查询失败')
  return (await response.json()) as T
}

export async function queryRelationVersions(params: {
  table: string
  targetDate?: string
  rangeStart?: string
  rangeEnd?: string
  direction?: 'downstream' | 'upstream'
}): Promise<RelationVersionResult> {
  const query = new URLSearchParams({ table: params.table })
  if (params.targetDate) query.set('target_date', params.targetDate)
  if (params.rangeStart) query.set('range_start', params.rangeStart)
  if (params.rangeEnd) query.set('range_end', params.rangeEnd)
  if (params.direction) query.set('direction', params.direction)
  return getJson<RelationVersionResult>(`/api/lineage/versions?${query.toString()}`)
}

export async function reviewVerifiableDownstream(params: {
  table: string
  targetDate?: string
  rangeStart?: string
  rangeEnd?: string
}): Promise<ReviewResult> {
  const query = new URLSearchParams({ table: params.table })
  if (params.targetDate) query.set('target_date', params.targetDate)
  if (params.rangeStart) query.set('range_start', params.rangeStart)
  if (params.rangeEnd) query.set('range_end', params.rangeEnd)
  return getJson<ReviewResult>(`/api/lineage/review?${query.toString()}`)
}

export async function retryMissingVersions(params: {
  table: string
  relationNo?: string
  simulateFailure?: boolean
}): Promise<ActionResult> {
  const query = new URLSearchParams({ table: params.table })
  if (params.relationNo) query.set('relation_no', params.relationNo)
  if (params.simulateFailure) query.set('simulate_failure', 'true')
  const response = await request(`/api/lineage/retry-missing?${query.toString()}`, { method: 'POST' })
  return await response.json()
}

export async function writeHistoricalImpact(params: {
  diffId: number
  targetDate?: string
  rangeStart?: string
  rangeEnd?: string
}): Promise<ActionResult> {
  const query = new URLSearchParams()
  if (params.targetDate) query.set('target_date', params.targetDate)
  if (params.rangeStart) query.set('range_start', params.rangeStart)
  if (params.rangeEnd) query.set('range_end', params.rangeEnd)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  const response = await request(`/api/lineage/diff/${params.diffId}/impact${suffix}`, { method: 'POST' })
  return await response.json()
}
