import { describe, expect, it } from 'vitest'

import {
  defaultRuleSplitLines,
  editorToRuleLines,
  isRuleSplitLines,
  isValidRuleSplitLines,
  ruleLinesToEditor,
  validateRuleSplitLines,
} from './rule-split-utils'

describe('isRuleSplitLines', () => {
  it('accepts a list of lines with a category id and nothing else', () => {
    expect(isRuleSplitLines([{ category_id: 'a', amount: 1 }, { category_id: 'b', remainder: true }])).toBe(true)
    expect(isRuleSplitLines('cat-1')).toBe(false)
    expect(isRuleSplitLines([{ amount: 1 }])).toBe(false)
    expect(isRuleSplitLines([null])).toBe(false)
  })
})

describe('validateRuleSplitLines', () => {
  it('matches the backend rules', () => {
    expect(validateRuleSplitLines([{ category_id: 'a', amount: 250 }, { category_id: 'b', remainder: true }])).toEqual([])
    expect(validateRuleSplitLines([{ category_id: 'a', amount: 250 }, { category_id: 'b', amount: 250 }])).toEqual([])
    expect(validateRuleSplitLines([{ category_id: 'a', percent: 50 }, { category_id: 'b', percent: 50 }])).toEqual([])
    expect(validateRuleSplitLines([{ category_id: 'a', amount: 1 }])).toEqual(['needTwoLines'])
    expect(validateRuleSplitLines(defaultRuleSplitLines())).toEqual(['needCategory', 'needValue'])
    expect(validateRuleSplitLines([{ category_id: 'a', remainder: true }, { category_id: 'b', remainder: true }])).toEqual(['tooManyRemainders'])
    expect(validateRuleSplitLines([{ category_id: 'a', percent: 60 }, { category_id: 'b', percent: 60 }])).toEqual(['percentOver'])
    expect(validateRuleSplitLines([{ category_id: 'a', percent: 40 }, { category_id: 'b', percent: 40 }])).toEqual(['percentShort'])
    expect(validateRuleSplitLines([{ category_id: 'a', amount: 1, percent: 5 }, { category_id: 'b', remainder: true }])).toEqual(['needValue'])
    expect(isValidRuleSplitLines([{ category_id: 'a', amount: 250 }, { category_id: 'b', remainder: true }])).toBe(true)
    expect(isValidRuleSplitLines('x')).toBe(false)
  })
})

describe('editor round trip', () => {
  it('keeps values through the editor and back in the display locale', () => {
    const lines = ruleLinesToEditor([{ category_id: 'a', amount: 250.5 }, { category_id: 'b', percent: 12.5 }, { category_id: 'c', remainder: true }], 'de-DE')
    expect(lines.map((l) => [l.category_id, l.amount, l.percent, l.remainder])).toEqual([
      ['a', '250,5', '', false], ['b', '', '12,5', false], ['c', '', '', true],
    ])
    expect(editorToRuleLines(lines, 'de-DE')).toEqual([
      { category_id: 'a', amount: 250.5 }, { category_id: 'b', percent: 12.5 }, { category_id: 'c', remainder: true },
    ])
  })

  it('leaves an unfinished number out instead of guessing', () => {
    const lines = ruleLinesToEditor([{ category_id: 'a', amount: 1 }, { category_id: 'b', remainder: true }], 'en-US')
    lines[0].amount = '12.'
    expect(editorToRuleLines(lines, 'en-US')[0]).toEqual({ category_id: 'a', amount: 12 })
    lines[0].amount = 'abc'
    expect(editorToRuleLines(lines, 'en-US')[0]).toEqual({ category_id: 'a' })
  })
})
