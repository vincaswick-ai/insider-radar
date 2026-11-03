# Pre-registered test: conviction clusters with realistic costs
**Rule (written down before running):** 3+ insiders within 7 days AND (total bought $5M+ OR insiders at least doubled their holdings / new position).
**Costs by liquidity:** >$10M/day 0.25%, $1-10M 0.5%, $250K-1M 1%, <$250K 2% per round trip.
Returns minus S&P over the same days. Entry next open after the signal. Win = beat the S&P after costs.

## 1. Trades
| Group | Years | Hold | Trades | Win | Avg | Median | Avg w/o top 1% | t |
|---|---|---|---|---|---|---|---|---|
| All 3+ clusters | 2006-2015 | 5d | 989 | 50% | +1.20% | +0.01% | +0.64% | 3.9 |
| All 3+ clusters | 2006-2015 | 10d | 989 | 50% | +1.72% | +0.05% | +0.95% | 4.1 |
| All 3+ clusters | 2016-2026 | 5d | 2,154 | 47% | +0.43% | -0.36% | -0.24% | 1.7 |
| All 3+ clusters | 2016-2026 | 10d | 2,154 | 47% | +0.19% | -0.64% | -0.61% | 0.6 |
| All 3+ clusters | All | 5d | 3,143 | 48% | +0.68% | -0.17% | +0.04% | 3.4 |
| All 3+ clusters | All | 10d | 3,143 | 48% | +0.67% | -0.36% | -0.12% | 2.6 |
| **Conviction** | 2006-2015 | 5d | 309 | 55% | +1.96% | +0.39% | +1.20% | 3.0 |
| **Conviction** | 2006-2015 | 10d | 309 | 52% | +2.79% | +0.49% | +1.78% | 3.3 |
| **Conviction** | 2016-2026 | 5d | 611 | 52% | +1.23% | +0.31% | +0.41% | 2.3 |
| **Conviction** | 2016-2026 | 10d | 611 | 50% | +0.85% | -0.12% | -0.08% | 1.3 |
| **Conviction** | All | 5d | 920 | 53% | +1.47% | +0.34% | +0.73% | 3.5 |
| **Conviction** | All | 10d | 920 | 51% | +1.50% | +0.24% | +0.61% | 2.9 |

**Conviction by liquidity (all years, 5d)**
| Traded per day | Trades | Cost | Win | Avg | Median |
|---|---|---|---|---|---|
| > $10M | 417 | 0.25% | 55% | +1.13% | +0.43% |
| $1-10M | 289 | 0.5% | 53% | +1.18% | +0.43% |
| $250K-1M | 113 | 1% | 53% | +1.46% | +1.00% |
| < $250K | 101 | 2% | 44% | +3.75% | -1.44% |

## 2. Placebo (same stocks 126 trading days later, same costs)
| Group | Years | Hold | Trades | Real − placebo | t |
|---|---|---|---|---|---|
| All 3+ clusters | 2016-2026 | 5d | 2,154 | +1.81% | 5.6 |
| All 3+ clusters | 2016-2026 | 10d | 2,154 | +1.69% | 4.1 |
| All 3+ clusters | All | 5d | 3,143 | +1.68% | 6.6 |
| All 3+ clusters | All | 10d | 3,143 | +1.76% | 5.4 |
| Conviction | 2016-2026 | 5d | 611 | +2.98% | 4.3 |
| Conviction | 2016-2026 | 10d | 611 | +3.19% | 3.9 |
| Conviction | All | 5d | 920 | +2.53% | 4.7 |
| Conviction | All | 10d | 920 | +2.87% | 4.4 |

## 3. Portfolio with realistic costs (start 2006-04)
| Strategy | Slots | Hold | Idle money | Per year | Since 2016 | Worst drop | Trades/yr |
|---|---|---|---|---|---|---|---|
| S&P 500 | – | – | – | +10.93% | +14.82% | -55% | – |
| All 3+ clusters | 10 | 5d | cash | **+11.27%** | +10.61% | -38% | 138 |
| All 3+ clusters | 10 | 5d | SPY | **+20.12%** | +22.32% | -50% | 138 |
| All 3+ clusters | 10 | 10d | cash | **+12.02%** | +9.65% | -57% | 117 |
| All 3+ clusters | 10 | 10d | SPY | **+18.25%** | +16.66% | -57% | 117 |
| All 3+ clusters | 5 | 5d | cash | **+18.35%** | +19.65% | -53% | 111 |
| All 3+ clusters | 5 | 5d | SPY | **+25.04%** | +27.50% | -60% | 111 |
| All 3+ clusters | 5 | 10d | cash | **+21.48%** | +20.94% | -49% | 81 |
| All 3+ clusters | 5 | 10d | SPY | **+25.22%** | +24.58% | -53% | 81 |
| **Conviction** | 10 | 5d | cash | **+6.26%** | +7.04% | -37% | 45 |
| **Conviction** | 10 | 5d | SPY | **+17.27%** | +21.81% | -53% | 45 |
| **Conviction** | 10 | 10d | cash | **+7.95%** | +7.00% | -43% | 44 |
| **Conviction** | 10 | 10d | SPY | **+17.98%** | +20.53% | -52% | 44 |
| **Conviction** | 5 | 5d | cash | **+11.80%** | +13.07% | -48% | 43 |
| **Conviction** | 5 | 5d | SPY | **+23.00%** | +28.01% | -57% | 43 |
| **Conviction** | 5 | 10d | cash | **+13.08%** | +9.83% | -59% | 40 |
| **Conviction** | 5 | 10d | SPY | **+22.17%** | +21.61% | -57% | 40 |

### Year by year (conviction)
| Year | S&P 500 | Conviction, 10 slots, 5d, idle cash | Conviction, 10 slots, 5d, idle SPY |
|---|---|---|---|
| 2006 | +10.7% | -1.3% | +9.5% |
| 2007 | +5.1% | +2.7% | +9.3% |
| 2008 | -36.8% | +7.3% | -29.7% |
| 2009 | +26.4% | +9.8% | +35.4% |
| 2010 | +15.1% | +13.4% | +30.7% |
| 2011 | +1.9% | -5.2% | -2.4% |
| 2012 | +16.0% | +3.7% | +18.6% |
| 2013 | +32.3% | +5.0% | +37.5% |
| 2014 | +13.5% | +5.0% | +17.4% |
| 2015 | +1.2% | +13.8% | +14.3% |
| 2016 | +12.0% | +8.5% | +18.4% |
| 2017 | +21.7% | +7.3% | +28.8% |
| 2018 | -4.6% | +1.9% | -1.7% |
| 2019 | +31.2% | +13.6% | +42.7% |
| 2020 | +18.3% | -0.4% | +24.5% |
| 2021 | +28.7% | +15.0% | +42.5% |
| 2022 | -18.2% | -10.1% | -22.5% |
| 2023 | +26.2% | +9.5% | +35.5% |
| 2024 | +24.9% | +31.5% | +53.9% |
| 2025 | +17.7% | +10.2% | +26.4% |
| 2026 | +11.6% | -5.1% | +7.9% |

_Survivorship bias: delisted/bankrupt companies often have no prices. No taxes._