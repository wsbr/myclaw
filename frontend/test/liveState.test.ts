import assert from 'node:assert/strict'
import test from 'node:test'

import { appendLiveDelta, closeLiveDraft } from '../src/pages/Session/liveState.ts'


const open = new Set<string>()

test('appends only contiguous chunks for the active Run', () => {
  const first = appendLiveDelta(null, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 1, delta: 'A',
  }, 'run-1', false, open)
  const second = appendLiveDelta(first, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 2, delta: 'B',
  }, 'run-1', false, open)

  assert.equal(second?.text, 'AB')
  assert.equal(second?.sequence, 2)
})

test('ignores duplicate, out-of-order, stale Run, and missing-prefix chunks', () => {
  const first = appendLiveDelta(null, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 1, delta: 'A',
  }, 'run-1', false, open)
  const gap = appendLiveDelta(first, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 3, delta: 'C',
  }, 'run-1', false, open)
  const duplicate = appendLiveDelta(first, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 1, delta: 'A',
  }, 'run-1', false, open)
  const stale = appendLiveDelta(first, {
    stream_id: 'stream-2', run_id: 'run-2', sequence: 1, delta: 'stale',
  }, 'run-1', false, open)
  const missingPrefix = appendLiveDelta(null, {
    stream_id: 'stream-3', run_id: 'run-1', sequence: 2, delta: 'tail',
  }, 'run-1', false, open)

  assert.equal(gap, first)
  assert.equal(duplicate, first)
  assert.equal(stale, first)
  assert.equal(missingPrefix, null)
})

test('blocked reconnects and terminal events retract provisional text', () => {
  const first = appendLiveDelta(null, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 1, delta: 'draft',
  }, 'run-1', false, open)
  const blocked = appendLiveDelta(first, {
    stream_id: 'stream-1', run_id: 'run-1', sequence: 2, delta: ' tail',
  }, 'run-1', true, open)

  assert.equal(blocked, first)
  assert.equal(closeLiveDraft(first, 'stream-1'), null)
  assert.equal(closeLiveDraft(first, 'another-stream'), first)
})
