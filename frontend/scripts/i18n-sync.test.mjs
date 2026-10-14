// Exercises i18n-sync.mjs as a CLI against throwaway locale fixtures, since
// the contract that matters is the one git/CI actually invoke: flags, exit
// codes, and what lands on disk.
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, readFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), 'i18n-sync.mjs')

function makeLocalesDir(files) {
  const dir = mkdtempSync(path.join(tmpdir(), 'i18n-sync-test-'))
  for (const [name, obj] of Object.entries(files)) {
    writeFileSync(path.join(dir, `${name}.json`), `${JSON.stringify(obj, null, 2)}\n`)
  }
  return dir
}

function runSync(dir, extraArgs = []) {
  const result = spawnSync('node', [SCRIPT, '--locales-dir', dir, ...extraArgs], { encoding: 'utf8' })
  return result
}

function readLocale(dir, name) {
  return JSON.parse(readFileSync(path.join(dir, `${name}.json`), 'utf8'))
}

describe('i18n-sync', () => {
  test('inserts a missing key at the right position using the en value', () => {
    const dir = makeLocalesDir({
      en: { a: 'A', b: 'B', c: 'C' },
      fr: { a: 'A-fr', c: 'C-fr' },
    })
    try {
      const result = runSync(dir)
      assert.equal(result.status, 0)

      const fr = readLocale(dir, 'fr')
      assert.deepEqual(Object.keys(fr), ['a', 'b', 'c'])
      assert.equal(fr.a, 'A-fr') // existing translation untouched
      assert.equal(fr.b, 'B') // copied verbatim from en
      assert.equal(fr.c, 'C-fr')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('inserts a missing key at the front when en has nothing earlier', () => {
    const dir = makeLocalesDir({
      en: { a: 'A', b: 'B' },
      fr: { b: 'B-fr' },
    })
    try {
      runSync(dir)
      const fr = readLocale(dir, 'fr')
      assert.deepEqual(Object.keys(fr), ['a', 'b'])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('recurses into nested objects, adding a missing child and a missing whole subtree', () => {
    const dir = makeLocalesDir({
      en: {
        group: { a: 'A', b: 'B', c: 'C' },
        other: { x: 'X' },
      },
      fr: {
        group: { a: 'A-fr', c: 'C-fr' },
      },
    })
    try {
      runSync(dir)
      const fr = readLocale(dir, 'fr')
      assert.deepEqual(Object.keys(fr.group), ['a', 'b', 'c'])
      assert.equal(fr.group.b, 'B')
      assert.deepEqual(fr.other, { x: 'X' })
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('honours plural forms: a locale covering a base via other suffixes gets nothing added', () => {
    const dir = makeLocalesDir({
      en: { cnt_one: 'one item', cnt_other: '{{count}} items' },
      pl: { cnt_one: 'jeden', cnt_few: 'kilka', cnt_many: 'wiele', cnt_other: 'innych' },
    })
    try {
      const before = readFileSync(path.join(dir, 'pl.json'), 'utf8')
      const result = runSync(dir)
      assert.equal(result.status, 0)
      const after = readFileSync(path.join(dir, 'pl.json'), 'utf8')
      assert.equal(after, before) // untouched: plural forms already cover both en keys
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('honours plural forms: a bare en key already pluralized by the locale gets nothing added', () => {
    const dir = makeLocalesDir({
      en: { drillDown: 'View {{count}} transaction' },
      pl: { drillDown_one: 'jeden', drillDown_few: 'kilka', drillDown_many: 'wiele', drillDown_other: 'inne' },
    })
    try {
      const before = readFileSync(path.join(dir, 'pl.json'), 'utf8')
      runSync(dir)
      const after = readFileSync(path.join(dir, 'pl.json'), 'utf8')
      assert.equal(after, before)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('keeps extra keys not present in en and reports them, never deleting them', () => {
    const dir = makeLocalesDir({
      en: { a: 'A' },
      fr: { a: 'A-fr', legacy: 'old translation' },
    })
    try {
      const result = runSync(dir)
      assert.equal(result.status, 0)
      assert.match(result.stdout, /extra/i)

      const fr = readLocale(dir, 'fr')
      assert.equal(fr.legacy, 'old translation')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('--check exits 1 and prints missing keys per locale, without writing anything', () => {
    const dir = makeLocalesDir({
      en: { a: 'A', b: 'B' },
      fr: { a: 'A-fr' },
    })
    try {
      const before = readFileSync(path.join(dir, 'fr.json'), 'utf8')
      const result = runSync(dir, ['--check'])
      assert.equal(result.status, 1)
      assert.match(result.stdout, /fr\.json/)
      assert.match(result.stdout, /\bb\b/)

      const after = readFileSync(path.join(dir, 'fr.json'), 'utf8')
      assert.equal(after, before) // --check must never write
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('--check exits 0 when every locale already has every key', () => {
    const dir = makeLocalesDir({
      en: { a: 'A', b: 'B' },
      fr: { a: 'A-fr', b: 'B-fr' },
    })
    try {
      const result = runSync(dir, ['--check'])
      assert.equal(result.status, 0)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('--check does not fail on extra keys alone', () => {
    const dir = makeLocalesDir({
      en: { a: 'A' },
      fr: { a: 'A-fr', legacy: 'old' },
    })
    try {
      const result = runSync(dir, ['--check'])
      assert.equal(result.status, 0)
      assert.match(result.stdout, /legacy/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('leaves an already-complete locale file untouched (same content, no rewrite)', () => {
    const dir = makeLocalesDir({
      en: { a: 'A', b: 'B' },
      fr: { a: 'A-fr', b: 'B-fr' },
      de: { a: 'A-de' }, // de is missing "b" so the run does write something
    })
    try {
      const frPath = path.join(dir, 'fr.json')
      const before = readFileSync(frPath, 'utf8')
      const statBefore = statSync(frPath)

      runSync(dir)

      const after = readFileSync(frPath, 'utf8')
      const statAfter = statSync(frPath)
      assert.equal(after, before)
      assert.equal(statAfter.mtimeMs, statBefore.mtimeMs)

      const de = readLocale(dir, 'de')
      assert.equal(de.b, 'B') // sanity: the run did do something, just not to fr
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('rejects an unknown flag', () => {
    const dir = makeLocalesDir({ en: { a: 'A' }, fr: { a: 'A-fr' } })
    try {
      const result = runSync(dir, ['--bogus'])
      assert.notEqual(result.status, 0)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('default --locales-dir resolves relative to the script, not the cwd', () => {
    // Smoke test only: run with no --locales-dir from an arbitrary cwd and
    // confirm it finds the real frontend/src/locales directory rather than
    // erroring out looking somewhere relative to process.cwd().
    const result = spawnSync('node', [SCRIPT, '--check'], { encoding: 'utf8', cwd: tmpdir() })
    assert.ok(result.status === 0 || result.status === 1, `unexpected status ${result.status}: ${result.stderr}`)
  })
})
