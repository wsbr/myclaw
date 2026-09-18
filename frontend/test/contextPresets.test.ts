import assert from 'node:assert/strict'
import test from 'node:test'

import { contextPolicyOptions, contextSelectionChanged, contextSelectionUnavailable, matchContextPreset, resolveContextPresets } from '../src/pages/Agents/Config/contextPresets.ts'

const contexts = [
  { id: 'ctx-short', key: 'context.concise' },
  { id: 'ctx-normal', key: 'context.standard' },
  { id: 'ctx-evidence', key: 'context.evidence' },
]
const histories = [
  { id: 'hist-window', key: 'history.recent' },
  { id: 'hist-summary', key: 'history.summary_recent' },
]

test('resolves the three supported combinations by stable policy key, independent of option order', () => {
  const resolved = resolveContextPresets([...contexts].reverse(), [...histories].reverse())
  assert.deepEqual(resolved.map(item => item.selection), [
    { context_policy_id: 'ctx-short', history_policy_id: 'hist-window' },
    { context_policy_id: 'ctx-normal', history_policy_id: 'hist-summary' },
    { context_policy_id: 'ctx-evidence', history_policy_id: 'hist-window' },
  ])
  assert.ok(resolved.every(item => item.unavailableReason === null))
  for (const preset of resolved) {
    assert.equal(matchContextPreset(preset.selection!, contexts, histories), preset.key)
  }
})

test('a missing history policy disables only its dependent preset and does not substitute another history', () => {
  const resolved = resolveContextPresets(contexts, histories.slice(0, 1))
  assert.ok(resolved[0].selection)
  assert.equal(resolved[1].selection, null)
  assert.match(resolved[1].unavailableReason!, /history\.summary_recent/)
  assert.ok(resolved[2].selection)
})

test('missing keys and disabled policies cannot be selected through a preset', () => {
  const resolved = resolveContextPresets([
    { id: 'ctx-short' },
    { id: 'ctx-normal', key: 'context.standard', enabled: false },
    contexts[2],
  ], histories)
  assert.equal(resolved[0].selection, null)
  assert.equal(resolved[1].selection, null)
  assert.ok(resolved[2].selection)
  assert.ok(resolveContextPresets([], []).every(item => item.selection === null))
})

test('advanced combinations and unavailable saved IDs remain custom; empty creation fields remain unset', () => {
  assert.equal(matchContextPreset({}, contexts, histories), undefined)
  assert.equal(matchContextPreset({ context_policy_id: null, history_policy_id: null }, contexts, histories), undefined)
  assert.equal(matchContextPreset({ context_policy_id: 'ctx-normal', history_policy_id: 'hist-window' }, contexts, histories), 'custom')
  assert.equal(matchContextPreset({ context_policy_id: 'ctx-short' }, contexts, histories), 'custom')
  assert.equal(matchContextPreset({ context_policy_id: 'removed-policy', history_policy_id: 'hist-window' }, contexts, histories), 'custom')
})

test('an explicitly disabled saved combination can still be identified without becoming selectable', () => {
  const disabledContexts = contexts.map(item => ({ ...item, enabled: false }))
  const current = { context_policy_id: 'ctx-short', history_policy_id: 'hist-window' }
  assert.equal(matchContextPreset(current, disabledContexts, histories), 'concise')
  assert.equal(resolveContextPresets(disabledContexts, histories)[0].selection, null)
  assert.deepEqual(current, { context_policy_id: 'ctx-short', history_policy_id: 'hist-window' })
})

test('applying a preset produces only the two existing policy fields and preserves independent memory choices', () => {
  const draft = Object.freeze({
    context_policy_id: 'legacy-context', history_policy_id: 'legacy-history',
    memory_write_policy_id: 'memory-write-custom', memory_recall_policy_id: 'memory-recall-custom',
    name: 'Existing agent',
  })
  const patch = resolveContextPresets(contexts, histories)[1].selection!
  assert.deepEqual(Object.keys(patch).sort(), ['context_policy_id', 'history_policy_id'])
  assert.deepEqual({ ...draft, ...patch }, {
    ...draft, context_policy_id: 'ctx-normal', history_policy_id: 'hist-summary',
  })
  assert.equal(draft.context_policy_id, 'legacy-context')
})

test('a preset change remains unsaved until the saved draft echoes both selected policy IDs', () => {
  const saved = { context_policy_id: 'ctx-short', history_policy_id: 'hist-window' }
  const next = resolveContextPresets(contexts, histories)[1].selection!
  assert.equal(contextSelectionChanged(saved, { ...saved }), false)
  assert.equal(contextSelectionChanged(next, saved), true)
  assert.equal(contextSelectionChanged(next, { ...saved, context_policy_id: next.context_policy_id }), true)
  const reloaded = JSON.parse(JSON.stringify({ ...saved, ...next }))
  assert.equal(contextSelectionChanged(next, reloaded), false)
  assert.equal(matchContextPreset(reloaded, contexts, histories), 'continuous')
  assert.equal(contextSelectionChanged({}, { context_policy_id: null, history_policy_id: null }), false)
})

test('a missing saved policy stays visible as an unavailable selected option without substitution', () => {
  const choices = contextPolicyOptions(contexts, 'previous-context-policy')
  assert.deepEqual(choices.at(-1), {
    label: '当前策略不可用（previous）', value: 'previous-context-policy', disabled: true,
  })
  assert.equal(contextSelectionUnavailable({ context_policy_id: 'previous-context-policy' }, contexts, histories), true)
  assert.equal(contextPolicyOptions(contexts, 'ctx-short').length, contexts.length)
  assert.equal(contextPolicyOptions(contexts, null).length, contexts.length)
  assert.equal(contextSelectionUnavailable({}, contexts, histories), false)
})

test('disabled advanced options are unavailable even when their IDs are still returned', () => {
  const items = [{ id: 'ctx-short', key: 'context.concise', name: '简洁上下文', enabled: false }]
  assert.deepEqual(contextPolicyOptions(items, 'ctx-short'), [{
    value: 'ctx-short', label: '简洁上下文 · context.concise（已停用）', disabled: true,
  }])
  assert.equal(contextSelectionUnavailable({ context_policy_id: 'ctx-short', history_policy_id: 'hist-window' }, items, histories), true)
  assert.equal(contextSelectionUnavailable({ history_policy_id: 'hist-window' }, contexts, [{ ...histories[0], enabled: false }]), true)
})

test('platform version and parameter customization does not mislabel the selected policy combination', () => {
  const customized = contexts.map(item => ({
    ...item, current_version_id: 'platform-policy-v7', default_parameters: { input_target_ratio: 0.7 },
  }))
  const selection = { context_policy_id: 'ctx-normal', history_policy_id: 'hist-summary' }
  assert.equal(matchContextPreset(selection, customized, histories), 'continuous')
  assert.equal(contextSelectionChanged(selection, { ...selection }), false)
  assert.equal(resolveContextPresets(customized, histories)[1].label, '持续通用型长任务')
})
