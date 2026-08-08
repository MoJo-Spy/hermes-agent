import fs from 'node:fs'

export interface OfflineEditionMarker {
  format: 1
  version: string
  installedAt?: string
  payloadSha256: string
}

const HERMES_VERSION = /^\d+\.\d+\.\d+$/
const SHA256 = /^[0-9a-f]{64}$/i

export function parseOfflineEditionMarker(value: unknown): OfflineEditionMarker | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const marker = value as Record<string, unknown>

  if (
    marker.format !== 1 ||
    typeof marker.version !== 'string' ||
    !HERMES_VERSION.test(marker.version) ||
    typeof marker.payloadSha256 !== 'string' ||
    !SHA256.test(marker.payloadSha256) ||
    (marker.installedAt !== undefined && typeof marker.installedAt !== 'string')
  ) {
    return null
  }

  const installedAt = typeof marker.installedAt === 'string' ? marker.installedAt : undefined

  return {
    format: 1,
    version: marker.version,
    ...(installedAt === undefined ? {} : { installedAt }),
    payloadSha256: marker.payloadSha256
  }
}

export function readOfflineEditionMarker(filePath: string): OfflineEditionMarker | null {
  try {
    return parseOfflineEditionMarker(JSON.parse(fs.readFileSync(filePath, 'utf8')))
  } catch {
    return null
  }
}
