# Reality Check — Pitch Deck

> **Audience:** Executives & Hackathon Judges  
> **Total Time:** 3 minutes (60 seconds speaking time + 2 minutes live demo)  
> **Running Example:** Enterprise Amazon S3 Archive Migration

---

## Slide 1 — Identify risks before making customer commitments

### Visual Layout
```
[ Draft customer commitment ]
              │
              ▼
[ Search scattered docs ] ──► [ Ask product ] ──► [ Follow up with ops ]
                                                         │
                                                         ▼
[ Assess risk manually ] ◄── [ Piece together answers ] ◄┘
```

### On-Slide Content
* **Today's Reality:** Sales teams make promises on features, delivery timelines, and performance without fast access to delivery ground truth.
* **The Manual Friction:** Checking a deal means digging through scattered folders, messaging product managers, and chasing operations for historical benchmarks.
* **The Business Impact:** Risks slip through unnoticed until *after* the customer commitment has already been made.

---
**Speaker Notes (15 seconds):**
> "Sales teams frequently need to make commitments about features, delivery dates, and performance in draft contracts. Today, verifying those promises requires searching through scattered documents and chasing product and operations teams for ad hoc answers. Because that process is fragmented and slow, commitments get made before the real delivery risks are spotted. Reality Check solves this by identifying those risks *before* the commitment is signed."

---

## Slide 2 — One contract in. An evidence-backed review out.

### Visual Layout
```
┌─────────────────────────────────┐
│ Draft sales contract            │
├─────────────────────────────────┤
│ Ground-truth documents in Box:  │
│  • Approved product docs (B1)   │ ──► ┌──────────────────────┐ ──► ┌───────────────────────────┐
│  • Roadmap & release plans (B2) │     │ Contract Risk Review │     │ Risks identified before   │
│  • Delivery reviews (B3)        │     └──────────────────────┘     │ customer commitment       │
└─────────────────────────────────┘                                  └───────────────────────────┘
```

### On-Slide Content
* **Single Input:** Sales submits one draft contract.
* **Box Ground Truth (Three Prioritized Sources):**
  1. **Approved product documentation (B1):** What can be promised today (supported configurations, benchmarks, limits).
  2. **Product roadmap & release plans (B2):** What is planned and committed vs. exploratory.
  3. **Project closeouts & delivery reviews (B3):** Historical performance on similar deliveries.
* **Evidence-Backed Output:** Comments posted directly on the contract in Box plus a concise one-page risk report.

---
**Speaker Notes (10 seconds):**
> "Reality Check takes a single draft contract and checks every commitment against the company's verified ground truth in Box—approved product docs, roadmaps, and delivery closeouts. The salesperson submits the draft and immediately sees which claims carry delivery or feature risks before committing."

---

## Slide 3 — A simple decision for every claim

### Visual Layout
```
                          ┌────────────────────────┐
                          │ Contract claim promise │
                          └───────────┬────────────┘
                                      │
                                      ▼
                             Source found in Box?
                                ├── No ──► [ No source found · HIGH ]
                                │
                                └── Yes
                                      │
                                      ▼
                                Claim matches?
                                ├── Yes ─► [ No risk · NONE ]
                                │
                                └── No ──► What risk category?
                                             ├── Feature availability   ──► [ HIGH ]
                                             ├── Delivery deadline      ──► [ HIGH ]
                                             └── Performance commitment ──► [ MEDIUM ]
```

### On-Slide Content
* **Deterministic Risk Model:** Fixed severities in code—never left to LLM guesswork.
* **Rigorous Matching Rules:**
  - Roadmap items marked *Target* or *Exploratory* do not support committed dates.
  - Benchmarks only support tested conditions; burst is not sustained.
  - Missing sources are flagged as High severity (unverified, not proven false).

---
**Speaker Notes (10 seconds):**
> "Every claim follows a strict, deterministic rule: if evidence confirms it, there is no risk. If a promised feature is only exploratory, or a deadline beats historical delivery times, it's flagged as High risk. Unverified claims are flagged as High. A human reviewer always makes the final call."

---

## Slide 4 — Live demo: five claims, one review

### Visual Layout
* **Center Question:** *“What risks should sales see before committing?”*
* **Scenario:** Northstar Enterprise S3 Archive Migration Agreement (5 Claims)

| Claim in Draft Contract | Cited Box Ground Truth | Result | Severity |
|:---|:---|:---|:---:|
| **§1 Encryption at rest** | **B1** Approved Solution Guide confirms support in US-West | **No risk** | `NONE` |
| **§2 Smart Archive Search at launch** | **B2** Roadmap lists feature as Exploratory with no ship date | **Feature availability** | `HIGH` |
| **§3 Migration within 30 days** | **B3** Past 400–800 TB migrations took 58–72 days | **Delivery deadline** | `HIGH` |
| **§4 10 GB/s sustained throughput** | **B1** Benchmark validates 5 GB/s in agreed setup | **Performance commitment** | `MEDIUM` |
| **§5 LegacyVault direct import** | **No source** found across Box evidence corpus | **No source found** | `HIGH` |

---
**Presenter Walkthrough Notes (2 minutes total):**
* **0:00 – 0:20 | Draft Contract:** *"Here is the Northstar draft agreement. Sales is proposing 5 specific commitments: encryption, Smart Archive search at launch, a 30-day migration, 10 GB/s throughput, and direct import from their legacy vault."*
* **0:20 – 0:35 | Box Connection:** *"We submit this agreement. The agent pulls the document and cross-references our company's approved evidence folders in Box."*
* **0:35 – 1:00 | Summary Results:** *"Instantly, we get a clear verdict: 1 claim supported with No Risk, 3 Risks Identified, and 1 No Source Found."*
* **1:00 – 1:30 | Delivery Deadline Risk:** *"Looking at Section 3, sales promised migration within 30 days. But our delivery review in Box shows comparable 400 to 800 terabyte migrations took 58 to 72 days. This is flagged High before anyone commits."*
* **1:30 – 1:50 | Feature & Missing Sources:** *"Section 1 encryption is verified by documentation. But Section 2 Smart Search is only exploratory on the roadmap, and Section 5 LegacyVault import has zero supporting documentation in Box—both flagged High."*
* **1:50 – 2:00 | Report:** *"The rep gets inline comments directly on the Box document and a 1-page risk report PDF ready for review."*

---

## Slide 5 — Three benefits

### Visual Layout
```
┌─────────────────────────────────┐   ┌─────────────────────────────────┐   ┌─────────────────────────────────┐
│     1. Identify Risks Early     │   │   2. Make Informed Promises     │   │   3. Eliminate Manual Chasing   │
├─────────────────────────────────┤   ├─────────────────────────────────┤   ├─────────────────────────────────┤
│ Surface unsupported features,   │   │ Arm field teams with verified,  │   │ Reduce hours spent digging      │
│ unrealistic delivery dates, and │   │ cited evidence behind every     │   │ through folders and waiting on  │
│ unbenchmarked performance claims│   │ proposed commitment before sign │   │ ad hoc answers from product/ops │
└─────────────────────────────────┘   └─────────────────────────────────┘   └─────────────────────────────────┘
```

### On-Slide Content
1. **Identify risks before committing:** Surface unsupported features, delivery dates, and performance promises.
2. **Make informed commitments:** Give sales source-backed evidence about each proposed claim.
3. **Reduce manual checking:** Spend less time searching documents and chasing product and operations teams.

---
**Speaker Notes (15 seconds):**
> "Reality Check delivers three concrete outcomes: First, it spots delivery, availability, and performance risks before commitments are signed. Second, it equips sales teams with factual evidence and document citations for customer discussions. And third, it saves field teams hours of manual searching and inter-department chasing."

---

## Slide 6 — Three future enhancements

### Visual Layout
```
┌─────────────────────────────────┐   ┌─────────────────────────────────┐   ┌─────────────────────────────────┐
│      1. Suggested Wording       │   │       2. Owner Approvals        │   │     3. Continuous Rechecks      │
├─────────────────────────────────┤   ├─────────────────────────────────┤   ├─────────────────────────────────┤
│ Propose contract language and   │   │ Automatically route exceptions  │   │ Continuously re-evaluate draft  │
│ safe fallback terms supported   │   │ directly to product or ops      │   │ contracts when roadmaps or      │
│ directly by verified evidence   │   │ owners for expedited sign-off   │   │ benchmarks update               │
└─────────────────────────────────┘   └─────────────────────────────────┘   └─────────────────────────────────┘
```

### On-Slide Content
1. **Suggested wording:** Propose contract language supported by the evidence.
2. **Owner approvals:** Route exceptions directly to product or operations owners.
3. **Continuous rechecks:** Automatically re-evaluate contracts when roadmaps or capabilities change.

---
**Speaker Notes (10 seconds):**
> "Looking ahead, we see three natural expansions: generating evidence-backed alternative wording, routing risk exceptions directly to product owners for approval, and continuously monitoring draft contracts as roadmaps update. Thank you."
