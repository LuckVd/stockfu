## Problem Statement

现有分享图仅展示整体市场、创业板、科创50与自选标的，无法说明行业强弱、行业情绪及主力资金的流向；用户无法从导出图片判断市场轮动。

## Solution

分享图增加独立的行业全景分页。采用东方财富同一行业分类下的历史行情和历史主力资金流，展示全部可取得且当日完整的行业。首次初始化串行回补历史；后续仅用同一交易日数据更新。行业情绪分别表达波动/下跌、上涨/流入与相对放量，不建立不可解释的第四个总分。

## User Stories

1. As an investor, I want to see every available industry in export images, so that I can identify market-wide rotation.
2. As an investor, I want each industry to show price change, five-day change and main-fund flow, so that I can distinguish price strength from capital flow.
3. As an investor, I want fear, greed and heat shown per industry, so that I can compare downside pressure, momentum and attention separately.
4. As an investor, I want funding states such as continuous inflow, continuous outflow, strengthening and weakening, so that I can read rotation without a black-box score.
5. As an investor, I want unavailable or stale industry data excluded rather than silently backfilled with an old date, so that exported conclusions are trustworthy.
6. As an operator, I want initial history collection to be serial and rate-limited, so that the free upstream is not overloaded.
7. As an operator, I want one failed industry to be reported but not stop the remaining sectors, so that a partial upstream outage does not discard available data.

## Implementation Decisions

- The export-data seam is a single market-pulse builder invoked by the existing share-card builder.
- Historical price and historical fund-flow records use the same Eastmoney industry names; no cross-vendor industry mapping or aggregation is used.
- Historical collection requests one industry at a time, waits at least 1.2 seconds between industries, is idempotent by date, and reports failed names.
- An industry is exportable only when both its industry quote and fund-flow records equal the card trade date.
- Fear combines valid volatility, inverse momentum and inverse fund-flow percentiles; greed combines momentum and fund-flow percentiles; heat is the percentile of amount relative to its preceding 20-day mean.
- Fund-flow percentile is only used after ten observations. Amount and price history may still produce the remaining measures.
- Multi-image export dedicates fixed-size industry pages before watchlist pages.

## Testing Decisions

- Test the market-pulse builder at its public output boundary with an in-memory database.
- Assert that an industry missing same-day flow is omitted and a complete industry exposes its state. This follows existing share-integrity tests that validate output freshness instead of private helper calls.
- Keep data-source calls unmocked only in manually invoked backfill; automated tests do not require network access.

## Out of Scope

- Paid data vendors, intraday data, automatic trading signals, concept-board aggregation, and a fourth composite rotation score are out of scope.

## Further Notes

The free historical-fund-flow endpoint may fail or offer only a recent window. The UI must expose only successfully synchronized same-day rows and must not imply five-year fund-flow history.
