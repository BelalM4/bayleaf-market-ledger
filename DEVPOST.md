# Devpost draft

Paste each section into the matching Devpost field. Lines in **[brackets]** are for you
to fill in with your own words. Don't let anyone else (including me) invent your story.

---

## Project name
Bayleaf Market Ledger

## Tagline (elevator pitch)
An autonomous bookkeeper for a farmers' market food-stamp token program. It catches fake tokens before payday, and it can't pay anyone without the manager's sign-off.

## Seats it fills (one line, as Hyphenate asks)
Token program bookkeeper and vendor payout clerk at a farmers' market SNAP token program. It prepares and explains everything; only the market manager approves money.

---

## The moment

Last Saturday, vendors at the (fictional) Bayleaf Farmers Market turned in **280** red match tokens. The market's own records say no more than **258** could exist: what shoppers were still holding, plus what the booth handed out. My agent caught the 22 impossible tokens **before** payday. It worked out that Green Acres Farm's count (168, against its usual 110) was the likely source, and held Green Acres' match tokens. Every other vendor was paid on time. Ask it "why not just pay?" and it answers with the exact file and line, the rule it applied, and what would change its mind.

## Inspiration

**[In your own words: why this problem? For example, have you used, volunteered at, or seen a farmers' market or a food-assistance program? If you talked to anyone about it this weekend, say so. If not, just say why it caught your interest. Keep it true.]**

The Hyphenate prompt asked for "a company we haven't seen made autonomous before." Farmers' market token programs are tiny, run by volunteers, and messy underneath. Wooden tokens act like a private currency that has to reconcile against a card terminal, a bank, volunteer count sheets and a grant budget. One mistake shorts a farmer or bills a grant for money it doesn't owe.

## What it does

How the program works: a shopper swipes their EBT card at the info booth and gets $1 SNAP tokens, plus free match tokens (up to $10 a day) paid for by a grant. They spend the tokens at farm stands. Vendors turn them in, and the market pays $1 per token the next Tuesday.

The agent takes over on a Monday after the volunteer treasurer quits. It:

- **Reconciles four sources that disagree:** the card terminal, the bank statement, the volunteers' token counts and attendance log, and the grant claims.
- **Finds the problems and picks one of three actions:** fix it alone (and write down why), open an item for someone to chase, or hold the money for the manager. Every finding cites the file and line it came from and the rule it applied.
- **Writes down what it checked and found fine.** Three planted traps look like errors but aren't (for example, more tokens came back than went out on a rainy Saturday, because shoppers had leftovers from the week before).
- **Prepares Tuesday's payments with holds:** $470 ready to pay and $207 held across three vendors.
- **Keeps real double-entry books.** Every event becomes a balanced journal entry pointing back to its source, August and September are closed, and 17 tie-outs check each balance against an outside record (the $25 in EBT receivable is exactly the swipe the bank never paid).
- **Stops at an approval gate.** The agent requests; only the market manager approves. The agent can't approve its own requests (segregation of duties), volunteers can only view, overrides need a written note, and every attempt, including refused ones, goes into the audit trail.
- **Answers "why?"** in plain language using only the evidence, with Claude when connected and its own recorded reasoning otherwise.
- **Looks ahead:** at this pace, the match grant runs out around Nov 7, about six Saturdays before the season ends.

## Proof it works: what's real, what's simulated, and how I know

- **Simulated:** the market, its 8 vendors and its shoppers, and every swipe, deposit and dollar: 8 Saturdays (Aug 29 to Oct 17, 2026), 177 card swipes, 48 vendor turn-ins and 53 bank lines. The generator is deterministic: same seed, same files.
- **Real:** how SNAP token and match programs work (no hot food on SNAP, match capped per shopper per day, the grant reimbursing redeemed match, vendors paid per token), and double-entry accounting.
- **Planted mistakes and an answer key:** 9 mistakes, from a duplicated swipe to a fake bank-change email the day before payday, plus 3 traps. The agent is blocked from reading the answer key.
- **Score:** 9/9 caught, 9/9 right action, 0 false alarms, and Tuesday's payment batch exactly right.
- **90 automated tests.** They check the answer key against the raw files, grade the agent, test every ledger rule (unbalanced entries, unknown accounts, closed months), test every approval rule, and edit the data in "what if" scenarios to show the agent reasons instead of memorizing.

## How I built it

Plain Python with no dependencies (Claude is optional), so it runs anywhere with `python3 server.py`.

- `generate_data.py`: builds the fake market honestly, plants the mistakes, and writes the answer key.
- `agent.py`: eight reconciliation checks. It can only open an allow-list of files and fingerprints everything it reads.
- `ledger.py` and `books.py`: a small double-entry ledger in whole cents, plus the tie-outs.
- `gate.py`: the approval gate, with roles, segregation of duties, required notes and the audit trail.
- `explain.py`: "ask why". It sends one finding's records to Claude (`claude-opus-5-5`) as data, with instructions to cite them and never approve anything. It falls back to recorded reasoning if there's no key or no network.
- `server.py` and `static/index.html`: the web app and the "What if…?" scenarios.

I kept every money decision in tested, repeatable code, and used Claude where it shines: explaining a decision to a person who asks.

## Challenges I ran into

- **My own tests caught my own bug.** I planted 18 tokens for a vendor without subtracting them from what shoppers held, so fake shoppers spent tokens they didn't have. The answer-key tests failed, and I fixed the generator.
- **A "judge-style" scenario exposed a real flaw.** When I inflated a second vendor's count, the agent held that vendor but released Green Acres, which was still far above normal. Now it holds every unusually high count and splits the missing tokens between them in proportion.
- **An outside review broke it, and I fixed it.** Overlapping holds let one vendor be paid $441 against $273 owed, even though the books still "balanced". Approvals could be faked by sending a name. Approvals survived edits to the records. I reproduced all six weaknesses as failing tests, then fixed each one: per-payment blocker lists, server sign-in sessions, frozen runs, "books must tie out before any approval", and a hash-chained audit trail. A second round found four more, including a $940 double payment across runs and a stale screen approving a newer run, and I fixed those the same way: reproduce first, then fix.
- **Traps vs. real problems.** Checking tokens day by day flags the rainy Saturday falsely. Checking season to date gets it right.
- **[Anything else that was hard for you personally: building solo, learning accounting terms, time.]**

## Accomplishments I'm proud of

- Zero false alarms on the traps, and the books tie out after every possible decision.
- It holds money for the right reasons and says why, in words a volunteer market manager would understand.
- **[Your own: e.g. building it solo in a weekend.]**

## What I learned

**[Your own words. Ideas: what double-entry bookkeeping is, why segregation of duties matters, why the "system of record" matters when sources disagree.]**

## What's next

- Pull real records from a market's card terminal export and bank feed instead of a simulation.
- Let the agent draft the vendor and processor emails for open items, for a person to send.
- The monthly grant claim, prepared automatically with the write-offs already applied.
- A text-message approval for managers who are at the market, not at a desk.

## Built with
python, claude, anthropic-api, html, css, javascript, double-entry-accounting

## Tracks to select on Devpost
- The Hyphenated Enterprise: Autonomous Systems for Finance (Hyphenate)
- Best Solo Hack
- Anthropic: works best if the Inspiration section names a real community and how you know about the problem
- Microsoft "Built for the Real World": a production-minded finance agent
- Best Beginner Hack, if it's your first or second hackathon (check the rules)

## Links to add
- GitHub repo (push this folder; the README explains everything)
- Demo video (opens with the moment; see DEMO.md)
