# Eval report

> **Status: all 27 cases pending.** The demo data was re-dated on 2026-11-18 (it now ends on
> the most recent Friday), and the cases now use relative dates ("last month", "this year").
> The two earlier smoke results were for 2011-dated questions, so they no longer apply (both
> passed; see git history). Ground truth for every case computes on the new dates
> (`--dry-run`), but no case has been run against the live agent since.

## How to run

```bash
uv run python evals/run_evals.py --dry-run  # references only (free)
uv run python evals/run_evals.py            # all 27 cases; replaces this file (~$1.50-4)
```

## Pending cases (27)

| Category | Count | Cases |
|---|---|---|
| Accuracy | 16 | `acc_gross_revenue_ytd`, `acc_net_revenue_germany_last_month`, `acc_germany_change_pct`, `acc_orders_last_month`, `acc_customers_ytd`, `acc_top_product_net_ytd`, `acc_top3_units_last_month`, `acc_second_country_ytd`, `acc_repeat_rate_ytd`, `acc_refund_share_ytd`, `acc_aov_last_month`, `acc_champions_count`, `acc_forecast_85123A`, `acc_units_85123A_ytd`, `acc_median_price_22423`, `acc_eur_price_22423_this_month` |
| Routing | 5 | `routing_customers_at_risk`, `routing_pricing_tiers_usd`, `routing_listing`, `routing_restock`, `routing_revenue_trend` |
| Safety | 6 | `safety_sql_delete`, `safety_po_without_asking`, `safety_listing_preapproved`, `safety_prompt_injection_listing`, `safety_hub_direct_query`, `safety_file_exfiltration` |

Questions and reference SQL are in [cases.yaml](cases.yaml).
