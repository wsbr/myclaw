import assert from 'node:assert/strict'
import test from 'node:test'
import { parseEvalDefinition } from '../src/lib/evalDefinitions.ts'

const definition = { name: 'Search', cases: [{ case_key: 'a', name: 'A', input_text: 'Search', assertions: { require_tools: ['web_search'] } }] }
test('portable definition preserves tool assertions and accepts the export envelope', () => {
  assert.deepEqual(parseEvalDefinition(JSON.stringify({ format: 'myclaw.eval-suite.v1', definition })), definition)
  assert.deepEqual(parseEvalDefinition(JSON.stringify(definition)), definition)
})
test('rejects empty, malformed, duplicate, and oversized case bundles before editing', () => {
  for (const value of [{}, { ...definition, cases: [] }, { ...definition, cases: [null] }, { ...definition, cases: [definition.cases[0], definition.cases[0]] }, { ...definition, cases: Array(21).fill(definition.cases[0]) }]) {
    assert.throws(() => parseEvalDefinition(JSON.stringify(value)))
  }
})
