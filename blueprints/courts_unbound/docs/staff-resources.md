# 📑 Staff Resources

## Staff roles
- {@High Council}: owners of the rules. Final word on rulings and claims.
- {@Moderator}: conduct, tickets, warnings.
- {@Lorekeeper}: applications, ledgers, claims, lore.
- {@Storyteller}: missions, sagas, regents and NPCs. Not staff.
- Everyone above except Storytellers also holds {@Staff}, which opens the staff channels.

## Setup that lives outside the blueprint
- **The Courts bot** (Cauldron) gives {@New Arrival} on join, welcomes newcomers in {#waiting-room}, flags accounts under six months here in the staff log, and runs the {#roles} and {#submit-a-ticket} panels. Run `/court-admin setup` once, and again after recreating any of those channels.
- **Re-running the build** re-syncs every doc. Edit docs in the repository, not on Discord.

## Accepting a newcomer
Website applications arrive as private threads in {#submit-a-ticket}, with **Accept** and **Close** buttons on the staff ping.
- Read the answers and the sample. Answers that only define the terms, or read as copied, go back for a rewrite.
- **Accept** checks the birthday (21+), then asks which proof covers the account rule: the account is six months old, a member vouched (`/vouch`, {@Approved Player} only), or a censored ID was checked in the thread. It gives {@Member} and removes {@New Arrival}.
- **IDs:** delete the image once checked. The bot records which proof was given, never its details.

## Approving a character
1. Read the whole post. Personality and history first, abilities second. Answers under **In your own words** that only define the terms, or read as copied, go back for a rewrite.
2. Check rank, slots, tiers, claims, face claim and the no-canon rule.
3. Ask for changes in the post. Vote in {#application-review} for claims and anything unusual.
4. On approval: tag **Approved** and run `/char approve` in the post. It gives {@Approved Player} and the allegiance, tier and romance roles; its options fix a post that strays from the template. Then set the nickname.
5. Add titles as earned: {@House Head}, {@Master}, {@Shard-bearer} and so on.

## Grading
For each character in a request from {#thread-inbox}:
- **Marks** = graded words ÷ 10 × quality (0.5 / 1 / 1.5) × type (solo 1, group 1.25, saga 1.5 active only). Cap 2,000 words a post, 20,000 solo words a week. Then apply any business bonus.
- **Progress:** 1 thread toward the named ability (2 for a saga), × a declared boost. Check the weekly limits.
- **Renown / Bounty:** mission tier with diminishing returns; captures and kills per {#renown}.

Record one line per character in {#ledger-log}:
```
Character · +Marks (total) · +Renown/Bounty (total) · +progress: ability (running count) · thread link
```
React ✅ on the request when done.

## Regents
A regent holds each empty throne. Storytellers voice them: they issue court missions, writs and promotions until a claimant takes the throne. Regents never fight player characters outside a saga.

## Scrying
When a scry is posted in a purse, roll **1d7** where everyone can see it. Each number is a court's shard, in the order of {#shard-tracker}. Update the tracker and the {@Shard-bearer} role.

## DE moderation
Any staff member may intervene in a DE thread when asked (see {#combat}). Post rulings in the thread, signed. Log deaths in {#graveyard} with a link.
