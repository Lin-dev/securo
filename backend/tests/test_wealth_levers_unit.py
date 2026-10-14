"""Pure lever figures (fork, plan P3): edges that the API scenarios do not reach."""
from app.services.wealth.figures import AnnualFigure, MedianMonth, TypicalFigures
from app.services.wealth.levers import compute


def _typical(full_months: int, expenses_median=None) -> TypicalFigures:
    zero = AnnualFigure(all=0.0, typical=0.0)
    return TypicalFigures(
        coverage={}, income=zero, expenses=zero, direct=zero, invested=zero, saving=zero,
        median_month=MedianMonth(cash_flow_after_investing=None, saving=None, expenses=expenses_median, full_months=full_months),
    )


def test_no_spending_history_calls_nothing_idle():
    figures = compute(cash=20000.0, median_monthly_spend=None, buffer_months=3, typical=_typical(0),
                      category_monthly={}, currency="USD")
    assert (figures.idle_cash, figures.buffer, figures.cash_cushion_months) == (0.0, 0.0, None)
    assert figures.investing_exceeds_saving is False


def test_idle_cash_is_cash_above_the_buffer():
    figures = compute(cash=20000.0, median_monthly_spend=4000.0, buffer_months=3, typical=_typical(2, 4000.0),
                      category_monthly={}, currency="USD")
    assert (figures.buffer, figures.idle_cash, figures.cash_cushion_months) == (12000.0, 8000.0, 5.0)
