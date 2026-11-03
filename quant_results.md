# Quant pass: making the 3+ insider cluster strategy better (without fooling ourselves)
Rules picked ONLY on 2006-2015 (train), then checked on 2016-2026 (test, never used for picking). Entry = next open after the 3rd insider's Form 4 is public. Returns minus S&P (SPY) over the same days, 0.25% cost included. **Win** = beat the S&P. **Win raw** = made money at all. **Avg w/o top 1%** = average after removing the 1% biggest winners (shows if a few lottery tickets carry it).

## 1. Baseline (all 3+ clusters)

**Hold 5 days**
| Group | Trades | Win | Win raw | Avg | Median | Avg w/o top 1% | t |
|---|---|---|---|---|---|---|---|
| Train 2006-2015 | 989 | 53% | 54% | +1.63% | +0.33% | +1.06% | 5.2 |
| Test 2016-2026 | 2,154 | 50% | 51% | +0.78% | -0.03% | +0.11% | 3.0 |

**Hold 10 days**
| Group | Trades | Win | Win raw | Avg | Median | Avg w/o top 1% | t |
|---|---|---|---|---|---|---|---|
| Train 2006-2015 | 989 | 53% | 55% | +2.15% | +0.42% | +1.37% | 5.2 |
| Test 2016-2026 | 2,154 | 49% | 54% | +0.54% | -0.24% | -0.27% | 1.7 |

## 2. What separates winners from losers (5-day hold)
Left = train years, right = test years. A useful feature shows the SAME direction on both sides.

| Feature | Bucket | Train trades | Train win | Train avg | Test trades | Test win | Test avg |
|---|---|---|---|---|---|---|---|
| Insiders known at signal | 3 | 669 | 52% | +1.12% | 1,505 | 50% | +0.76% |
| Insiders known at signal | 4 | 164 | 51% | +2.63% | 349 | 51% | +1.20% |
| Insiders known at signal | 5+ | 156 | 61% | +2.75% | 300 | 47% | +0.43% |
| Total bought | $1-5M | 222 | 58% | +3.28% | 551 | 49% | +0.61% |
| Total bought | $250K-1M | 400 | 52% | +1.36% | 890 | 45% | +0.07% |
| Total bought | $5M+ | 150 | 59% | +2.49% | 315 | 55% | +1.83% |
| Total bought | <$250K | 217 | 46% | -0.17% | 398 | 56% | +1.77% |
| CEO/CFO in cluster | no | 394 | 55% | +2.32% | 805 | 50% | +0.28% |
| CEO/CFO in cluster | yes | 595 | 52% | +1.17% | 1,349 | 49% | +1.08% |
| Median stake increase | 100%+/new | 192 | 58% | +2.72% | 368 | 54% | +1.50% |
| Median stake increase | 20-100% | 300 | 53% | +2.33% | 589 | 46% | +1.01% |
| Median stake increase | 5-20% | 275 | 47% | +0.06% | 663 | 48% | +0.34% |
| Median stake increase | <5% | 222 | 58% | +1.67% | 534 | 52% | +0.57% |
| Insider price | $2-5 | 120 | 52% | +2.71% | 284 | 48% | +0.39% |
| Insider price | $20+ | 334 | 49% | +0.27% | 763 | 50% | +0.00% |
| Insider price | $5-20 | 466 | 56% | +1.78% | 910 | 51% | +0.90% |
| Insider price | <$2 | 69 | 52% | +5.30% | 197 | 46% | +3.83% |
| Traded per day | $1-10M | 326 | 56% | +1.82% | 732 | 49% | +0.63% |
| Traded per day | $10M+ | 402 | 51% | +1.03% | 952 | 49% | +0.01% |
| Traded per day | $250K-1M | 116 | 57% | +1.72% | 257 | 53% | +1.93% |
| Traded per day | <$250K | 145 | 49% | +2.78% | 213 | 52% | +3.39% |
| Below 1y high | 15-30% | 240 | 51% | +0.55% | 483 | 54% | +0.20% |
| Below 1y high | 30-50% | 229 | 54% | +1.58% | 613 | 47% | -0.24% |
| Below 1y high | 50%+ | 242 | 56% | +3.91% | 648 | 50% | +2.43% |
| Below 1y high | <15% | 278 | 52% | +0.61% | 410 | 46% | +0.40% |
| Last 20 days | -20..-5% | 298 | 54% | +1.60% | 672 | 49% | +0.13% |
| Last 20 days | -5..+5% | 290 | 50% | +0.57% | 501 | 52% | +0.88% |
| Last 20 days | < -20% | 186 | 51% | +2.39% | 497 | 47% | +0.93% |
| Last 20 days | > +5% | 215 | 58% | +2.42% | 484 | 50% | +1.44% |
| Last 5 days | -10..0% | 342 | 53% | +1.61% | 702 | 48% | -0.06% |
| Last 5 days | 0..+10% | 404 | 52% | +1.17% | 698 | 51% | +0.95% |
| Last 5 days | < -10% | 137 | 55% | +2.09% | 438 | 50% | +0.74% |
| Last 5 days | > +10% | 106 | 55% | +2.82% | 316 | 48% | +2.34% |
| Filing day vs S&P | -3..+3% | 729 | 53% | +1.35% | 1,383 | 50% | +0.35% |
| Filing day vs S&P | < -3% | 79 | 53% | +2.62% | 277 | 51% | +2.35% |
| Filing day vs S&P | > +3% | 181 | 55% | +2.31% | 494 | 46% | +1.11% |
| Gap at entry open | +2..+5% | 111 | 59% | +3.89% | 381 | 50% | +1.38% |
| Gap at entry open | -2..+2% | 797 | 52% | +0.84% | 1,418 | 50% | +0.70% |
| Gap at entry open | < -2% | 53 | 64% | +4.80% | 120 | 45% | -0.83% |
| Gap at entry open | > +5% | 28 | 46% | +9.08% | 235 | 48% | +1.14% |
| Daily volatility | 2-4% | 413 | 53% | +1.02% | 807 | 49% | +0.02% |
| Daily volatility | 4%+ | 277 | 55% | +3.71% | 813 | 49% | +2.18% |
| Daily volatility | <2% | 299 | 52% | +0.53% | 534 | 52% | -0.20% |
| S&P vs 200-day avg | above | 604 | 51% | +1.03% | 1,557 | 50% | +0.83% |
| S&P vs 200-day avg | below | 385 | 56% | +2.56% | 597 | 48% | +0.65% |
| Insider sells 90 days before | none | 664 | 54% | +2.03% | 1,506 | 49% | +0.89% |
| Insider sells 90 days before | some | 325 | 51% | +0.82% | 648 | 51% | +0.53% |
| Filing delay | 0-1 days | 267 | 50% | +0.90% | 525 | 51% | +1.70% |
| Filing delay | 2 days | 204 | 56% | +2.42% | 469 | 50% | +0.97% |
| Filing delay | 3+ days | 518 | 54% | +1.69% | 1,160 | 49% | +0.29% |

## 3. Automatic rule search (train only), then the honest test

**Hold 5 days** — filters added one by one, each must clearly improve the train t-stat
| Group | Trades | Win | Win raw | Avg | Median | Avg w/o top 1% | t |
|---|---|---|---|---|---|---|---|
| Baseline train | 989 | 53% | 54% | +1.63% | +0.33% | +1.06% | 5.2 |
| Baseline TEST | 2,154 | 50% | 51% | +0.78% | -0.03% | +0.11% | 3.0 |

**Hold 10 days** — filters added one by one, each must clearly improve the train t-stat
| Group | Trades | Win | Win raw | Avg | Median | Avg w/o top 1% | t |
|---|---|---|---|---|---|---|---|
| Baseline train | 989 | 53% | 55% | +2.15% | +0.42% | +1.37% | 5.2 |
| Baseline TEST | 2,154 | 49% | 54% | +0.54% | -0.24% | -0.27% | 1.7 |

**Hygiene rule decided in advance (not optimised):** Insider price $3+, Trading $1M+/day, No 10b5-1 plan, Open gap under +2%
| Group | Trades | Win | Win raw | Avg | Median | Avg w/o top 1% | t |
|---|---|---|---|---|---|---|---|
| 5d train | 615 | 53% | 55% | +1.01% | +0.38% | +0.66% | 3.3 |
| 5d TEST | 1,148 | 49% | 49% | +0.18% | -0.14% | -0.37% | 0.6 |
| 10d train | 615 | 53% | 56% | +1.22% | +0.42% | +0.66% | 3.1 |
| 10d TEST | 1,148 | 48% | 54% | -0.26% | -0.34% | -0.88% | -0.8 |

## 4. Placebo on the TEST years (same stocks 126 trading days later)
| Rule | Hold | Trades | Real − placebo | t |
|---|---|---|---|---|
| Baseline (all 3+) | 5d | 2,154 | +1.81% | 5.6 |
| Baseline (all 3+) | 10d | 2,154 | +1.69% | 4.1 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 5d | 1,148 | +1.10% | 3.1 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 10d | 1,148 | +0.94% | 2.1 |

## 5. Portfolio: 10 slots, idle money in cash, start 2006-04
Same simulator as before. Cost per round trip changed to show how fragile the edge is.

| Rule | Hold | Cost | Per year | Since 2016 | Worst drop | Trades/yr |
|---|---|---|---|---|---|---|
| S&P 500 | – | – | +10.93% | +14.82% | -55% | – |
| Baseline (all 3+) | 5d | 0.25% | **+17.38%** | +17.87% | -37% | 138 |
| Baseline (all 3+) | 5d | 0.5% | **+13.40%** | +12.85% | -38% | 138 |
| Baseline (all 3+) | 5d | 1% | **+5.84%** | +3.45% | -42% | 138 |
| Baseline (all 3+) | 10d | 0.25% | **+17.52%** | +15.83% | -57% | 117 |
| Baseline (all 3+) | 10d | 0.5% | **+14.14%** | +11.82% | -57% | 117 |
| Baseline (all 3+) | 10d | 1% | **+7.68%** | +4.22% | -63% | 117 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 5d | 0.25% | **+4.81%** | +3.01% | -45% | 81 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 5d | 0.5% | **+2.70%** | +0.49% | -46% | 81 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 5d | 1% | **-1.39%** | -4.36% | -54% | 81 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 10d | 0.25% | **+4.42%** | +1.81% | -53% | 74 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 10d | 0.5% | **+2.50%** | -0.41% | -56% | 74 |
| Hygiene rule (set in advance): Insider price $3+ + Trading $1M+/day + No 10b5-1 plan + Open gap under +2% | 10d | 1% | **-1.23%** | -4.73% | -68% | 74 |

_Survivorship bias: delisted/bankrupt companies often have no prices. No taxes. Filters chosen on 2006-2015 only; 2016-2026 is the honest check._