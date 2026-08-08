import { describe, expect, it } from 'vitest'

import { parseOfflineEditionMarker } from './offline-edition'

describe('parseOfflineEditionMarker', () => {
  it('accepts the canonical offline marker', () => {
    expect(
      parseOfflineEditionMarker({
        format: 1,
        version: '1.2.3',
        installedAt: '2026-07-17T00:00:00Z',
        payloadSha256: 'a'.repeat(64)
      })
    ).toEqual({
      format: 1,
      version: '1.2.3',
      installedAt: '2026-07-17T00:00:00Z',
      payloadSha256: 'a'.repeat(64)
    })
  })

  it.each([
    null,
    {},
    { format: 2, version: '1.2.3', payloadSha256: 'a'.repeat(64) },
    { format: 1, version: 'latest', payloadSha256: 'a'.repeat(64) },
    { format: 1, version: '1.2.3', payloadSha256: 'short' }
  ])('rejects an invalid marker: %j', marker => {
    expect(parseOfflineEditionMarker(marker)).toBeNull()
  })
})
