<template>
  <section class="history-panel" data-testid="lineage-history-panel">
    <header class="history-head">
      <h3>关系版本 · 历史依赖明细</h3>
      <p class="page-desc">
        按生效时点筛选历史路径：只有时间范围相交的边才能组成有效路径；
        缺少历史版本的边保留为缺证片段，不会被视作无依赖。
      </p>
    </header>

    <form class="analysis-form" @submit.prevent="runQuery">
      <label>
        <span>{{ mode === 'lineage' ? '查询起点表' : '评审目标表' }}</span>
        <input
          v-model="tableName"
          data-testid="history-table"
          placeholder="如 ods_order_info"
          required
        />
      </label>
      <label>
        <span>目标日期（生效时点）</span>
        <input v-model="targetDate" data-testid="history-date" type="date" />
      </label>
      <label v-if="mode === 'lineage'">
        <span>查询区间起</span>
        <input v-model="rangeStart" type="date" />
      </label>
      <label v-if="mode === 'lineage'">
        <span>查询区间止</span>
        <input v-model="rangeEnd" type="date" />
      </label>
      <label v-if="mode === 'lineage'">
        <span>查询方向</span>
        <select v-model="direction">
          <option value="downstream">下游依赖</option>
          <option value="upstream">上游依赖</option>
        </select>
      </label>
      <button class="btn primary" data-testid="history-query" type="submit">
        {{ mode === 'lineage' ? '查询历史路径' : '检索可证实下游' }}
      </button>
      <button class="btn ghost" type="button" @click="fillBoundary('before')">交界前 2026-06-15</button>
      <button class="btn ghost" type="button" @click="fillBoundary('after')">交界后 2026-08-01</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <!-- 血缘模块：历史路径分组结果 -->
    <template v-if="mode === 'lineage' && versionResult">
      <div class="analysis-summary">
        <article class="analysis-card">
          <span class="stat-label">路径总数</span>
          <strong class="stat-value">{{ versionResult.路径总数 }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">可直接签发</span>
          <strong class="stat-value signable">{{ versionResult.可直接签发数 }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">需补证</span>
          <strong class="stat-value gap">{{ versionResult.需补证数 }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">时间无交集（已过滤）</span>
          <strong class="stat-value disjoint">{{ versionResult.时间无交集数 }}</strong>
        </article>
      </div>

      <PathGroup title="可直接签发路径" tone="signable" :paths="versionResult.可直接签发路径" />
      <PathGroup title="需补证路径（时间相交但缺少历史版本证据）" tone="gap" :paths="versionResult.需补证路径" />
      <PathGroup title="时间无交集路径（不能在目标日期下发改表）" tone="disjoint" :paths="versionResult.时间无交集路径" />

      <div class="fragment-block">
        <div class="fragment-head">
          <h4>缺证片段（与已检验路径分开管理）</h4>
          <button
            v-if="versionResult.缺证片段.length"
            class="btn"
            data-testid="retry-all"
            type="button"
            :disabled="retrying"
            @click="retryAll"
          >
            {{ retrying ? '补采中…' : '全部补采重试' }}
          </button>
        </div>
        <table v-if="versionResult.缺证片段.length" class="data-table">
          <thead>
            <tr>
              <th>关系编号</th>
              <th>上游表名</th>
              <th>下游表名</th>
              <th>版本校验状态</th>
              <th>补采次数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="fragment in versionResult.缺证片段" :key="fragment.关系编号">
              <td>{{ fragment.关系编号 }}</td>
              <td>{{ fragment.上游表名 }}</td>
              <td>{{ fragment.下游表名 }}</td>
              <td>{{ fragment.版本校验状态 }}</td>
              <td>{{ fragment.补采次数 }}</td>
              <td class="row-actions">
                <button
                  class="link"
                  type="button"
                  :data-testid="`retry-${fragment.关系编号}`"
                  :disabled="retrying"
                  @click="retryOne(fragment)"
                >
                  补采重试
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-state">当前查询无缺证片段，历史版本证据齐全。</p>
      </div>
    </template>

    <!-- 变更对比模块：评审视图 + 影响清单回写 -->
    <template v-if="mode === 'review' && reviewResult">
      <div class="analysis-summary">
        <article class="analysis-card">
          <span class="stat-label">可直接签发对象</span>
          <strong class="stat-value signable">{{ reviewResult.可直接签发对象.length }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">需补证对象</span>
          <strong class="stat-value gap">{{ reviewResult.需补证对象.length }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">已过滤无交集路径</span>
          <strong class="stat-value disjoint">{{ reviewResult.已过滤无交集路径.length }}</strong>
        </article>
      </div>

      <ReviewGroup title="可直接签发对象（允许在目标日期下发改表）" tone="signable" :items="reviewResult.可直接签发对象" />
      <ReviewGroup title="需补证对象（须等待补采回证）" tone="gap" :items="reviewResult.需补证对象" />
      <ReviewGroup title="已过滤：时间无交集" tone="disjoint" :items="reviewResult.已过滤无交集路径" />

      <div class="fragment-block">
        <h4>关联变更结论（点击回写历史依赖明细到影响清单）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>变更编号</th>
              <th>变更对象</th>
              <th>变更类型</th>
              <th>当前状态</th>
              <th>影响清单状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="diff in reviewResult.关联变更记录" :key="diff.变更编号">
              <td>{{ diff.变更编号 }}</td>
              <td>{{ diff.变更对象 }}</td>
              <td>{{ diff.变更类型 }}</td>
              <td>{{ diff.status }}</td>
              <td>{{ diff.影响清单状态 }}</td>
              <td class="row-actions">
                <button
                  class="link"
                  type="button"
                  :data-testid="`writeback-${diff.变更编号}`"
                  :disabled="writingBack"
                  @click="writeBack(diff.变更编号)"
                >
                  回写历史影响
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="page-desc">{{ reviewResult.评审说明 }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, h, ref } from 'vue'

import {
  queryRelationVersions,
  retryMissingVersions,
  reviewVerifiableDownstream,
  writeHistoricalImpact,
  type RelationVersionResult,
  type ReviewResult,
  type VersionPath,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{
  moduleKey: string
  rows: EntryRow[]
  onWrittenBack: () => Promise<void>
}>()

const mode = computed<'lineage' | 'review'>(() =>
  props.moduleKey === 'metadata_diff' ? 'review' : 'lineage',
)

const tableName = ref('ods_order_info')
const targetDate = ref('2026-05-01')
const rangeStart = ref('')
const rangeEnd = ref('')
const direction = ref<'downstream' | 'upstream'>('downstream')
const errorMessage = ref('')
const retrying = ref(false)
const writingBack = ref(false)
const versionResult = ref<RelationVersionResult | null>(null)
const reviewResult = ref<ReviewResult | null>(null)

function fillBoundary(point: 'before' | 'after') {
  targetDate.value = point === 'before' ? '2026-06-15' : '2026-08-01'
}

async function runQuery() {
  errorMessage.value = ''
  try {
    if (mode.value === 'lineage') {
      versionResult.value = await queryRelationVersions({
        table: tableName.value,
        targetDate: targetDate.value || undefined,
        rangeStart: rangeStart.value || undefined,
        rangeEnd: rangeEnd.value || undefined,
        direction: direction.value,
      })
    } else {
      reviewResult.value = await reviewVerifiableDownstream({
        table: tableName.value,
        targetDate: targetDate.value || undefined,
      })
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史依赖查询失败'
  }
}

async function retryOne(fragment: { 关系编号: string; 上游表名: string; 下游表名: string }) {
  retrying.value = true
  errorMessage.value = ''
  try {
    const result = await retryMissingVersions({
      table: fragment.下游表名,
      relationNo: fragment.关系编号,
    })
    if (!result.ok) throw new Error(result.message)
    await runQuery()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补采重试失败'
  } finally {
    retrying.value = false
  }
}

async function retryAll() {
  retrying.value = true
  errorMessage.value = ''
  try {
    const fragments = versionResult.value?.缺证片段 ?? []
    for (const fragment of fragments) {
      // 串行补采，保证每条缺证边独立留痕
      await retryMissingVersions({ table: fragment.下游表名, relationNo: fragment.关系编号 })
    }
    await runQuery()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量补采失败'
  } finally {
    retrying.value = false
  }
}

async function writeBack(diffNo: string) {
  writingBack.value = true
  errorMessage.value = ''
  try {
    const target = props.rows.find((row) => row.变更编号 === diffNo)
    if (!target) throw new Error(`未找到变更记录 ${diffNo}`)
    const result = await writeHistoricalImpact({
      diffId: Number(target.id),
      targetDate: targetDate.value || undefined,
    })
    if (!result.ok) throw new Error(result.message)
    await props.onWrittenBack()
    await runQuery()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '影响清单回写失败'
  } finally {
    writingBack.value = false
  }
}

// 轻量内联子组件：路径分组表格（血缘模块）
const PathGroup = (groupProps: { title: string; tone: string; paths: VersionPath[] }) =>
  h('div', { class: `fragment-block tone-${groupProps.tone}` }, [
    h('h4', null, `${groupProps.title}（${groupProps.paths.length}）`),
    groupProps.paths.length
      ? h('table', { class: 'data-table' }, [
          h('thead', null, h('tr', null, [
            h('th', null, '终点表'),
            h('th', null, '路径'),
            h('th', null, '交集窗口'),
            h('th', null, '缺证关系'),
          ])),
          h('tbody', null, groupProps.paths.map((path) =>
            h('tr', null, [
              h('td', null, path.终点表),
              h('td', null, path.路径.join(' → ')),
              h('td', null, `${path.路径交集窗口[0] ?? '开放'} ~ ${path.路径交集窗口[1] ?? '至今'}`),
              h('td', null, path.缺证关系编号.length ? path.缺证关系编号.join('、') : '—'),
            ]),
          )),
        ])
      : h('p', { class: 'empty-state' }, '暂无数据'),
  ])
PathGroup.props = ['title', 'tone', 'paths']

// 轻量内联子组件：评审分组（变更对比模块）
const ReviewGroup = (groupProps: { title: string; tone: string; items: Record<string, unknown>[] }) =>
  h('div', { class: `fragment-block tone-${groupProps.tone}` }, [
    h('h4', null, `${groupProps.title}（${groupProps.items.length}）`),
    groupProps.items.length
      ? h('table', { class: 'data-table' }, [
          h('thead', null, h('tr', null, [
            h('th', null, '下游表'),
            h('th', null, '路径'),
            h('th', null, '评审结论'),
          ])),
          h('tbody', null, groupProps.items.map((item) =>
            h('tr', null, [
              h('td', null, String(item.下游表 ?? '')),
              h('td', null, Array.isArray(item.路径) ? item.路径.join(' → ') : '—'),
              h('td', null, String(item.评审结论 ?? '')),
            ]),
          )),
        ])
      : h('p', { class: 'empty-state' }, '暂无数据'),
  ])
ReviewGroup.props = ['title', 'tone', 'items']
</script>

<style scoped>
.history-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin: 16px 0;
}
.history-head h3 {
  margin: 0 0 4px;
}
.fragment-block {
  margin-top: 14px;
}
.fragment-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.tone-signable h4 {
  color: #15803d;
}
.tone-gap h4 {
  color: #b45309;
}
.tone-disjoint h4 {
  color: #b42318;
}
.signable {
  color: #15803d;
}
.gap {
  color: #b45309;
}
.disjoint {
  color: #b42318;
}
</style>
