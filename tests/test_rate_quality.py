"""费率质量回归测试：公布档位缺费率 / 费率非有限数的采集、资格与计费衔接。

覆盖两类本地报价站样例：
- P008：峰段末档费率留空（缺费率），即使所有月份用量都不进入该档也必须排除；
- P009：峰段末档费率写成 inf（非有限数），用量进入该档时旧实现会抛
  decimal.InvalidOperation 导致整个比价 500，新实现必须在资格判定阶段排除，
  且其余完整套餐照常返回年度费用与排序。

运行（在仓库根目录）：
    python3 -I -m unittest discover -s tests -v
"""
from __future__ import annotations

import math
import unittest
from pathlib import Path

from app.billing import annual_total, bill_plan, check_eligibility
from app.scraper import RateTier, crawl_quote_site, parse_detail_page

ROOT = Path(__file__).resolve().parent.parent
LIST_URL = (ROOT / "quote-site" / "index.html").as_uri()

USAGE = [
    {"month": 1, "peak_kwh": 260, "valley_kwh": 360},
    {"month": 2, "peak_kwh": 240, "valley_kwh": 340},
    {"month": 3, "peak_kwh": 280, "valley_kwh": 380},
    {"month": 4, "peak_kwh": 300, "valley_kwh": 400},
    {"month": 5, "peak_kwh": 330, "valley_kwh": 430},
    {"month": 6, "peak_kwh": 380, "valley_kwh": 480},
    {"month": 7, "peak_kwh": 420, "valley_kwh": 520},
    {"month": 8, "peak_kwh": 410, "valley_kwh": 510},
    {"month": 9, "peak_kwh": 360, "valley_kwh": 460},
    {"month": 10, "peak_kwh": 310, "valley_kwh": 410},
    {"month": 11, "peak_kwh": 280, "valley_kwh": 380},
    {"month": 12, "peak_kwh": 270, "valley_kwh": 370},
]

# 低用量：每月峰电 200 kWh，绝不进入 P008/P009 的问题末档（450 kWh 起）。
LOW_USAGE = [{"month": month, "peak_kwh": 200.0, "valley_kwh": 250.0} for month in range(1, 13)]
# 高用量：每月峰电 500 kWh，必然进入 450 kWh 起的问题末档。
HIGH_USAGE = [{"month": month, "peak_kwh": 500.0, "valley_kwh": 300.0} for month in range(1, 13)]


def _plans_by_id() -> dict:
    return {plan.plan_id: plan for plan in crawl_quote_site(LIST_URL).plans}


class ScrapeRateQualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plans = _plans_by_id()

    def test_blank_rate_tier_is_kept_and_flagged(self) -> None:
        p008 = self.plans["P008"]
        # 三个档位全部保留，末档（450 起、无上限）不能被静默丢弃
        self.assertEqual([t.lower_kwh for t in p008.peak_tiers], [0.0, 250.0, 450.0])
        self.assertIsNone(p008.peak_tiers[-1].upper_kwh)
        self.assertIsNone(p008.peak_tiers[-1].rate_cny_per_kwh)
        issues = p008.rate_issues
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].rate_kind, "peak")
        self.assertEqual(issues[0].issue, "missing_tier_rate")
        self.assertEqual(issues[0].tier_index, 3)

    def test_inf_rate_is_normalized_to_none_and_flagged(self) -> None:
        p009 = self.plans["P009"]
        for tier in p009.peak_tiers:
            # 任何进入计费数据结构的费率都必须是有限数或 None，inf 不得残留
            self.assertTrue(tier.rate_cny_per_kwh is None or math.isfinite(tier.rate_cny_per_kwh))
        self.assertIsNone(p009.peak_tiers[-1].rate_cny_per_kwh)
        self.assertEqual([i.issue for i in p009.rate_issues], ["invalid_rate_value"])

    def test_nan_and_unparseable_rates_are_rejected_at_parse_layer(self) -> None:
        markup = """
        <html><body><main data-page="plan-detail" data-plan-id="X">
          <h2 data-plan-name>X</h2><span data-supplier>S</span>
          <span data-region="上海">上海</span>
          <strong data-base-fee-cny="30.00">30</strong>
          <table class="rate-table" data-rate-kind="peak"><tbody>
            <tr data-rate-tier data-tier-from-kwh="0" data-tier-to-kwh="100" data-rate-cny-per-kwh="nan"></tr>
            <tr data-rate-tier data-tier-from-kwh="100" data-tier-to-kwh="" data-rate-cny-per-kwh="待定"></tr>
          </tbody></table>
          <table class="rate-table" data-rate-kind="valley"><tbody>
            <tr data-rate-tier data-tier-from-kwh="0" data-tier-to-kwh="" data-rate-cny-per-kwh="0.4"></tr>
          </tbody></table>
        </main></body></html>
        """
        plan = parse_detail_page(markup, "file:///detail/x.html", LIST_URL)
        self.assertEqual([i.issue for i in plan.rate_issues],
                         ["invalid_rate_value", "missing_tier_rate"])
        self.assertTrue(all(t.rate_cny_per_kwh is None for t in plan.peak_tiers))
        result = check_eligibility(plan, "上海", LOW_USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "missing_tier_rate")
        self.assertIn("file:///detail/x.html", result.detail)


class EligibilityRateQualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plans = _plans_by_id()

    def test_blank_rate_excludes_plan_even_when_usage_never_enters_tier(self) -> None:
        result = check_eligibility(self.plans["P008"], "上海", LOW_USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "missing_tier_rate")
        # 排除说明必须带详情来源，方便采购员复核
        self.assertIn("详情来源", result.detail)
        self.assertTrue(result.detail.endswith("p008.html"))
        self.assertIn("峰段", result.detail)

    def test_inf_rate_excludes_plan_before_billing(self) -> None:
        result = check_eligibility(self.plans["P009"], "上海", HIGH_USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "invalid_rate_value")
        self.assertTrue(result.detail.endswith("p009.html"))

    def test_inf_rate_excludes_plan_even_when_usage_never_enters_tier(self) -> None:
        result = check_eligibility(self.plans["P009"], "上海", LOW_USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "invalid_rate_value")

    def test_blank_tier_is_not_misreported_as_usage_exceeds_tiers(self) -> None:
        # 旧实现吞掉末档后会把 450 kWh 封顶，低用量（200 kWh）本不应触发超档；
        # 这里同时确认新实现给出的是缺费率原因而非超档原因。
        result = check_eligibility(self.plans["P008"], "上海", LOW_USAGE)
        self.assertNotEqual(result.reason, "usage_exceeds_published_tiers")

    def test_complete_plans_remain_eligible_and_keep_ranking(self) -> None:
        ranked = []
        for plan_id in ("P001", "P002", "P003"):
            plan = self.plans[plan_id]
            self.assertTrue(check_eligibility(plan, "上海", USAGE).eligible)
            ranked.append((plan_id, annual_total(bill_plan(plan, USAGE))))
        ranked.sort(key=lambda item: item[1])
        self.assertEqual([pid for pid, _ in ranked], ["P003", "P001", "P002"])
        self.assertEqual([total for _, total in ranked], [6899.40, 7247.40, 7842.00])


class BillingGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plans = _plans_by_id()

    def test_billing_invalid_rate_raises_value_error_not_decimal_crash(self) -> None:
        from decimal import InvalidOperation

        for plan_id in ("P008", "P009"):
            with self.subTest(plan_id=plan_id):
                with self.assertRaises(ValueError):
                    bill_plan(self.plans[plan_id], HIGH_USAGE)
            # 显式确认不会再出现旧版 decimal.InvalidOperation 泄漏
            try:
                bill_plan(self.plans[plan_id], HIGH_USAGE)
            except InvalidOperation:  # pragma: no cover - 防御性断言
                self.fail("非有限费率导致 decimal.InvalidOperation 泄漏到调用方")
            except ValueError:
                pass

    def test_billing_rejects_non_finite_base_fee(self) -> None:
        plan = self.plans["P001"]
        plan.base_fee_cny = float("inf")
        with self.assertRaises(ValueError):
            bill_plan(plan, USAGE)

    def test_synthetic_tier_with_none_rate_is_rejected(self) -> None:
        plan = self.plans["P001"]
        plan.peak_tiers[-1] = RateTier(
            index=plan.peak_tiers[-1].index,
            lower_kwh=plan.peak_tiers[-1].lower_kwh,
            upper_kwh=None,
            rate_cny_per_kwh=None,
        )
        self.assertFalse(check_eligibility(plan, "上海", LOW_USAGE).eligible)
        with self.assertRaises(ValueError):
            bill_plan(plan, HIGH_USAGE)


class CompareApiTest(unittest.TestCase):
    """通过 FastAPI TestClient 验证完整 HTTP 编排（需要 fastapi/httpx 依赖）。"""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # 标准库离线环境下跳过，Docker 镜像内会执行
            self.skipTest("fastapi 未安装，跳过 API 级测试")
        from app.main import app

        self.client = TestClient(app)
        self.payload = {"list_url": LIST_URL, "region": "上海", "monthly_usage": USAGE}

    def test_compare_endpoint_excludes_bad_quotes_and_keeps_ranking(self) -> None:
        resp = self.client.post("/api/v1/compare-plans", json=self.payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        ranked = body["ranked_plans"]
        self.assertEqual([item["plan_id"] for item in ranked], ["P003", "P001", "P002"])
        self.assertEqual([item["annual_total_cny"] for item in ranked],
                         [6899.40, 7247.40, 7842.00])
        self.assertEqual([item["rank"] for item in ranked], [1, 2, 3])
        for item in ranked:
            self.assertEqual(len(item["monthly_bills"]), 12)
            self.assertTrue(item["detail_url"].endswith(f"{item['plan_id'].lower()}.html"))

        excluded = {item["plan_id"]: item for item in body["excluded_plans"]}
        self.assertEqual(excluded["P008"]["reason"], "missing_tier_rate")
        self.assertTrue(excluded["P008"]["detail_url"].endswith("p008.html"))
        self.assertIn("详情来源", excluded["P008"]["detail"])
        self.assertEqual(excluded["P009"]["reason"], "invalid_rate_value")
        self.assertTrue(excluded["P009"]["detail_url"].endswith("p009.html"))
        # 问题套餐绝不能进入排名首位
        self.assertNotIn("P008", [item["plan_id"] for item in ranked])
        self.assertNotIn("P009", [item["plan_id"] for item in ranked])

        # P007 详情 404：失败地址必须列出，最低价结论标为待核实
        failures = body["failures"]
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]["url"].endswith("p007.html"))
        self.assertEqual(failures[0]["stage"], "detail")
        self.assertEqual(failures[0]["status"], 404)
        self.assertEqual(body["conclusion"]["verification_status"], "pending_verification")
        self.assertEqual(body["conclusion"]["lowest_plan_id"], "P003")

    def test_compare_endpoint_verified_when_no_crawl_failure(self) -> None:
        # 只给单页列表且所有详情存在的临时站点，验证全成功时结论为 verified
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "plans").mkdir()
            cards = []
            for plan_id, src in (("P001", "p001.html"), ("P002", "p002.html"), ("P003", "p003.html")):
                markup = (ROOT / "quote-site" / "plans" / src).read_text(encoding="utf-8")
                (site / "plans" / src).write_text(markup, encoding="utf-8")
                cards.append(
                    f'<article class="plan-card" data-plan-id="{plan_id}">'
                    f'<a class="plan-link" href="plans/{src}">{plan_id}</a></article>'
                )
            (site / "index.html").write_text(
                '<html><body><main data-page="plan-list">' + "".join(cards) + "</main></body></html>",
                encoding="utf-8",
            )
            payload = dict(self.payload)
            payload["list_url"] = (site / "index.html").as_uri()
            resp = self.client.post("/api/v1/compare-plans", json=payload)
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["failures"], [])
            self.assertEqual(body["conclusion"]["verification_status"], "verified")
            self.assertEqual(len(body["ranked_plans"]), 3)


if __name__ == "__main__":
    unittest.main()
