"""峰谷阶梯计费引擎与套餐资格判定。

规则：
1. 每月峰、谷电量分别沿各自费率表逐档计费（区间为 [下限, 上限)，上限为 None 即无上限）。
2. 每月另加固定月基本费，12 个月合计为年度成本。
3. 套餐缺少任一类费率、月基本费缺失，或任一月峰/谷电量超出已公布档位，
   该套餐不得参与比较，需给出排除原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

from .scraper import PlanDetail, RateTier

TWO_PLACES = Decimal("0.01")


def _d(value: float) -> Decimal:
    return Decimal(str(value))


class ExclusionReason(str, Enum):
    REGION_NOT_APPLICABLE = "region_not_applicable"
    MISSING_RATES = "missing_rates"
    MISSING_BASE_FEE = "missing_base_fee"
    USAGE_EXCEEDS_TIERS = "usage_exceeds_published_tiers"
    INVALID_TIER_STRUCTURE = "invalid_tier_structure"


@dataclass
class TierCharge:
    tier_index: int
    lower_kwh: float
    upper_kwh: float | None
    rate_cny_per_kwh: float
    charged_kwh: float
    charge_cny: float


@dataclass
class MonthlyBill:
    month: int  # 1-12
    peak_kwh: float
    valley_kwh: float
    peak_charges: list[TierCharge]
    valley_charges: list[TierCharge]
    peak_energy_fee_cny: float
    valley_energy_fee_cny: float
    base_fee_cny: float
    monthly_total_cny: float


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str | None  # ExclusionReason 的值
    detail: str | None


@dataclass
class PlanComparison:
    plan_id: str
    plan_name: str
    supplier: str | None
    regions: list[str]
    detail_url: str
    list_url: str
    eligible: bool
    exclusion_reason: str | None
    exclusion_detail: str | None
    annual_total_cny: float | None
    monthly_bills: list[MonthlyBill]


def region_matches(plan_regions: list[str], requested_region: str) -> bool:
    target = requested_region.strip().lower()
    return any(target == region.strip().lower() for region in plan_regions)


def validate_tier_structure(tiers: list[RateTier]) -> str | None:
    """返回 None 表示结构有效，否则返回问题描述。"""
    if not tiers:
        return "费率表为空"
    ordered = sorted(tiers, key=lambda t: t.lower_kwh)
    if ordered[0].lower_kwh != 0:
        return "首档下限不为 0，无法从零电量开始计费"
    for prev, curr in zip(ordered, ordered[1:]):
        if prev.upper_kwh is None:
            return "存在中间档位未设置上限"
        if prev.upper_kwh != curr.lower_kwh:
            return f"档位不连续：{prev.lower_kwh}-{prev.upper_kwh} 与 {curr.lower_kwh} 未衔接"
    return None


def _charge_along_tiers(usage_kwh: float, tiers: list[RateTier]) -> tuple[list[TierCharge], Decimal | None]:
    """逐档计费。返回（各档明细, 总费用）；若电量超出已公布档位，总费用为 None。"""
    remaining = _d(usage_kwh)
    charges: list[TierCharge] = []
    total = Decimal("0")
    for tier in sorted(tiers, key=lambda t: t.lower_kwh):
        lower = _d(tier.lower_kwh)
        upper = _d(tier.upper_kwh) if tier.upper_kwh is not None else None
        if remaining <= 0:
            break
        if upper is None:
            band = remaining  # 无上限档承接全部剩余电量
        else:
            capacity = upper - lower
            band = min(remaining, capacity)
        charge = (band * _d(tier.rate_cny_per_kwh)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        total += charge
        charges.append(
            TierCharge(
                tier_index=tier.index,
                lower_kwh=tier.lower_kwh,
                upper_kwh=tier.upper_kwh,
                rate_cny_per_kwh=tier.rate_cny_per_kwh,
                charged_kwh=float(band),
                charge_cny=float(charge),
            )
        )
        remaining -= band
    if remaining > 0:
        return charges, None
    return charges, total


def check_eligibility(plan: PlanDetail, region: str, monthly_usage: list[dict]) -> EligibilityResult:
    if not region_matches(plan.regions, region):
        return EligibilityResult(False, ExclusionReason.REGION_NOT_APPLICABLE.value,
                                 f"套餐适用地区 {plan.regions} 不包含请求地区“{region}”")
    if not plan.peak_tiers or not plan.valley_tiers:
        missing = []
        if not plan.peak_tiers:
            missing.append("峰段费率")
        if not plan.valley_tiers:
            missing.append("谷段费率")
        return EligibilityResult(False, ExclusionReason.MISSING_RATES.value, "缺少" + "、".join(missing))
    for kind, tiers in (("峰段", plan.peak_tiers), ("谷段", plan.valley_tiers)):
        problem = validate_tier_structure(tiers)
        if problem:
            return EligibilityResult(False, ExclusionReason.INVALID_TIER_STRUCTURE.value, f"{kind}{problem}")
    if plan.base_fee_cny != plan.base_fee_cny:  # NaN 检测
        return EligibilityResult(False, ExclusionReason.MISSING_BASE_FEE.value, "详情页未公布月基本费")

    for item in monthly_usage:
        month = item["month"]
        for kind, usage, tiers in (
            ("峰", item["peak_kwh"], plan.peak_tiers),
            ("谷", item["valley_kwh"], plan.valley_tiers),
        ):
            top = max(tiers, key=lambda t: t.lower_kwh)
            if top.upper_kwh is not None and usage > top.upper_kwh:
                return EligibilityResult(
                    False,
                    ExclusionReason.USAGE_EXCEEDS_TIERS.value,
                    f"第 {month} 月{kind}段用电 {usage} kWh 超出已公布档位上限 {top.upper_kwh} kWh",
                )
    return EligibilityResult(True, None, None)


def bill_plan(plan: PlanDetail, monthly_usage: list[dict]) -> list[MonthlyBill]:
    """对已通过资格校验的套餐逐月计费。"""
    base_fee = _d(plan.base_fee_cny).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    bills: list[MonthlyBill] = []
    for item in monthly_usage:
        peak_charges, peak_total = _charge_along_tiers(item["peak_kwh"], plan.peak_tiers)
        valley_charges, valley_total = _charge_along_tiers(item["valley_kwh"], plan.valley_tiers)
        assert peak_total is not None and valley_total is not None  # 资格校验已排除超档
        month_total = (peak_total + valley_total + base_fee).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        bills.append(
            MonthlyBill(
                month=item["month"],
                peak_kwh=item["peak_kwh"],
                valley_kwh=item["valley_kwh"],
                peak_charges=peak_charges,
                valley_charges=valley_charges,
                peak_energy_fee_cny=float(peak_total),
                valley_energy_fee_cny=float(valley_total),
                base_fee_cny=float(base_fee),
                monthly_total_cny=float(month_total),
            )
        )
    return bills


def annual_total(bills: list[MonthlyBill]) -> float:
    total = sum((_d(b.monthly_total_cny) for b in bills), Decimal("0"))
    return float(total.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))
