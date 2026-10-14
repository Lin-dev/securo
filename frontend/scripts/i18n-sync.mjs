#!/usr/bin/env node
// Keeps every locale under frontend/src/locales structurally complete.
//
// Up to four agent slices add keys to en.json in parallel, each under its
// own top-level prefix. This script copies whatever a locale is missing
// straight from en (English text as a placeholder translation), landing
// each new key in the exact position it holds in en's own key order so a
// human translator can find it next to its English neighbours. It never
// reorders or rewrites a key a locale already has.
//
// The missing/extra-key logic mirrors frontend/src/locales/i18n.test.ts's
// pluralBase/hasKeyOrPluralForms rules exactly, so `--check` agrees with
// what that test will say.
//
// Usage:
//   node frontend/scripts/i18n-sync.mjs [--check] [--locales-dir <path>]
'use strict'

import { readFileSync, writeFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const DEFAULT_LOCALES_DIR = path.join(SCRIPT_DIR, '..', 'src', 'locales')
const EN_LOCALE = 'en'

// Mirrors i18n.test.ts's PLURAL_SUFFIXES / pluralBase / hasKeyOrPluralForms
// verbatim. Keep these two files in lockstep -- this is what makes
// `--check` agree with the parity test.
const PLURAL_SUFFIXES = ['_zero', '_one', '_two', '_few', '_many', '_other']

function pluralBase(key) {
  for (const suffix of PLURAL_SUFFIXES) {
    if (key.endsWith(suffix)) {
      const base = key.slice(0, -suffix.length)
      // One suffix, not a chain -- otherwise "title_one_other" would reduce
      // to "title_one", which en might have, letting a malformed key ride
      // in as though it were a legitimate plural form of a real one.
      return PLURAL_SUFFIXES.some((inner) => base.endsWith(inner)) ? null : base
    }
  }
  return null
}

function hasKeyOrPluralForms(keySet, baseKey) {
  return keySet.has(baseKey) || PLURAL_SUFFIXES.some((suffix) => keySet.has(`${baseKey}${suffix}`))
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function flattenKeys(obj, prefix = '') {
  const out = []
  for (const [key, value] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${key}` : key
    if (isPlainObject(value)) out.push(...flattenKeys(value, full))
    else out.push(full)
  }
  return out
}

function canonicalJson(obj) {
  return `${JSON.stringify(obj, null, 2)}\n`
}

/**
 * Builds the merged locale object for one object level: the result keeps
 * the locale's own key order (nothing it already has is ever reordered or
 * rewritten), with any key en has that this locale lacks -- and that isn't
 * already covered through a plural-form sibling -- inserted right after
 * whichever en key precedes it, or at the front of the object if en has
 * nothing earlier.
 *
 * `localeFlatKeys` is the *whole file's* flattened key set (not just this
 * level's), since a plural-form sibling always lives next to its base in
 * the same object, so checking globally is equivalent and simpler.
 */
function mergeLevel(enNode, localeNode, localeFlatKeys, pathPrefix, added) {
  const entries = Object.entries(localeNode)
  const indexOfKey = new Map(entries.map(([key], i) => [key, i]))

  for (let i = 0; i < entries.length; i++) {
    const [key, localeValue] = entries[i]
    const full = pathPrefix ? `${pathPrefix}.${key}` : key
    const enValue = Object.prototype.hasOwnProperty.call(enNode, key) ? enNode[key] : undefined
    if (isPlainObject(localeValue) && isPlainObject(enValue)) {
      entries[i] = [key, mergeLevel(enValue, localeValue, localeFlatKeys, full, added)]
    }
  }

  let insertAfterIndex = -1
  for (const key of Object.keys(enNode)) {
    const full = pathPrefix ? `${pathPrefix}.${key}` : key

    if (indexOfKey.has(key)) {
      insertAfterIndex = indexOfKey.get(key)
      continue
    }

    const enValue = enNode[key]
    if (!isPlainObject(enValue) && hasKeyOrPluralForms(localeFlatKeys, full)) {
      // Covered by a plural-form sibling that already lives in this same
      // object (e.g. en has "count_one"/"count_other" and this locale
      // already carries "count_few"/"count_many"/"count_other"). Nothing
      // to insert, and we leave insertAfterIndex where it was since this
      // key was never actually added to the result.
      continue
    }

    const newValue = isPlainObject(enValue)
      ? mergeLevel(enValue, {}, localeFlatKeys, full, added)
      : enValue
    if (!isPlainObject(enValue)) added.push(full)

    const insertPos = insertAfterIndex + 1
    entries.splice(insertPos, 0, [key, newValue])
    for (const [existingKey, idx] of indexOfKey) {
      if (idx >= insertPos) indexOfKey.set(existingKey, idx + 1)
    }
    indexOfKey.set(key, insertPos)
    insertAfterIndex = insertPos
  }

  return Object.fromEntries(entries)
}

function findExtraKeys(enFlatKeySet, localeFlatKeys) {
  return localeFlatKeys.filter((key) => {
    if (enFlatKeySet.has(key)) return false
    const base = pluralBase(key)
    return !(base && hasKeyOrPluralForms(enFlatKeySet, base))
  })
}

function findMissingKeys(enFlatKeys, localeFlatKeySet) {
  return enFlatKeys.filter((key) => !hasKeyOrPluralForms(localeFlatKeySet, key))
}

function parseArgs(argv) {
  let check = false
  let localesDir = DEFAULT_LOCALES_DIR
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === '--check') {
      check = true
    } else if (arg === '--locales-dir') {
      const value = argv[++i]
      if (value === undefined) throw new Error('--locales-dir requires a path argument')
      localesDir = path.resolve(value)
    } else {
      throw new Error(`Unknown argument: ${arg}`)
    }
  }
  return { check, localesDir }
}

function loadLocale(dir, name) {
  const filePath = path.join(dir, `${name}.json`)
  const raw = readFileSync(filePath, 'utf8')
  return { filePath, raw, root: JSON.parse(raw) }
}

function run(argv) {
  const { check, localesDir } = parseArgs(argv)

  const en = loadLocale(localesDir, EN_LOCALE)
  const enFlatKeys = flattenKeys(en.root)
  const enFlatKeySet = new Set(enFlatKeys)

  const localeNames = readdirSync(localesDir)
    .filter((f) => f.endsWith('.json') && f !== `${EN_LOCALE}.json`)
    .map((f) => f.replace(/\.json$/, ''))
    .sort()

  let anyMissing = false

  for (const name of localeNames) {
    const locale = loadLocale(localesDir, name)
    const localeFlatKeys = flattenKeys(locale.root)
    const localeFlatKeySet = new Set(localeFlatKeys)

    const missing = findMissingKeys(enFlatKeys, localeFlatKeySet)
    const extra = findExtraKeys(enFlatKeySet, localeFlatKeys)

    if (check) {
      if (missing.length > 0) {
        anyMissing = true
        console.log(`${name}.json: missing ${missing.length} key(s):`)
        for (const key of missing) console.log(`  ${key}`)
      }
      if (extra.length > 0) {
        console.log(`${name}.json: ${extra.length} extra key(s) not in en (kept):`)
        for (const key of extra) console.log(`  ${key}`)
      }
      continue
    }

    if (missing.length === 0) {
      if (extra.length > 0) {
        console.log(`${name}.json: unchanged, ${extra.length} extra key(s) not in en (kept)`)
      }
      continue
    }

    const added = []
    const merged = mergeLevel(en.root, locale.root, localeFlatKeySet, '', added)
    const newText = canonicalJson(merged)
    if (newText === locale.raw) continue // nothing textually changed, don't touch mtime

    writeFileSync(locale.filePath, newText)
    const extraNote = extra.length > 0 ? `, kept ${extra.length} extra key(s)` : ''
    console.log(`${name}.json: added ${added.length} key(s)${extraNote}`)
  }

  if (check && anyMissing) process.exitCode = 1
}

try {
  run(process.argv.slice(2))
} catch (error) {
  console.error(`i18n-sync: ${error.message}`)
  process.exitCode = 1
}
