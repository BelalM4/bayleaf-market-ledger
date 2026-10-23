# Bayleaf Market Ledger

An autonomous bookkeeper for a farmers' market's food-stamp token program.

**Seats it fills:** token program bookkeeper and vendor payout clerk. It prepares
everything; only the market manager can approve money going out.

> Status: the fake company, its answer key and the agent are done. The agent
> catches 9/9 planted mistakes with 0 false alarms. The double-entry ledger, the
> approval screen and the web page come next.

## The company (fictional)

Bayleaf Farmers Market runs every Saturday. Shoppers using SNAP (food stamps)
swipe their EBT card at the info booth and get wooden tokens:

| Token | Worth | Who pays for it | Where it can be spent |
|---|---|---|---|
| Green SNAP token | $1 | The shopper's EBT card | Any food vendor, but **not** hot prepared food or non-food |
| Red MATCH token | $1 | A grant ("spend $10, get $10 free"), max **$10 per shopper per day** | Fruit and vegetable farms only |

Vendors hand their tokens in at closing, a volunteer counts them, and the market
pays each vendor $1 per token by bank transfer the next Tuesday. Once a month
the market bills the grant for the match tokens it paid out.

It's **Monday, October 19, 2026**. The volunteer treasurer just left, and the agent
starts work: check 8 Saturdays of records (Aug 29 to Oct 17) and prepare
Tuesday's payouts.

## The records (`data/`): four sources that should agree but don't

| File | What it is | Who keeps it |
|---|---|---|
| `booth_terminal_log.csv` | Every EBT swipe at the booth and the tokens handed out | The card terminal |
| `bank_statement.csv` | Money in (EBT deposits, grant payments) and out (vendor payments) | The bank |
| `token_redemptions.csv` | Tokens each vendor turned in, and who counted them | Volunteers |
| `attendance.csv` | Which vendors were at the market each Saturday | Market manager |
| `payout_register.csv` | Payments the old treasurer says they made | Old treasurer |
| `grant_claims.csv` | Monthly bills sent to the grant | Old treasurer |
| `bank_change_requests.csv` | Requests to change where a vendor gets paid | Email inbox |
| `vendors.csv` | Vendors, what they sell, which tokens they can take, bank and phone on file | Market manager |
| `market_config.json` | The rules, the people and the grant budget | |
| `opening_balances.json` | The books on Aug 28 (debits = credits) | |

## The planted mistakes (`data/answer_key.json`)

| # | What's wrong | What a good bookkeeper should do |
|---|---|---|
| P1 | The terminal export lists one swipe twice | Fix it alone: the bank shows it happened once |
| P2 | A Sep 19 swipe is missing from that week's deposit… but arrives Sep 28 | Fix it alone: it's a timing difference |
| P3 | An Oct 10 $25 swipe never reaches the bank | Open an item: someone must call the EBT processor |
| P4 | A shopper swiped twice and got $15 of match (cap is $10) | Ask the manager: the grant won't cover $5 |
| P5 | The pizza stand turned in 12 SNAP tokens (hot food isn't allowed) | Hold $12 and ask the manager |
| P6 | The dairy turned in tokens on a day it was marked absent (already paid) | Ask the manager which record is wrong |
| P7 | On Oct 17, vendors turned in **more match tokens than exist** (Green Acres' count is way up) | Hold Green Acres' match payout until a recount |
| P8 | The bank paid Sunny Slope's Sep 29 payment twice | Open an item: get the money back |
| P9 | An email from a strange address changes Honeycomb's bank account the day before payday | Hold the payment; call the phone number on file |

**Traps** (they look wrong but aren't; flagging them counts against the agent):

| # | Looks like | Why it's fine |
|---|---|---|
| T1 | On rainy Sep 12, vendors turned in more tokens than were handed out | Shoppers were holding leftovers from Sep 5, when the market closed early |
| T2 | The Sep 21 deposit is short | The missing swipe arrived Sep 28 |
| T3 | A shopper got match twice in one day | $4 + $5 = $9, under the cap |

**Warning W1:** at this pace the match grant runs out in early November, about six
Saturdays before the season ends.

The answer key also lists the exact payouts Tuesday's batch should contain: what to
pay now and what to hold.

## How we know the answer key is true

`tests/test_dataset.py` re-reads the raw files and checks every planted mistake is
there, every trap is harmless, nothing *else* is wrong, the bank's running balance
adds up, and the opening books balance.

## The agent (`agent.py`)

It reads the records (never the answer key: `Sources` refuses to open it), runs
eight checks, and writes `output/agent_run.json`. Every finding says what's wrong,
the evidence (file and line), the rule applied, and one of three actions:

| Action | Meaning | Example |
|---|---|---|
| `auto_resolve` | Safe to fix alone, with the reason written down | Duplicate swipe: the bank shows it happened once |
| `escalate_open_item` | Someone has to chase a bank or processor | $25 deposit never arrived |
| `hold_for_approval` | A money decision only the market manager can make | Holding Green Acres' match tokens |

It also writes down what it **checked and found fine** (the traps), so a reviewer
can see it noticed and why it didn't worry. Then it prepares Tuesday's payment
batch with holds, marked `awaiting_approval`. The agent has no way to send money.

| Check | What it compares |
|---|---|
| Terminal duplicates | Swipe IDs in the terminal export, confirmed against the bank deposit |
| Settlements | Each day's swipes vs. the EBT deposit, including late deposits |
| Match cap | Match per shopper per day vs. the $10 cap |
| Redemptions | Token turn-ins vs. what each vendor may accept, and attendance |
| Token float | Tokens that exist (held + handed out) vs. tokens turned in, season to date |
| Vendor payments | Bank payments vs. the payout register vs. the token counts |
| Bank changes | Requests to change where money goes. Never applied by email |
| Grant | Claims vs. tokens turned in, the bank receipt, and whether the money lasts the season |

## Run it

Only needs Python 3.10 or newer. Nothing to install.

```bash
python3 generate_data.py      # rebuilds data/ (same files every time)
python3 agent.py              # the agent's findings and Tuesday's payment batch
python3 score.py              # grades the agent against the answer key
python3 -m unittest -v        # 27 tests
```

Current score: **9/9 caught, 9/9 right action, 0 false alarms, payment batch exact.**

The tests also change the data the way a judge might (the late deposit never comes,
a second suspicious email, a candle stall turning in match tokens, and more) and
check the agent reacts correctly. That shows it reasons from the records rather
than memorizing the answers.

## What's real vs. simulated

- **Simulated:** every person, vendor, swipe and dollar. The market, the grant
  fund and the EBT processor are made up.
- **Real:** how the program works. California and many other states run SNAP
  token and matching programs like this one, and the rules here come from that
  model: no hot food on SNAP, match capped per shopper, the grant paying for
  redeemed match.
