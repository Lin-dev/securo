"""frontend/src/types/wealth.ts mirrors app/schemas/wealth.py field for field (fork).

Two hand-written copies of one contract drift silently: the backend renames a
field, the page reads `undefined` and renders a dash. This reads the
TypeScript interfaces and compares their field names with the Pydantic models.
"""
import re
from pathlib import Path

import pytest

from app.schemas import wealth as schemas

_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "wealth.ts"

# TypeScript interface -> Pydantic model
_PAIRS = {
    "WealthCoverage": schemas.Coverage,
    "WealthAccountAnnotation": schemas.AccountAnnotation,
    "WealthProjectionSettings": schemas.ProjectionSettings,
    "WealthSettingsValues": schemas.WealthSettingsValues,
    "WealthAccountSettingRow": schemas.AccountSettingRow,
    "WealthIncomeCategoryRow": schemas.IncomeCategoryRow,
    "WealthOneOffRow": schemas.OneOffRow,
    "WealthSettingsRead": schemas.WealthSettingsRead,
    "WealthSettingsUpdate": schemas.WealthSettingsUpdate,
    "WealthGoalProgress": schemas.GoalProgress,
    "WealthSummary": schemas.WealthSummary,
    "WealthNetWorthPoint": schemas.NetWorthPoint,
    "WealthNetWorthHistory": schemas.NetWorthHistory,
    "WealthSavingsMonth": schemas.SavingsMonth,
    "WealthSavingsRateSeries": schemas.SavingsRateSeries,
    "WealthProjectionPathPoint": schemas.ProjectionPathPoint,
    "WealthProjectionScenario": schemas.ProjectionScenario,
    "WealthProjectionInputs": schemas.ProjectionInputs,
    "WealthLeverResult": schemas.LeverResult,
    "WealthFiFigures": schemas.FiFigures,
    "WealthProjection": schemas.WealthProjection,
    "WealthCashFlow": schemas.CashFlow,
    "WealthTopControllable": schemas.TopControllable,
    "WealthThisMonthMove": schemas.ThisMonthMove,
    "WealthLevers": schemas.WealthLevers,
}


def _interfaces(source: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, body in re.findall(r"export interface (\w+) \{(.*?)\n\}", source, re.S):
        out[name] = set(re.findall(r"^\s+(\w+)\??:", body, re.M))
    return out


@pytest.mark.skipif(not _TS.exists(), reason="frontend sources not present")
def test_every_wealth_interface_matches_its_model():
    interfaces = _interfaces(_TS.read_text(encoding="utf-8"))
    assert set(interfaces) == set(_PAIRS), "interfaces and pairs differ"
    mismatched = {
        name: {"ts_only": sorted(fields - set(_PAIRS[name].model_fields)), "py_only": sorted(set(_PAIRS[name].model_fields) - fields)}
        for name, fields in interfaces.items()
        if fields != set(_PAIRS[name].model_fields)
    }
    assert mismatched == {}


@pytest.mark.skipif(not _TS.exists(), reason="frontend sources not present")
def test_bucket_and_lever_lists_match():
    source = _TS.read_text(encoding="utf-8")
    ts_buckets = re.search(r"WEALTH_BUCKETS = \[(.*?)\] as const", source, re.S)
    assert ts_buckets is not None
    assert re.findall(r"'(\w+)'", ts_buckets.group(1)) == list(schemas.BUCKETS)
    ts_levers = re.search(r"export type WealthLeverKey =(.*?)\n\n", source, re.S)
    assert ts_levers is not None
    assert set(re.findall(r"'(\w+)'", ts_levers.group(1))) == set(schemas.LeverKey.__args__)
