export interface ToolCatalogMetadata {
  origin?: string
  annotations?: Record<string, unknown>
}

export interface ToolBindingItem extends ToolCatalogMetadata {
  id: string
  key?: string
  name: string
  display_name_zh?: string
  enabled?: boolean
}

export const isRuntimeTool = (tool: ToolCatalogMetadata) => tool.annotations?.runtime_owned === true

export function runtimeModeLabel(tool: ToolCatalogMetadata): string {
  if (!isRuntimeTool(tool)) return ''
  const modes = tool.annotations?.runtime_modes
  if (!Array.isArray(modes)) return ''
  const labels: Record<string, string> = { chat: '对话', plan: '规划', group: '群组' }
  return [...new Set(modes.filter((mode): mode is string => typeof mode === 'string'))]
    .map(mode => labels[mode]).filter(Boolean).join('、')
}

/** These labels never add bindings or grant a capability. */
export function toolBindingOptions(items: ToolBindingItem[], selectedIds: readonly string[] = []) {
  const availableIds = new Set(items.map(item => item.id))
  const selected = new Set(selectedIds)
  return [
    ...items.map(item => ({
      value: item.id,
      label: [
        item.display_name_zh || item.name,
        item.key,
        ...(isRuntimeTool(item) ? ['会话与协作', runtimeModeLabel(item)] : []),
      ].filter(Boolean).join(' · ') + (item.enabled === false ? '（已停用）' : ''),
      // Ant Select also prevents removing a disabled selected option. Keep an
      // existing binding removable; once removed, it cannot be selected again.
      disabled: item.enabled === false && !selected.has(item.id),
    })),
    ...[...new Set(selectedIds)].filter(id => !availableIds.has(id)).map(id => ({
      value: id, label: `当前工具不可用（${id.slice(0, 8)}）`, disabled: false,
    })),
  ]
}
