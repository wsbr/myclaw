export interface LiveDraft {
  streamId: string
  runId: string
  sequence: number
  text: string
}

export interface LiveDeltaLike {
  stream_id: string
  run_id: string
  sequence: number
  delta: string
}

export function appendLiveDelta(
  current: LiveDraft | null,
  event: LiveDeltaLike,
  currentRunId: string | null,
  blocked: boolean,
  closedStreams: ReadonlySet<string>,
): LiveDraft | null {
  if (
    blocked
    || event.run_id !== currentRunId
    || closedStreams.has(event.stream_id)
  ) return current

  if (current?.streamId === event.stream_id) {
    if (event.sequence !== current.sequence + 1) return current
    return {
      ...current, sequence: event.sequence,
      text: current.text + event.delta,
    }
  }

  // Ephemeral streams are not replayed. Never display a tail when the first
  // observed chunk is already missing an earlier sequence.
  if (event.sequence !== 1) return current
  return {
    streamId: event.stream_id, runId: event.run_id,
    sequence: event.sequence, text: event.delta,
  }
}

export function closeLiveDraft(
  current: LiveDraft | null, streamId: string,
): LiveDraft | null {
  return current?.streamId === streamId ? null : current
}
