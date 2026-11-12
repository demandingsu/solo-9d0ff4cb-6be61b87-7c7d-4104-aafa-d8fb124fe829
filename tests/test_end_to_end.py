"""端到端离线测试：直接以 file:// 协议抓取随仓库提供的本地报价站。

运行（在仓库根目录）：
    python3 -I -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from app.billing import annual_total, bill_plan, check_eligibility
from app.scraper import crawl_quote_site

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


class ScraperTest(unittest.TestCase):
    def test_crawls_both_list_pages_and_all_available_details(self) -> None:
        result = crawl_quote_site(LIST_URL)
        self.assertEqual(result.visited_list_urls[0], LIST_URL)
        self.assertEqual(len(result.visited_list_urls), 2, "应跟随 next 链接抓取两页列表")
        plan_ids = {p.plan_id for p in result.plans}
        self.assertEqual(plan_ids, {"P001", "P002", "P003", "P004", "P005", "P006"})
        # P007 详情页故意缺失 -> 404 失败记录
        self.assertEqual(len(result.failures), 1)
        failure = result.failures[0]
        self.assertEqual(failure.stage, "detail")
        self.assertEqual(failure.status, 404)
        self.assertTrue(failure.url.endswith("p007.html"))

    def test_detail_parsing_rates_and_regions(self) -> None:
        result = crawl_quote_site(LIST_URL)
        p001 = next(p for p in result.plans if p.plan_id == "P001")
        self.assertEqual(p001.base_fee_cny, 60.0)
        self.assertIn("上海", p001.regions)
        self.assertEqual(len(p001.peak_tiers), 3)
        self.assertEqual(len(p001.valley_tiers), 2)
        # 最后一档无上限
        self.assertIsNone(p001.peak_tiers[-1].upper_kwh)
        # P005 缺谷段费率
        p005 = next(p for p in result.plans if p.plan_id == "P005")
        self.assertEqual(p005.valley_tiers, [])
        # P006 两表均为有封顶单档
        p006 = next(p for p in result.plans if p.plan_id == "P006")
        self.assertEqual(p006.peak_tiers[0].upper_kwh, 150.0)
        self.assertEqual(p006.valley_tiers[0].upper_kwh, 200.0)


class EligibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plans = {p.plan_id: p for p in crawl_quote_site(LIST_URL).plans}

    def test_region_filter(self) -> None:
        p004 = self.plans["P004"]
        result = check_eligibility(p004, "上海", USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "region_not_applicable")

    def test_missing_valley_rates_excluded(self) -> None:
        result = check_eligibility(self.plans["P005"], "上海", USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "missing_rates")

    def test_usage_exceeding_published_tiers_excluded(self) -> None:
        result = check_eligibility(self.plans["P006"], "上海", USAGE)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "usage_exceeds_published_tiers")
        self.assertIn("第 1 月", result.detail or "")

    def test_complete_plans_eligible(self) -> None:
        for plan_id in ("P001", "P002", "P003"):
            with self.subTest(plan_id=plan_id):
                self.assertTrue(check_eligibility(self.plans[plan_id], "上海", USAGE).eligible)


class BillingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plans = {p.plan_id: p for p in crawl_quote_site(LIST_URL).plans}

    def test_tiered_billing_manual_calculation_p002(self) -> None:
        # P002：月基本费 45；峰 0-150@1.12，150+@1.25；谷 0-200@0.48，200+@0.60
        # 第 1 月：峰 260 / 谷 360
        #   峰：150*1.12 + 110*1.25 = 168 + 137.5 = 305.50
        #   谷：200*0.48 + 160*0.60 = 96 + 96 = 192.00
        bills = bill_plan(self.plans["P002"], USAGE)
        jan = bills[0]
        self.assertEqual(jan.peak_energy_fee_cny, 305.50)
        self.assertEqual(jan.valley_energy_fee_cny, 192.00)
        self.assertEqual(jan.base_fee_cny, 45.00)
        self.assertEqual(jan.monthly_total_cny, 542.50)
        self.assertEqual(len(jan.peak_charges), 2)
        self.assertEqual(jan.peak_charges[0].charged_kwh, 150.0)
        self.assertEqual(jan.peak_charges[1].charged_kwh, 110.0)

    def test_annual_total_p001(self) -> None:
        bills = bill_plan(self.plans["P001"], USAGE)
        # 手算第 1 月：峰 200*1.05 + 60*1.18 = 210 + 70.80 = 280.80
        #             谷 300*0.42 + 60*0.55 = 126 + 33 = 159.00
        #             月合计 = 280.80 + 159.00 + 60 = 499.80
        self.assertEqual(bills[0].monthly_total_cny, 499.80)
        # 与计费引擎无关的独立纯算术核算全年总额
        independent = 0.0
        for item in USAGE:
            pk, vk = item["peak_kwh"], item["valley_kwh"]
            peak_fee = (min(pk, 200) * 1.05
                        + max(0, min(pk, 400) - 200) * 1.18
                        + max(0, pk - 400) * 1.32)
            valley_fee = min(vk, 300) * 0.42 + max(0, vk - 300) * 0.55
            independent += peak_fee + valley_fee + 60.0
        self.assertAlmostEqual(annual_total(bills), round(independent, 2), places=2)
        self.assertEqual(annual_total(bills), 7247.40)
        self.assertEqual(len(bills), 12)

    def test_ranking_order_and_failure_flag(self) -> None:
        crawl = crawl_quote_site(LIST_URL)
        eligible = []
        for plan in crawl.plans:
            if check_eligibility(plan, "上海", USAGE).eligible:
                eligible.append((plan.plan_id, annual_total(bill_plan(plan, USAGE))))
        eligible.sort(key=lambda item: item[1])
        # 复核金额：P003=6899.40 < P001=7247.40 < P002=7842.00
        self.assertEqual([pid for pid, _ in eligible], ["P003", "P001", "P002"])
        self.assertEqual([total for _, total in eligible], [6899.40, 7247.40, 7842.00])
        self.assertTrue(crawl.failures)  # P007 404 -> 最低价须待核实


if __name__ == "__main__":
    unittest.main()
