export interface ContextPolicyOption {
  id: string
  key?: string
  name?: string
  enabled?: boolean
}

export interface ContextPolicySelection {
  context_policy_id?: string | null
  history_policy_id?: string | null
}

const presets = [
  {
    key: 'concise', label: '简洁问答',
    contextKey: 'context.concise', historyKey: 'history.recent',
    description: '适合独立问答与少量追问。优先保留当前问题和最近一轮交互，减少无关历史。',
  },
  {
    key: 'continuous', label: '持续通用型长任务',
    contextKey: 'context.standard', historyKey: 'history.summary_recent',
    description: '适合多轮执行。保留近期执行步骤和用户修正，结合目标、计划与历史摘要延续任务；摘要不可用时退回近期窗口。',
  },
  {
    key: 'evidence', label: '证据优先',
    contextKey: 'context.evidence', historyKey: 'history.recent',
    description: '适合资料核对与多来源对照。优先保留来源引用和较完整的证据摘录，控制普通聊天历史占用；入模内容仍受预算限制。',
  },
] as const

export type ContextPresetKey = typeof presets[number]['key']

export function contextSelectionChanged(current: ContextPolicySelection, saved: ContextPolicySelection) {
  return (current.context_policy_id || null) !== (saved.context_policy_id || null)
    || (current.history_policy_id || null) !== (saved.history_policy_id || null)
}

export function contextSelectionUnavailable(
  selection: ContextPolicySelection,
  contexts: readonly ContextPolicyOption[],
  histories: readonly ContextPolicyOption[],
) {
  return Boolean(
    (selection.context_policy_id && !contexts.some(item => item.id === selection.context_policy_id && item.enabled !== false))
    || (selection.history_policy_id && !histories.some(item => item.id === selection.history_policy_id && item.enabled !== false)),
  )
}

export function contextPolicyOptions(items: readonly ContextPolicyOption[], selectedId?: string | null) {
  const options = items.map(item => ({
    label: `${item.name || item.key || item.id}${item.name && item.key ? ` · ${item.key}` : ''}${item.enabled === false ? '（已停用）' : ''}`,
    value: item.id,
    disabled: item.enabled === false,
  }))
  if (selectedId && !items.some(item => item.id === selectedId)) {
    options.push({ label: `当前策略不可用（${selectedId.slice(0, 8)}）`, value: selectedId, disabled: true })
  }
  return options
}

export function resolveContextPresets(
  contexts: readonly ContextPolicyOption[],
  histories: readonly ContextPolicyOption[],
) {
  return presets.map(preset => {
    const context = contexts.find(item => item.key === preset.contextKey && item.enabled !== false)
    const history = histories.find(item => item.key === preset.historyKey && item.enabled !== false)
    const unavailableKeys = [!context && preset.contextKey, !history && preset.historyKey].filter(Boolean)
    return {
      ...preset,
      selection: context && history
        ? { context_policy_id: context.id, history_policy_id: history.id }
        : null,
      unavailableReason: unavailableKeys.length ? `策略缺失或已停用：${unavailableKeys.join('、')}` : null,
    }
  })
}

export function matchContextPreset(
  selection: ContextPolicySelection,
  contexts: readonly ContextPolicyOption[],
  histories: readonly ContextPolicyOption[],
): ContextPresetKey | 'custom' | undefined {
  if (!selection.context_policy_id && !selection.history_policy_id) return undefined
  const contextKey = contexts.find(item => item.id === selection.context_policy_id)?.key
  const historyKey = histories.find(item => item.id === selection.history_policy_id)?.key
  return presets.find(preset => preset.contextKey === contextKey && preset.historyKey === historyKey)?.key ?? 'custom'
}
