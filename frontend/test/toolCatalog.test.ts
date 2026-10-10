import assert from 'node:assert/strict'
import test from 'node:test'

import { isRuntimeTool, runtimeModeLabel, toolBindingOptions } from '../src/lib/toolCatalog.ts'

const runtime = {
  id: 'runtime-id', key: 'ask_user', name: 'Ask user', display_name_zh: '询问用户',
  origin: 'builtin', annotations: { runtime_owned: true, runtime_modes: ['chat', 'plan', 'group'], eager: true },
}
const fileTool = { id: 'file-id', key: 'read_file', name: 'Read file', origin: 'builtin' }

test('runtime tools remain in the same binding choices as other built-in and MCP tools', () => {
  const mcp = { id: 'mcp-id', key: 'remote_lookup', name: 'Remote lookup', origin: 'mcp' }
  const choices = toolBindingOptions([fileTool, runtime, mcp])
  assert.deepEqual(choices.map(item => item.value), ['file-id', 'runtime-id', 'mcp-id'])
  assert.equal(choices[1].label, '询问用户 · ask_user · 会话与协作 · 对话、规划、群组')
  assert.ok(choices.every(item => !item.disabled))
})

test('runtime labeling requires explicit metadata and never relies on the tool name or origin', () => {
  assert.equal(isRuntimeTool(runtime), true)
  assert.equal(isRuntimeTool({ ...runtime, annotations: {} }), false)
  assert.equal(isRuntimeTool({ ...runtime, annotations: { runtime_owned: 'true' } }), false)
  assert.equal(isRuntimeTool(fileTool), false)
  assert.equal(toolBindingOptions([{ ...runtime, annotations: {} }])[0].label, '询问用户 · ask_user')
})

test('mode restrictions are shown without hiding group-only choices from agent assembly', () => {
  const groupTool = { ...runtime, key: 'send_group_message', annotations: { runtime_owned: true, runtime_modes: ['group'] } }
  assert.equal(runtimeModeLabel(groupTool), '群组')
  assert.equal(toolBindingOptions([groupTool]).length, 1)
  assert.equal(toolBindingOptions([groupTool])[0].disabled, false)
  assert.equal(runtimeModeLabel({ annotations: { runtime_owned: true, runtime_modes: ['chat', 'chat', null, 'unknown'] } }), '对话')
  assert.equal(runtimeModeLabel({ annotations: { runtime_owned: true, runtime_modes: 'group' } }), '')
})

test('a previously selected disabled or unavailable tool remains visible without replacing its binding', () => {
  const selectedIds = Object.freeze(['runtime-id', 'missing-tool-id'])
  const choices = toolBindingOptions([{ ...runtime, enabled: false }, fileTool], selectedIds)
  assert.equal(choices[0].disabled, false)
  assert.match(choices[0].label, /已停用/)
  assert.deepEqual(choices.at(-1), {
    value: 'missing-tool-id', label: '当前工具不可用（missing-）', disabled: false,
  })
  assert.deepEqual(selectedIds, ['runtime-id', 'missing-tool-id'])
  const afterRemoval = toolBindingOptions([{ ...runtime, enabled: false }, fileTool], [])
  assert.equal(afterRemoval[0].disabled, true)
  assert.ok(!afterRemoval.some(item => item.value === 'missing-tool-id'))
})

test('showing newly registered tools does not select them or modify the existing draft', () => {
  const selectedIds = Object.freeze(['file-id'])
  const draft = Object.freeze({ tool_ids: selectedIds, bound_agent_ids: Object.freeze([]) })
  toolBindingOptions([fileTool, runtime], draft.tool_ids)
  assert.deepEqual(draft, { tool_ids: ['file-id'], bound_agent_ids: [] })
  assert.deepEqual(toolBindingOptions([], []), [])
})
