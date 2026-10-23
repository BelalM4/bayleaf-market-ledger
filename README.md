# Bayleaf Market Ledger

An autonomous bookkeeper for a farmers' market's food-stamp token program.

**Seats it fills:** token program bookkeeper and vendor payout clerk. It prepares
everything; only the market manager can approve money going out.

> Status: working end to end: data, agent, double-entry books, approval gate and
> web app with "ask why" (67 tests). The agent catches 9/9 planted mistakes with 0 false alarms.

## Start the app

```bash
python3 server.py
```

Then open **http://localhost:8000**. Nothing to install (Python 3.10+).

| Tab | What it shows |
|---|---|
| Needs a decision | The agent's six approval requests. Switch "Acting as" to try approving as the agent or a volunteer, and watch the gate refuse. |
| What the agent found | All findings, each with the explanation, the rule and the evidence (file and line) |
| Checked & fine | Things that look wrong but aren't, and why the agent didn't flag them |
| Tuesday's payments | The payment batch: owed, held, paid, refused, and the account each payment goes to |
| Books | 17 tie-outs, the trial balance, and every journal entry with its source |
| Audit trail | Every step, request, decision and refused attempt |
| What if…? | Change the records (or any vendor's token count) and watch the agent react, on a scratch copy |
| Proof | The score against the answer key, what's real vs. simulated, and fingerprints of the files read |

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

## The books (`ledger.py`, `books.py`)

**Double entry** means every entry moves money between at least two accounts, and
debits always equal credits. The ledger refuses, with an error, any entry that
doesn't balance, uses an unknown account, has a bad line, or is dated in a
**closed month** (August and September are closed; a fix to September gets posted
in October with a note). Amounts are whole cents, so there's no rounding.

`books.py` turns every record into an entry that points back to its source file:

| What happened | Debit | Credit |
|---|---|---|
| Booth hands out SNAP tokens | 1100 EBT Receivable | 2000 SNAP Tokens Outstanding |
| Booth hands out match tokens | 1210 Grant Unbilled | 2010 Match Tokens Outstanding |
| Vendor turns tokens in | 2000 / 2010 | 2100 Vendor Payable (or 2110 On Hold) |
| Tokens that can't exist | 1900 Suspense | |
| EBT deposit arrives | 1000 Cash | 1100 EBT Receivable |
| Market pays a vendor | 2100 Vendor Payable | 1000 Cash |
| Bank pays someone twice | 1400 Due from Bank | 1000 Cash |
| Bill the grant / grant pays | 1200 Grant Billed / 1000 Cash | 1210 / 1200 |

Then **17 tie-outs** check each important balance against an outside source:
- Cash vs. the bank statement
- EBT receivable vs. swipes not yet deposited (exactly the missing $25)
- Tokens outstanding vs. the token count
- Suspense vs. the 22 tokens that can't exist
- Vendor payable vs. the payment batch, vendor by vendor (the subledger)
- Due from bank vs. the duplicate payment

All pass, before and after every approval decision.

## The approval gate (`gate.py`)

The agent prepares six requests: the payment batch, plus one for each money
decision. Only the market manager (`jordan.lee`) can decide them.

- **Segregation of duties:** whoever requests something can't approve it. The agent
  requests everything, so it can never approve anything. A volunteer can only view.
- Each request has specific choices, and each choice posts specific balanced
  entries marked with who approved them. Overrides, like paying tokens that can't
  exist, require a written note, and the cost shows up as a loss on the books.
- Each request can be decided once. Refused attempts are written to the audit trail too.
- Releasing a hold after the batch is approved pays it as a separate payment.
- A confirmed bank change sends the money to the new account. Otherwise it goes to
  the account on file.

```bash
python3 gate.py status
python3 gate.py decide REQ-01 approve --as agent        # REFUSED: segregation of duties
python3 gate.py decide REQ-01 approve --as jordan.lee   # pays $470, books still tie out
python3 gate.py decide REQ-05 pay_verified_only --as jordan.lee
python3 gate.py reset
```

## Ask why (`explain.py`)

Every finding and decision card has an **Ask why** box ("Why not just pay it?",
"How sure are you?", "What would change your mind?", or any question you type).

- **With Claude connected**, Claude (`claude-opus-5-5`) answers using only that finding's
  records: the explanation, the evidence (file and line), the rule, the related approval
  requests and decisions, and what the agent cleared. It cites `file:line`, defends the
  decision, and says what new evidence would change it. It can explain but never approve.
  The records go in as data, so text inside them (like the suspicious email) can't
  instruct Claude. If Claude declines, the API automatically retries on a fallback model
  (`fallbacks: "default"`).
- **Without Claude** (no package, no key, no wifi), the box answers from the agent's
  recorded reasoning: the explanation, rule, evidence and what would change the decision.
  The page labels which one answered, so the demo never breaks.
- Every question is written to the audit trail with who asked it and which source answered.

To connect Claude:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-claude.txt
export ANTHROPIC_API_KEY=your-key-here
python3 server.py
```

## Run it

Only needs Python 3.10 or newer. Nothing to install.

```bash
python3 generate_data.py      # rebuilds data/ (same files every time)
python3 agent.py              # the agent's findings and Tuesday's payment batch
python3 score.py              # grades the agent against the answer key
python3 gate.py status        # the approval queue and the books
python3 server.py             # the web app at http://localhost:8000
python3 -m unittest -v        # 67 tests
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
