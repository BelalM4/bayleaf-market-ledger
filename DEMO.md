# Demo script (2 minutes)

The same script works for the live table demo and the demo video. Per Hyphenate's
instructions, it **opens with the moment**. Words to say are in quotes; what to
click is in *italics*. Times are targets, not rules.

## Before judges arrive (2 minutes)

1. `python3 server.py`, then open http://localhost:8000 and make the window large.
2. Sign in as **Jordan Lee (market manager)**, PIN **2468** (a demo PIN for the fake market).
3. Click **Reset demo** so every decision is pending again (the old run is archived, not deleted), and stay on the **Needs a decision** tab.
4. Laptop charging; notifications off (Focus mode).
5. No wifi needed: "Ask why" falls back to the agent's recorded reasoning on its own.

---

## 0:00 – 0:25 · The moment

*Screen: the purple card at the top, "Oct 17: 22 more MATCH tokens turned in than exist."*

> "Last Saturday, the vendors at this farmers' market handed in **280** red match tokens.
> But the market's own records say only **258** of those tokens could exist: what shoppers were
> still holding, plus what the booth handed out. So 22 of them aren't real.
>
> My agent caught that **before** paying anyone. It noticed Green Acres Farm turned in 168,
> when it usually turns in about 110, and held that $168. Every other vendor still gets paid on time."

## 0:25 – 0:45 · What it is

*Point at the line under the title.*

> "This is Bayleaf Market Ledger. It fills two seats at a farmers' market food-stamp program:
> the **token program bookkeeper** and the **vendor payout clerk**. Shoppers swipe their EBT card
> for wooden tokens, plus free match tokens paid for by a grant. Vendors turn the tokens in, and the
> market pays them. Four records are supposed to agree: the card terminal, the bank, the volunteers'
> token counts, and the grant. They don't."

## 0:45 – 1:05 · It defends its decision

*In the "Ask why" box on the same purple card, click **"Why not just pay it?"***

> "If anyone asks why, it answers from the evidence: the exact file and line, the rule,
> and what would change its mind, like a recount."

(If Claude is connected the answer comes from Claude. If not, it's labeled "Agent's
recorded reasoning". Say whichever one is true.)

## 1:05 – 1:30 · It knows when to ask, and it can't approve itself

*Click **Let the agent try to approve it** on the same purple card.*

> "The agent prepares everything, but it can't move money. Watch: it tries to approve its own request…"

*Red message: "Segregation of duties…"*

> "…and the gate refuses. Whoever requests a payment can't approve it."

*Choose "Pay only the tokens that can exist", then click **Decide as Jordan Lee**.*

> "The market manager decides. That posted balanced journal entries, and the books still agree:"

*Point at the **Books: 17/17 agree** box.*

## 1:30 – 1:50 · It catches what others miss, and isn't fooled by traps

*Click the **Proof** tab.*

> "I planted nine mistakes in this data and kept an answer key the agent isn't allowed to open.
> It catches all nine, picks the right action each time, and raises zero false alarms.
> That includes three traps that look wrong but aren't, like a rainy Saturday when more tokens
> came back than went out. It explains why that one's fine on the 'Checked & fine' tab."

## 1:50 – 2:00 · Close

> "Nine catches, zero false alarms, books that balance after every decision, and nothing paid
> without a person signing off. Want to try a scenario of your own?"

*Click **What if…?** and hand them the trackpad.*

---

## If a judge says "try our own scenario"

Use the **What if…?** tab. Good ones to offer:

| They ask | Click | What happens |
|---|---|---|
| "What if the count was honest?" | Green Acres' count was honest | The hold disappears and Green Acres is paid in full |
| "What if another vendor inflates too?" | Change any vendor's token count: Sunny Slope, Oct 17, match, +70 | Both vendors are held, and the 92 missing tokens are split between them |
| "What if that late deposit never came?" | The Sep 19 late deposit never arrives | "Late, fine" becomes "missing, call the processor" |
| "What if another fake email arrives?" | A second bank-change email arrives | Sunny Slope's payment is held too |

It never touches the real books: each run uses a scratch copy.

## Likely questions, and honest answers

**"Is any of this real?"**
"The market and the data are made up: 8 Saturdays, 177 card swipes, 53 bank lines. The way the
program works is real. Many states run SNAP token and match programs like this: no hot food on
SNAP, match capped per shopper, a grant paying for redeemed match."

**"How do you know it works?"**
"101 automated tests. They check that the answer key matches the raw files, grade the agent,
test every ledger rule and every approval rule, and change the data to make sure the agent is
reasoning rather than memorizing."

**"Why not just use an LLM for everything?"**
"Money decisions have to be repeatable and auditable, so the checks and the ledger are plain code
with tests. Claude does what it's good at: explaining a decision in plain language when someone
asks, using only the evidence."

**"What can the agent access?"**
"Only the ten record files it needs. It's blocked from the answer key. It can post routine entries
and request approvals, but it can't approve anything or send money."

**"What about time: does March affect June?"**
"Yes. The token check runs season to date, which is why the rainy-day trap isn't a false alarm.
Late deposits get matched to earlier days. And it projects the grant running out around Nov 7,
about six Saturdays before the season ends."

**"What did you find hard?"**
"Telling real problems from traps. My own tests caught a bug where I'd planted tokens without
taking them out of what shoppers held. And when I tried the 'inflate a second vendor' scenario,
the agent at first released the wrong vendor. Now it holds every unusual count."

**"Couldn't someone just pretend to be the manager?"**
"No. Approvals come only from a signed-in session on the server, not from anything the page sends. Wrong PINs are refused and logged. The agent can't sign in at all."

**"What if someone edits the records after you've approved things?"**
"Each run is tied to fingerprints of the files it read. If any file changes, that run and its approvals are archived as superseded and the agent starts over. Old approvals never carry over to new findings. And the audit trail is hash-chained: delete or edit it, and the app refuses to start."

**"Why can't the manager just override?"**
"They can. Overriding takes a written note, and the books record the 22 impossible tokens as a
loss, so it's visible forever."

## Recording the video (Mac)

1. QuickTime Player → File → New Screen Recording (or press ⌘⇧5) → record the browser window, with your microphone on.
2. Click **Reset demo** first, then follow the script. One take under 2:15 is fine.
3. Upload unlisted to YouTube and paste the link into Devpost.
