# 连锁便利店电价套餐比较 API

能源采购员按门店未来 12 个月的峰谷用电预测，比较公开电价套餐的年度成本，辅助续签决策。

- **Web 框架**：Python 3.11 + FastAPI（`app/main.py`）
- **抓取与计费核心**：仅依赖 Python 标准库（`urllib` + `html.parser` + `decimal`），离线可测
- **本地报价站**：随仓库提供的纯静态站点（`quote-site/`），nginx 容器托管，无需外网
- **部署**：Docker Compose 一键启动 API + 报价站

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与比较编排
│   ├── schemas.py     # 请求/响应 Pydantic 模型
│   ├── scraper.py     # 分页列表 + 套餐详情抓取解析
│   ├── fetcher.py     # http/https/file 统一抓取层与错误归一化
│   ├── html_dom.py    # 标准库实现的轻量 HTML DOM
│   └── billing.py     # 峰谷逐档计费、资格校验、年度汇总
├── quote-site/        # 本地报价站（静态 HTML，虚构演示数据）
│   ├── index.html         # 列表第 1 页（P001-P004，含 next 分页链接）
│   ├── index-2.html       # 列表第 2 页（P005-P007）
│   ├── plans/p001..p006.html
│   ├── 404.html           # p007.html 故意缺失，演示详情抓取失败
│   └── nginx.conf
├── tests/             # 标准库 unittest 离线测试（9 个用例）
├── demo/request.json  # 示例请求（12 个月峰谷用电）
├── Dockerfile
└── docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

- 报价站：http://localhost:8080/index.html
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

发起比较请求（容器内通过 `http://quote-site/index.html` 访问报价站）：

```bash
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request.json
```

## 无 Docker 的离线运行

核心抓取/计费逻辑仅用标准库，可直接运行测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

手动启动报价站与 API（需自行安装 `requirements.txt`）：

```bash
python3 -m http.server 8080 --directory quote-site
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 请求中将 list_url 改为 http://127.0.0.1:8080/index.html
# 也支持 file:// 绝对路径，完全离线：
#   "list_url": "file:///workspace/quote-site/index.html"
```

## 请求格式

`POST /api/v1/compare-plans`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 报价站套餐列表页 URL，支持 `http(s)://` 与 `file://` |
| `region` | 门店地区，需与套餐详情页地区标签精确匹配（如 `上海`） |
| `monthly_usage` | 恰好 12 条记录，每条含 `month`(1-12)、`peak_kwh`、`valley_kwh`（非负） |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 计费与筛选规则

1. **地区过滤**：套餐适用地区不包含请求地区 → 排除（`region_not_applicable`）。
2. **费率完整性**：缺峰段或谷段费率表、费率档位无法解析、月基本费缺失 → 排除
   （`missing_rates` / `invalid_tier_structure` / `missing_base_fee`）。
3. **档位覆盖校验**：任一月峰或谷用电量超过该费率表已公布的最高档位上限
   → 排除（`usage_exceeds_published_tiers`，响应中注明月份与段别）。无上限档（止为空）可承接任意电量。
4. **逐档计费**：每月峰、谷电量分别沿各自费率表按 `[下限, 上限)` 区间逐档计费，
   金额用 `Decimal` 保留两位小数（四舍五入），再加固定月基本费得月合计。
5. **年度汇总与排序**：12 个月月合计求和为年度成本，按从低到高排序返回。

## 抓取失败处理

- 列表页（含分页）或套餐详情抓取失败（连接错误、超时、HTTP 4xx/5xx、结构无法解析）时，
  在响应 `failures` 中列出失败地址、阶段（`list`/`detail`）、原因与 HTTP 状态码。
- 只要存在任何抓取失败，`conclusion.verification_status` 标记为 `pending_verification`（待核实）；
  全部成功且存在有效套餐时为 `verified`。无有效套餐时同样为 `pending_verification`。
- 分页跟随 `a[rel=next]`，并设有 50 页安全上限与去重，避免异常站点造成无限抓取。

## 演示数据覆盖的场景

| 套餐 | 地区 | 演示中的处理结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 上海/江苏/浙江/华东 | 有效，参与排序 |
| P002 安能 峰谷B | 上海/华东 | 有效，参与排序 |
| P003 沪电 阶梯标准版 | 上海 | 有效，示例数据下年度最低价 |
| P004 通商 华北套餐 | 北京/天津/华北 | 地区不匹配，排除 |
| P005 安能 A+ | 上海/江苏/华东 | 缺谷段费率，排除 |
| P006 沪电 封顶套餐 | 上海 | 第 1 月峰用电即超 150 kWh 封顶档，排除 |
| P007 安能 C | 上海/华东 | 详情页 404，进入 `failures`，最低价标为待核实 |

示例请求下的复核年度成本（元）：**P003 6899.40 < P001 7247.40 < P002 7842.00**。

## 响应结构（摘要）

```json
{
  "conclusion": {
    "lowest_plan_id": "P003",
    "lowest_plan_name": "沪电零售 · 峰谷阶梯标准版",
    "lowest_annual_total_cny": 6899.4,
    "verification_status": "pending_verification",
    "note": "存在抓取失败的列表页或套餐详情，最低价结论待核实"
  },
  "ranked_plans": [ { "rank": 1, "annual_total_cny": 6899.4,
                      "monthly_bills": [ { "peak_charges": [...], "valley_charges": [...] } ] } ],
  "excluded_plans": [ { "plan_id": "P006", "reason": "usage_exceeds_published_tiers", "detail": "..." } ],
  "failures": [ { "url": ".../p007.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每个参与排序的套餐均带 `detail_url`（详情来源）与 `list_url`（发现该套餐的列表页），
月度账单逐档列出 charged_kwh 与 charge_cny，便于采购员复核。
