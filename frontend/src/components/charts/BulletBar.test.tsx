import { afterEach, describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'

import { BulletBar } from '@/components/charts/BulletBar'
import { renderWithProviders } from '@/test/utils'

afterEach(() => {
  localStorage.clear()
})

describe('BulletBar', () => {
  it('exposes role="img" with the formatted value and target in its aria-label', () => {
    renderWithProviders(<BulletBar value={692800} target={1000000} currency="USD" />)

    const bar = screen.getByRole('img')
    expect(bar).toHaveAccessibleName('$692,800.00 of a $1,000,000.00 target')
  })

  it('shows the same numbers visibly in a caption', () => {
    renderWithProviders(<BulletBar value={500} target={1000} currency="USD" />)

    expect(screen.getByText('$500.00')).toBeInTheDocument()
    expect(screen.getByText(/Target: \$1,000\.00/)).toBeInTheDocument()
  })

  it('shows a marker caption with a default label when markerLabel is omitted', () => {
    renderWithProviders(<BulletBar value={500} target={1000} marker={700} currency="USD" />)

    expect(screen.getByText(/Marker: \$700\.00/)).toBeInTheDocument()
  })

  it('shows a custom marker label when given', () => {
    renderWithProviders(
      <BulletBar value={500} target={1000} marker={700} markerLabel="Pace" currency="USD" />,
    )

    expect(screen.getByText(/Pace: \$700\.00/)).toBeInTheDocument()
  })

  it('omits the marker caption when no marker is given', () => {
    renderWithProviders(<BulletBar value={500} target={1000} currency="USD" />)

    expect(screen.queryByText(/Marker:/)).not.toBeInTheDocument()
  })

  it('masks every visible and accessible number in privacy mode', () => {
    localStorage.setItem('privacyMode', 'true')

    renderWithProviders(<BulletBar value={500} target={1000} currency="USD" />)

    expect(screen.queryByText('$500.00')).not.toBeInTheDocument()
    const bar = screen.getByRole('img')
    expect(bar.getAttribute('aria-label')).not.toMatch(/\$/)
  })
})
