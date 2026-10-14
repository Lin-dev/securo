#!/usr/bin/env node
// Three-way JSON merge driver for the locale files, so two agent slices
// adding keys to en.json (or any other locale) under different top-level
// prefixes don't textually conflict just because they both touched a
// neighbouring line.
//
// One-time registration (the lead runs this once -- it edits shared git
// config, so this script never runs it itself):
//
//   git config merge.i18njson.name "i18n JSON 3-way" && git config merge.i18njson.driver "node frontend/scripts/i18n-merge-driver.mjs %O %A %B"
//
// Usage (as git invokes it): node frontend/scripts/i18n-merge-driver.mjs %O %A %B
//   %O = common ancestor (base), %A = ours, %B = theirs. The result is
//   written back into %A in canonical format. Exit 0 on a clean merge,
//   exit 1 (result still written, ours wins per key) on a conflict.
'use strict'

import { readFileSync, writeFileSync } from 'node:fs'

const MISSING = Symbol('missing')

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function canonicalJson(obj) {
  return `${JSON.stringify(obj, null, 2)}\n`
}

function deepEqual(a, b) {
  if (a === b) return true
  if (isPlainObject(a) && isPlainObject(b)) {
    const aKeys = Object.keys(a)
    const bKeys = Object.keys(b)
    if (aKeys.length !== bKeys.length) return false
    return aKeys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && deepEqual(a[key], b[key]))
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((value, i) => deepEqual(value, b[i]))
  }
  return false
}

function get(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key) ? obj[key] : MISSING
}

/**
 * Resolves one value (a whole file at the root, or one key's value one
 * level down) across base/ours/theirs. Returns MISSING when the key
 * should not appear in the result at all (an agreed-on, or ours-wins,
 * deletion). Pushes `path` onto `conflicts` whenever ours and theirs
 * disagree in a way that can't be resolved automatically.
 */
function resolveValue(base, ours, theirs, path, conflicts) {
  const baseMissing = base === MISSING
  const oursMissing = ours === MISSING
  const theirsMissing = theirs === MISSING

  if (baseMissing) {
    if (oursMissing && theirsMissing) return MISSING
    if (oursMissing) return theirs // added on theirs only
    if (theirsMissing) return ours // added on ours only
    // Added independently on both sides.
    if (isPlainObject(ours) && isPlainObject(theirs)) {
      return mergeObjects({}, ours, theirs, path, conflicts)
    }
    if (deepEqual(ours, theirs)) return ours // identical addition
    conflicts.push(path)
    return ours // ours wins, best effort
  }

  // base is present.
  if (oursMissing && theirsMissing) return MISSING // deleted on both, agreed

  if (oursMissing) {
    if (deepEqual(theirs, base)) return MISSING // deleted on ours, unchanged on theirs
    conflicts.push(path) // deleted on ours, changed on theirs
    return MISSING // ours wins -> stays deleted
  }

  if (theirsMissing) {
    if (deepEqual(ours, base)) return MISSING // unchanged on ours, deleted on theirs
    conflicts.push(path) // changed on ours, deleted on theirs
    return ours // ours wins -> keep ours' change
  }

  const oursChanged = !deepEqual(ours, base)
  const theirsChanged = !deepEqual(theirs, base)
  if (!oursChanged && !theirsChanged) return ours // unchanged on both
  if (!oursChanged) return theirs // changed on theirs only
  if (!theirsChanged) return ours // changed on ours only

  // Changed on both sides.
  if (isPlainObject(ours) && isPlainObject(theirs)) {
    return mergeObjects(isPlainObject(base) ? base : {}, ours, theirs, path, conflicts)
  }
  if (deepEqual(ours, theirs)) return ours // identical change
  conflicts.push(path)
  return ours // ours wins, best effort
}

function insertAfter(entries, indexOfKey, afterKey, newKey, newValue) {
  const insertPos = afterKey === null ? 0 : indexOfKey.get(afterKey) + 1
  entries.splice(insertPos, 0, [newKey, newValue])
  for (const [existingKey, idx] of indexOfKey) {
    if (idx >= insertPos) indexOfKey.set(existingKey, idx + 1)
  }
  indexOfKey.set(newKey, insertPos)
}

/**
 * Merges one object level. Key order: ours' own order is never disturbed.
 * A key theirs added that base and ours never had is inserted right after
 * whichever key precedes it in theirs' own order (or at the front), which
 * keeps en's intended order stable when ours and theirs each slot a new
 * top-level block next to the same neighbour.
 */
function mergeObjects(base, ours, theirs, pathPrefix, conflicts) {
  const baseObj = isPlainObject(base) ? base : {}
  const oursObj = isPlainObject(ours) ? ours : {}
  const theirsObj = isPlainObject(theirs) ? theirs : {}

  const entries = Object.keys(oursObj).map((key) => [key, oursObj[key]])
  const indexOfKey = new Map(entries.map(([key], i) => [key, i]))

  // Resolve every key ours currently has.
  for (const key of Object.keys(oursObj)) {
    const path = pathPrefix ? `${pathPrefix}.${key}` : key
    const resolved = resolveValue(get(baseObj, key), oursObj[key], get(theirsObj, key), path, conflicts)
    const idx = indexOfKey.get(key)
    entries[idx] = resolved === MISSING ? null : [key, resolved]
  }

  // Keys base had that ours deleted entirely: never re-added to the
  // result (ours wins on conflict means "stays deleted"), but still walk
  // them so a theirs-changed/ours-deleted disagreement is reported.
  for (const key of Object.keys(baseObj)) {
    if (Object.prototype.hasOwnProperty.call(oursObj, key)) continue
    const path = pathPrefix ? `${pathPrefix}.${key}` : key
    resolveValue(baseObj[key], MISSING, get(theirsObj, key), path, conflicts)
  }

  // Keys theirs added that neither base nor ours ever had.
  let lastSurvivingKey = null
  for (const key of Object.keys(theirsObj)) {
    const isTheirsOnlyNew =
      !Object.prototype.hasOwnProperty.call(baseObj, key) && !Object.prototype.hasOwnProperty.call(oursObj, key)

    if (isTheirsOnlyNew) {
      const path = pathPrefix ? `${pathPrefix}.${key}` : key
      const value = resolveValue(MISSING, MISSING, theirsObj[key], path, conflicts)
      insertAfter(entries, indexOfKey, lastSurvivingKey, key, value)
      lastSurvivingKey = key
      continue
    }

    const idx = indexOfKey.get(key)
    if (idx !== undefined && entries[idx] !== null) lastSurvivingKey = key
  }

  return Object.fromEntries(entries.filter((entry) => entry !== null))
}

function loadJson(filePath) {
  const raw = readFileSync(filePath, 'utf8')
  return JSON.parse(raw)
}

function main(argv) {
  const [basePath, oursPath, theirsPath] = argv
  if (!basePath || !oursPath || !theirsPath) {
    console.error('usage: i18n-merge-driver.mjs <base> <ours> <theirs>')
    process.exitCode = 1
    return
  }

  let base
  let ours
  let theirs
  try {
    base = loadJson(basePath)
    ours = loadJson(oursPath)
    theirs = loadJson(theirsPath)
  } catch (error) {
    console.error(`i18n-merge-driver: invalid JSON -- ${error.message}`)
    process.exitCode = 1
    return
  }

  const conflicts = []
  const merged = resolveValue(base, ours, theirs, '', conflicts)
  writeFileSync(oursPath, canonicalJson(merged === MISSING ? {} : merged))

  if (conflicts.length > 0) {
    console.error('i18n-merge-driver: conflicting keys:')
    for (const path of conflicts) console.error(`  ${path}`)
    process.exitCode = 1
    return
  }

  process.exitCode = 0
}

main(process.argv.slice(2))
