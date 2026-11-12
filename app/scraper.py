"""报价站抓取与解析：分页列表 + 套餐详情。

页面结构随应用提供（见 ../quote-site），解析器依赖页面上的稳定数据属性：
- 列表页：article.plan-card[data-plan-id] 内含 a.plan-link 与 region 标签
- 分页：nav[data-pagination] 内 a[rel=next]
- 详情页：main[data-page=plan-detail]，费率表 table.rate-table[data-rate-kind]，
  档位 tr[data-rate-tier] 携带起止电量与费率数据属性

费率采集约定（与资格判定、计费的衔接）：
- 公布档位费率留空/不可解析 -> RateTier.rate_cny_per_kwh=None，并登记 missing_tier_rate；
- 费率为 inf/-inf/nan 等非有限数 -> 同样置 None，并登记 invalid_rate_value（inf 不进入计费数据）；
- 问题档位不会被静默丢弃，保证其后的档位区间仍可参与结构与超档校验；
- 所有问题汇总在 PlanDetail.rate_issues，由 billing.check_eligibility 排除整份套餐，
  排除原因与详情来源（detail_url）一并返回给调用方。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .fetcher import FetchError, fetch_url, join_url
from .html_dom import Element, parse_html

MAX_LIST_PAGES = 50  # 分页安全上限，防止异常站点造成无限翻页


@dataclass
class RateTier:
    index: int
    lower_kwh: float
    upper_kwh: float | None  # None 表示无上限
    # None 表示该公布档位缺少可用费率（留空或不是有限数），由资格判定排除整份套餐。
    rate_cny_per_kwh: float | None


@dataclass
class RateParseIssue:
    """费率采集阶段记录的单个问题档位，供资格判定生成排除原因与详情。"""

    rate_kind: str  # peak | valley
    tier_index: int
    issue: str  # missing_tier_rate | invalid_rate_value | invalid_tier_structure
    message: str


@dataclass
class PlanDetail:
    plan_id: str
    name: str
    supplier: str | None
    regions: list[str]
    base_fee_cny: float
    peak_tiers: list[RateTier]
    valley_tiers: list[RateTier]
    detail_url: str
    list_url: str
    rate_issues: list[RateParseIssue] = field(default_factory=list)


@dataclass
class PlanListEntry:
    plan_id: str
    detail_url: str
    list_url: str
    name: str | None = None


@dataclass
class CrawlFailure:
    url: str
    stage: str  # list | detail
    reason: str
    status: int | None = None


@dataclass
class CrawlResult:
    plans: list[PlanDetail] = field(default_factory=list)
    failures: list[CrawlFailure] = field(default_factory=list)
    visited_list_urls: list[str] = field(default_factory=list)


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_list_page(markup: str, page_url: str) -> tuple[list[PlanListEntry], str | None]:
    root = parse_html(markup)
    entries: list[PlanListEntry] = []
    for card in root.find_all(tag="article", attr="data-plan-id"):
        plan_id = card.get("data-plan-id") or ""
        link = card.find(tag="a", attr="class", attr_value="plan-link")
        href = link.get("href") if link is not None else None
        if not plan_id or not href:
            continue
        name = link.text or None
        entries.append(
            PlanListEntry(
                plan_id=plan_id,
                detail_url=join_url(page_url, href),
                list_url=page_url,
                name=name,
            )
        )
    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = join_url(page_url, next_link.get("href")) if next_link is not None and next_link.get("href") else None
    return entries, next_url


def _parse_tiers(table: Element, rate_kind: str) -> tuple[list[RateTier], list[RateParseIssue]]:
    """解析一张费率表，同时逐档记录费率/结构问题。

    与旧实现不同：缺费率或费率不是有限数的公布档位不再静默丢弃，
    而是以 rate=None 保留在表中并登记问题，确保资格判定能排除整份套餐，
    且问题档位之后的档位区间不会被误判为“超已公布档位”。
    """
    tiers: list[RateTier] = []
    issues: list[RateParseIssue] = []
    for idx, row in enumerate(table.find_all(tag="tr", attr="data-rate-tier"), start=1):
        lower_raw = row.get("data-tier-from-kwh")
        upper = _parse_float(row.get("data-tier-to-kwh"))  # 空属性 -> None（无上限）
        rate_raw = row.get("data-rate-cny-per-kwh")
        lower = _parse_float(lower_raw)
        rate = _parse_float(rate_raw)
        if lower is None:
            issues.append(
                RateParseIssue(
                    rate_kind,
                    idx,
                    "invalid_tier_structure",
                    f"第 {idx} 档缺少可解析的起始电量（data-tier-from-kwh={lower_raw!r}）",
                )
            )
            continue
        if rate is None:
            # 留空或内容无法解析为数字：公布档位缺费率。
            issues.append(
                RateParseIssue(
                    rate_kind,
                    idx,
                    "missing_tier_rate",
                    f"第 {idx} 档（{lower:g} kWh 起）未公布费率（data-rate-cny-per-kwh={rate_raw!r}）",
                )
            )
        elif not math.isfinite(rate):
            # inf / -inf / nan 均不是可计费的有限费率。
            issues.append(
                RateParseIssue(
                    rate_kind,
                    idx,
                    "invalid_rate_value",
                    f"第 {idx} 档（{lower:g} kWh 起）费率不是有限数：{rate_raw!r}",
                )
            )
            rate = None
        tiers.append(
            RateTier(index=idx, lower_kwh=lower, upper_kwh=upper, rate_cny_per_kwh=rate)
        )
    return tiers, issues


def parse_detail_page(markup: str, detail_url: str, list_url: str) -> PlanDetail:
    root = parse_html(markup)
    main = root.find(tag="main", attr="data-page", attr_value="plan-detail")
    if main is None:
        raise ValueError("详情页缺少 plan-detail 主容器")

    plan_id = main.get("data-plan-id") or ""
    name_node = main.find(attr="data-plan-name")
    name = name_node.text if name_node is not None else plan_id
    supplier_node = main.find(attr="data-supplier")
    supplier = supplier_node.text if supplier_node is not None else None
    regions = [tag.get("data-region", "") for tag in main.find_all(attr="data-region")]
    regions = [r for r in regions if r]
    base_fee_node = main.find(attr="data-base-fee-cny")
    base_fee = _parse_float(base_fee_node.get("data-base-fee-cny")) if base_fee_node is not None else None

    rate_issues: list[RateParseIssue] = []
    peak_tiers: list[RateTier] = []
    valley_tiers: list[RateTier] = []
    for table in main.find_all(tag="table", attr="class", attr_value="rate-table"):
        kind = table.get("data-rate-kind")
        if kind not in ("peak", "valley"):
            continue
        tiers, issues = _parse_tiers(table, kind)
        rate_issues.extend(issues)
        if kind == "peak":
            peak_tiers = tiers
        else:
            valley_tiers = tiers

    return PlanDetail(
        plan_id=plan_id,
        name=name,
        supplier=supplier,
        regions=regions,
        # 缺失或非有限的月基本费统一以 NaN 表示，由资格判定排除并说明原因。
        base_fee_cny=base_fee if base_fee is not None and math.isfinite(base_fee) else float("nan"),
        peak_tiers=peak_tiers,
        valley_tiers=valley_tiers,
        detail_url=detail_url,
        list_url=list_url,
        rate_issues=rate_issues,
    )


def crawl_quote_site(list_url: str, timeout: float = 8.0) -> CrawlResult:
    """从报价列表页入口开始，抓取全部分页与每个套餐详情。"""
    result = CrawlResult()
    seen_list_urls: set[str] = set()
    entries: list[PlanListEntry] = []
    current: str | None = list_url

    while current and current not in seen_list_urls and len(seen_list_urls) < MAX_LIST_PAGES:
        seen_list_urls.add(current)
        result.visited_list_urls.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            result.failures.append(CrawlFailure(url=current, stage="list", reason=exc.reason, status=exc.status))
            break
        page_entries, next_url = parse_list_page(page.text, page.url)
        entries.extend(page_entries)
        current = next_url

    # 同一详情地址在多页中重复出现时只抓一次。
    seen_detail: set[str] = set()
    for entry in entries:
        if entry.detail_url in seen_detail:
            continue
        seen_detail.add(entry.detail_url)
        try:
            page = fetch_url(entry.detail_url, timeout=timeout)
        except FetchError as exc:
            result.failures.append(
                CrawlFailure(url=entry.detail_url, stage="detail", reason=exc.reason, status=exc.status)
            )
            continue
        try:
            result.plans.append(parse_detail_page(page.text, page.url, entry.list_url))
        except ValueError as exc:
            result.failures.append(CrawlFailure(url=entry.detail_url, stage="detail", reason=str(exc)))

    return result
