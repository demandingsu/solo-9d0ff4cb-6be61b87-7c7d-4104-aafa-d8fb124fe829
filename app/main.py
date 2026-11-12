"""连锁便利店电价套餐比较 API。

入口：POST /api/v1/compare-plans
流程：抓取报价站分页列表与套餐详情 -> 地区过滤 -> 资格校验
（缺费率 / 超已公布档位不得参与）-> 峰谷逐档计费 + 月基本费 -> 年度汇总排序。
任何列表或详情抓取失败都会记录失败地址，并把最低价结论标记为“待核实”。
"""
from __future__ import annotations

from fastapi import FastAPI

from .billing import annual_total, bill_plan, check_eligibility
from .schemas import (
    CompareRequest,
    CompareResponse,
    CrawlFailureOut,
    ExcludedPlanOut,
    MonthlyBillOut,
    RankedPlanOut,
    TierChargeOut,
)
from .scraper import crawl_quote_site

app = FastAPI(
    title="连锁便利店电价套餐比较 API",
    version="1.0.0",
    description="按门店峰谷用电预测，比较本地报价站公开电价套餐的年度成本。",
)


def _tier_out(charges) -> list[TierChargeOut]:
    return [TierChargeOut(**vars(charge)) for charge in charges]


def _monthly_out(bills) -> list[MonthlyBillOut]:
    return [
        MonthlyBillOut(
            month=bill.month,
            peak_kwh=bill.peak_kwh,
            valley_kwh=bill.valley_kwh,
            peak_charges=_tier_out(bill.peak_charges),
            valley_charges=_tier_out(bill.valley_charges),
            peak_energy_fee_cny=bill.peak_energy_fee_cny,
            valley_energy_fee_cny=bill.valley_energy_fee_cny,
            base_fee_cny=bill.base_fee_cny,
            monthly_total_cny=bill.monthly_total_cny,
        )
        for bill in bills
    ]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/compare-plans", response_model=CompareResponse)
def compare_plans(request: CompareRequest) -> CompareResponse:
    usage = [item.model_dump() for item in request.monthly_usage]
    crawl_result = crawl_quote_site(request.list_url, timeout=request.timeout_seconds)

    ranked: list[RankedPlanOut] = []
    excluded: list[ExcludedPlanOut] = []

    for plan in crawl_result.plans:
        eligibility = check_eligibility(plan, request.region, usage)
        if not eligibility.eligible:
            excluded.append(
                ExcludedPlanOut(
                    plan_id=plan.plan_id,
                    plan_name=plan.name,
                    supplier=plan.supplier,
                    regions=plan.regions,
                    reason=eligibility.reason or "unknown",
                    detail=eligibility.detail or "",
                    detail_url=plan.detail_url,
                    list_url=plan.list_url,
                )
            )
            continue
        bills = bill_plan(plan, usage)
        ranked.append(
            RankedPlanOut(
                rank=0,  # 排序后统一回填
                plan_id=plan.plan_id,
                plan_name=plan.name,
                supplier=plan.supplier,
                regions=plan.regions,
                annual_total_cny=annual_total(bills),
                detail_url=plan.detail_url,
                list_url=plan.list_url,
                monthly_bills=_monthly_out(bills),
            )
        )

    ranked.sort(key=lambda item: item.annual_total_cny)
    for index, item in enumerate(ranked, start=1):
        item.rank = index

    failures = [
        CrawlFailureOut(url=f.url, stage=f.stage, reason=f.reason, status=f.status)
        for f in crawl_result.failures
    ]

    has_failure = bool(failures)
    if ranked:
        cheapest = ranked[0]
        conclusion = {
            "lowest_plan_id": cheapest.plan_id,
            "lowest_plan_name": cheapest.plan_name,
            "lowest_annual_total_cny": cheapest.annual_total_cny,
            "verification_status": "pending_verification" if has_failure else "verified",
            "note": (
                "存在抓取失败的列表页或套餐详情，最低价结论待核实"
                if has_failure
                else "全部列表页与套餐详情抓取成功，最低价结论已核实"
            ),
        }
    else:
        conclusion = {
            "lowest_plan_id": None,
            "lowest_plan_name": None,
            "lowest_annual_total_cny": None,
            "verification_status": "pending_verification",
            "note": "没有可参与比较的有效套餐，无法给出最低价结论，待核实",
        }

    return CompareResponse(
        request_region=request.region,
        list_url=request.list_url,
        crawl={
            "visited_list_urls": crawl_result.visited_list_urls,
            "plans_crawled": len(crawl_result.plans),
            "eligible_count": len(ranked),
            "excluded_count": len(excluded),
            "failure_count": len(failures),
        },
        conclusion=conclusion,
        ranked_plans=ranked,
        excluded_plans=excluded,
        failures=failures,
    )
