// Runs i18n-merge-driver.mjs as a child process against temp files, the
// way git actually invokes a merge driver (`driver %O %A %B`). The "real
// locale" cases copy the actual en.json so the test exercises the merge
// against the file's real shape, not a toy fixture.
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(SCRIPT_DIR, 'i18n-merge-driver.mjs')
const EN_JSON_PATH = path.join(SCRIPT_DIR, '..', 'src', 'locales', 'en.json')

function writeTempFiles(dir, files) {
  const paths = {}
  for (const [name, obj] of Object.entries(files)) {
    const filePath = path.join(dir, name)
    writeFileSync(filePath, typeof obj === 'string' ? obj : `${JSON.stringify(obj, null, 2)}\n`)
    paths[name] = filePath
  }
  return paths
}

function runDriver(basePath, oursPath, theirsPath) {
  return spawnSync('node', [SCRIPT, basePath, oursPath, theirsPath], { encoding: 'utf8' })
}

function readOurs(oursPath) {
  return JSON.parse(readFileSync(oursPath, 'utf8'))
}

function withTempDir(fn) {
  const dir = mkdtempSync(path.join(tmpdir(), 'i18n-merge-driver-test-'))
  try {
    return fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

function loadRealEn() {
  return JSON.parse(readFileSync(EN_JSON_PATH, 'utf8'))
}

describe('i18n-merge-driver', () => {
  test('clean merge: two slices each add a new top-level block after "goals"', () => {
    withTempDir((dir) => {
      const base = loadRealEn()
      assert.ok('goals' in base && 'recurring' in base, 'fixture assumption: en has goals and recurring')

      const insertAfterGoals = (obj, key, value) => {
        const result = {}
        for (const [k, v] of Object.entries(obj)) {
          result[k] = v
          if (k === 'goals') result[key] = value
        }
        return result
      }

      const wealthRoad = { wealthRoad: { title: 'Wealth Road', step1: 'Step one', step2: 'Step two' } }
      const wealthNetworth = { wealthNetworth: { title: 'Net Worth', chart: 'Chart', summary: 'Summary' } }

      const ours = insertAfterGoals(base, 'wealthRoad', wealthRoad.wealthRoad)
      const theirs = insertAfterGoals(base, 'wealthNetworth', wealthNetworth.wealthNetworth)

      const paths = writeTempFiles(dir, { 'base.json': base, 'ours.json': ours, 'theirs.json': theirs })
      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 0, result.stderr)
      assert.equal(result.stderr, '')

      const merged = readOurs(paths['ours.json'])
      assert.deepEqual(merged.wealthRoad, wealthRoad.wealthRoad)
      assert.deepEqual(merged.wealthNetworth, wealthNetworth.wealthNetworth)

      // All original keys/values survive, in their original relative order.
      const mergedOriginalKeys = Object.keys(merged).filter((k) => k in base)
      assert.deepEqual(mergedOriginalKeys, Object.keys(base))
      for (const key of Object.keys(base)) assert.deepEqual(merged[key], base[key])

      // Both new blocks land between "goals" and "recurring".
      const order = Object.keys(merged)
      const goalsIdx = order.indexOf('goals')
      const recurringIdx = order.indexOf('recurring')
      const roadIdx = order.indexOf('wealthRoad')
      const networthIdx = order.indexOf('wealthNetworth')
      assert.ok(goalsIdx < roadIdx && roadIdx < recurringIdx)
      assert.ok(goalsIdx < networthIdx && networthIdx < recurringIdx)

      // Canonical formatting: re-serializing the parsed result reproduces the file byte-for-byte.
      const onDisk = readFileSync(paths['ours.json'], 'utf8')
      assert.equal(`${JSON.stringify(merged, null, 2)}\n`, onDisk)
    })
  })

  test('clean merge: two slices each add a different new key inside "dashboard"', () => {
    withTempDir((dir) => {
      const base = loadRealEn()
      assert.ok(isPlainObjectLike(base.dashboard), 'fixture assumption: en.dashboard is an object')

      const ours = { ...base, dashboard: { ...base.dashboard, oursOnlyKey: 'Ours only' } }
      const theirs = { ...base, dashboard: { ...base.dashboard, theirsOnlyKey: 'Theirs only' } }

      const paths = writeTempFiles(dir, { 'base.json': base, 'ours.json': ours, 'theirs.json': theirs })
      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 0, result.stderr)
      const merged = readOurs(paths['ours.json'])
      assert.equal(merged.dashboard.oursOnlyKey, 'Ours only')
      assert.equal(merged.dashboard.theirsOnlyKey, 'Theirs only')
      for (const key of Object.keys(base.dashboard)) assert.deepEqual(merged.dashboard[key], base.dashboard[key])
    })
  })

  test('conflict: both sides change common.save differently', () => {
    withTempDir((dir) => {
      const base = loadRealEn()
      const ours = { ...base, common: { ...base.common, save: 'Save (ours)' } }
      const theirs = { ...base, common: { ...base.common, save: 'Save (theirs)' } }

      const paths = writeTempFiles(dir, { 'base.json': base, 'ours.json': ours, 'theirs.json': theirs })
      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 1)
      assert.match(result.stderr, /common\.save/)

      // Best-effort output still written, ours wins.
      const merged = readOurs(paths['ours.json'])
      assert.equal(merged.common.save, 'Save (ours)')
    })
  })

  test('conflict: deleted on one side, modified on the other', () => {
    withTempDir((dir) => {
      const base = loadRealEn()
      const ours = { ...base, common: { ...base.common } }
      delete ours.common.cancel
      const theirs = { ...base, common: { ...base.common, cancel: 'Cancel (theirs)' } }

      const paths = writeTempFiles(dir, { 'base.json': base, 'ours.json': ours, 'theirs.json': theirs })
      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 1)
      assert.match(result.stderr, /common\.cancel/)

      // Ours wins: deletion stands.
      const merged = readOurs(paths['ours.json'])
      assert.equal('cancel' in merged.common, false)
    })
  })

  test('no conflict: identical change on both sides', () => {
    withTempDir((dir) => {
      const base = loadRealEn()
      const ours = { ...base, common: { ...base.common, save: 'Save now' } }
      const theirs = { ...base, common: { ...base.common, save: 'Save now' } }

      const paths = writeTempFiles(dir, { 'base.json': base, 'ours.json': ours, 'theirs.json': theirs })
      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 0, result.stderr)
      assert.equal(result.stderr, '')
      const merged = readOurs(paths['ours.json'])
      assert.equal(merged.common.save, 'Save now')
    })
  })

  test('invalid JSON on any side: exits 1, writes nothing', () => {
    withTempDir((dir) => {
      const base = { a: 'A' }
      const ours = { a: 'A-ours' }
      const paths = writeTempFiles(dir, {
        'base.json': base,
        'ours.json': ours,
        'theirs.json': '{ not valid json',
      })
      const oursBefore = readFileSync(paths['ours.json'], 'utf8')

      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 1)
      assert.match(result.stderr, /invalid JSON/i)
      const oursAfter = readFileSync(paths['ours.json'], 'utf8')
      assert.equal(oursAfter, oursBefore) // never write garbage
    })
  })

  test('simple case: unchanged on one side takes the other side, no conflict', () => {
    withTempDir((dir) => {
      const base = { a: 'A', b: 'B' }
      const ours = { a: 'A', b: 'B' } // unchanged
      const theirs = { a: 'A changed', b: 'B' }

      const paths = writeTempFiles(dir, { 'base.json': base, 'ours.json': ours, 'theirs.json': theirs })
      const result = runDriver(paths['base.json'], paths['ours.json'], paths['theirs.json'])

      assert.equal(result.status, 0, result.stderr)
      const merged = readOurs(paths['ours.json'])
      assert.deepEqual(merged, { a: 'A changed', b: 'B' })
    })
  })
})

function isPlainObjectLike(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
