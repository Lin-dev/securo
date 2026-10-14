/**
 * Spike for the stacked net-worth chart that slice S2 will build (not this
 * slice): does recharts 3.7's `<AreaChart stackOffset="sign">` actually
 * stack negative values (debt) below the zero line and positive values
 * (assets) above it, rather than just subtracting debt's height from
 * assets' running stack total (which `stackOffset="none"`, the default,
 * does — and which would draw debt as a shorter *positive* region instead
 * of a region below zero)?
 *
 * This is a verification spike, not a shipped kit component — K0 does not
 * build the stacked net-worth chart. See the plan's P8 / "recharts 3.7
 * spike" note and the known `activePayload` bug already found elsewhere in
 * this recharts version.
 */
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { Area, AreaChart, ReferenceLine, ResponsiveContainer } from 'recharts'

interface Row {
  month: string
  assets: number
  debt: number
}

const data: Row[] = [
  { month: 'Jul', assets: 500, debt: -100 },
  { month: 'Aug', assets: 520, debt: -90 },
  { month: 'Sep', assets: 540, debt: -80 },
]

function renderStackedChart() {
  return render(
    <ResponsiveContainer width={400} height={200}>
      <AreaChart data={data} stackOffset="sign">
        <ReferenceLine y={0} stroke="red" className="zero-line" />
        <Area
          type="linear"
          dataKey="assets"
          stackId="networth"
          stroke="green"
          fill="green"
          isAnimationActive={false}
          dot={{ r: 4, className: 'assets-dot' }}
        />
        <Area
          type="linear"
          dataKey="debt"
          stackId="networth"
          stroke="blue"
          fill="blue"
          isAnimationActive={false}
          dot={{ r: 4, className: 'debt-dot' }}
        />
      </AreaChart>
    </ResponsiveContainer>,
  )
}

describe('recharts 3.7 spike: AreaChart stackOffset="sign"', () => {
  it('stacks the positive series above the zero line and the negative series below it', () => {
    const { container } = renderStackedChart()

    const zeroLine = container.querySelector('.zero-line line')
    expect(zeroLine).not.toBeNull()
    const zeroY = Number(zeroLine!.getAttribute('y1'))
    expect(Number.isFinite(zeroY)).toBe(true)

    const assetsDots = [...container.querySelectorAll('.assets-dot')]
    const debtDots = [...container.querySelectorAll('.debt-dot')]

    expect(assetsDots).toHaveLength(data.length)
    expect(debtDots).toHaveLength(data.length)

    for (const dot of assetsDots) {
      const cy = Number(dot.getAttribute('cy'))
      // SVG y grows downward: "above the zero line" means a smaller y than zeroY.
      expect(cy).toBeLessThan(zeroY)
    }

    for (const dot of debtDots) {
      const cy = Number(dot.getAttribute('cy'))
      // "Below the zero line" means a larger y than zeroY.
      expect(cy).toBeGreaterThan(zeroY)
    }
  })

  it('does NOT reproduce with the default stackOffset ("none"), confirming "sign" is what fixes it', () => {
    const { container } = render(
      <ResponsiveContainer width={400} height={200}>
        <AreaChart data={data}>
          <ReferenceLine y={0} stroke="red" className="zero-line" />
          <Area
            type="linear"
            dataKey="assets"
            stackId="networth"
            isAnimationActive={false}
            dot={{ r: 4, className: 'assets-dot' }}
          />
          <Area
            type="linear"
            dataKey="debt"
            stackId="networth"
            isAnimationActive={false}
            dot={{ r: 4, className: 'debt-dot' }}
          />
        </AreaChart>
      </ResponsiveContainer>,
    )

    const zeroLine = container.querySelector('.zero-line line')
    const zeroY = Number(zeroLine!.getAttribute('y1'))
    const debtDots = [...container.querySelectorAll('.debt-dot')]

    // Without "sign", the negative series is subtracted from the running
    // total instead of dropping below zero — it lands at (assets + debt),
    // which is still positive here, i.e. still above the zero line.
    const allAboveZero = debtDots.every((dot) => Number(dot.getAttribute('cy')) < zeroY)
    expect(allAboveZero).toBe(true)
  })
})
