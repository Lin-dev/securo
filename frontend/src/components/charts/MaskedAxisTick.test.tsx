import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { MaskedAxisTick } from '@/components/charts/MaskedAxisTick'

afterEach(() => {
  localStorage.clear()
})

function renderTick(props: Parameters<typeof MaskedAxisTick>[0]) {
  return render(
    <svg>
      <MaskedAxisTick {...props} />
    </svg>,
  )
}

describe('MaskedAxisTick', () => {
  it('formats a money value compactly by default', () => {
    renderTick({ payload: { value: 1234 }, currency: 'USD' })
    expect(screen.getByText('$1.2K')).toBeInTheDocument()
  })

  it('formats a percent value', () => {
    renderTick({ payload: { value: 0.164 }, valueType: 'percent' })
    expect(screen.getByText('16.4%')).toBeInTheDocument()
  })

  it('renders a non-numeric payload value as-is', () => {
    renderTick({ payload: { value: 'Oct' } })
    expect(screen.getByText('Oct')).toBeInTheDocument()
  })

  it('masks the value while privacy mode is on', () => {
    localStorage.setItem('privacyMode', 'true')
    renderTick({ payload: { value: 1234 }, currency: 'USD' })
    expect(screen.queryByText('$1.2K')).not.toBeInTheDocument()
    expect(screen.getByText('•••••')).toBeInTheDocument()
  })

  it('positions the <text> element from x/y/textAnchor', () => {
    renderTick({ x: 10, y: 20, textAnchor: 'end', payload: { value: 5 } })
    const text = screen.getByText('$5')
    expect(text).toHaveAttribute('x', '10')
    expect(text).toHaveAttribute('y', '20')
    expect(text).toHaveAttribute('text-anchor', 'end')
  })
})
