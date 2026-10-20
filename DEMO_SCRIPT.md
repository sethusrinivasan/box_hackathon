# Reality Check — 3-Minute Video Demo Recording Script

A timed, second-by-second recording playbook for hackathon video submission and live executive judging.

> **Target Video Length:** 3 minutes (180 seconds)  
> **Structure:** 45s Pitch (Slides 1–3) ➔ 90s Live Demo (AWS Lambda) ➔ 45s Outcomes & Roadmap (Slides 5–6)  
> **Presenter Mode Deck:** [https://sethusrinivasan.github.io/box_hackathon/](https://sethusrinivasan.github.io/box_hackathon/)  
> **Live Web Demo:** [https://2c5gs4u2lsxjzi66dbxbr7iuzu0xhila.lambda-url.us-east-2.on.aws/](https://2c5gs4u2lsxjzi66dbxbr7iuzu0xhila.lambda-url.us-east-2.on.aws/)

---

## 1. Pre-Recording Setup Checklist

| Item | Recommendation / Setting |
| :--- | :--- |
| **Resolution** | 1920 × 1080 (1080p), 16:9 aspect ratio |
| **Browser Zoom** | 100% or 110% for crisp, legible typography |
| **Tab 1** | Pitch Deck: `https://sethusrinivasan.github.io/box_hackathon/` |
| **Tab 2** | Live Web Demo: `https://2c5gs4u2lsxjzi66dbxbr7iuzu0xhila.lambda-url.us-east-2.on.aws/` |
| **Tab 3** | GitHub Repo: `https://github.com/sethusrinivasan/box_hackathon` |
| **Deck Keyboard Shortcuts** | `Right` / `Space` = Next slide · `Left` = Previous slide · `P` = Presenter mode · `F` = Fullscreen |
| **Dual Monitor Setup (Optional)** | Open `P` (Presenter Mode) on monitor 2 for live countdown timer and speaker notes. |

---

## 2. Second-by-Second Recording Script

### Part 1: The Problem & The Solution (0:00 – 0:45)

#### [0:00 – 0:20] Slide 1 — Identify Risks Before Making Customer Commitments
* **On Screen:** Tab 1 on Slide 1. Fullscreen mode.
* **Stage Direction:** Keep cursor still or gesture gently to the flow diagram showing *Draft Commitment ➔ Scattered Docs ➔ Manual Review*.
* **Spoken Track:**
  > "Every day, sales teams make promises on features, delivery timelines, and performance in draft customer contracts. But verifying those promises today means digging through scattered folders and chasing product and operations teams for ad hoc answers.
  >
  > Because that process is fragmented and slow, customer commitments get signed before anyone spots the delivery risks."

#### [0:20 – 0:35] Slide 2 — One Contract In. An Evidence-Backed Review Out.
* **Action:** Press `Right Arrow` to advance to Slide 2.
* **Stage Direction:** Hover cursor briefly over the three Box ground-truth tiers: B1, B2, and B3.
* **Spoken Track:**
  > "Reality Check solves this by turning Box into an active ground-truth engine. 
  >
  > A field rep submits a draft contract. The agent extracts every single claim and checks it against three prioritized sources stored directly in Box: approved product documentation, roadmap and release plans, and historical delivery benchmarks."

#### [0:35 – 0:45] Slide 3 — Three Real Failure Modes Caught
* **Action:** Press `Right Arrow` to advance to Slide 3.
* **Stage Direction:** Mouse hovers across the three cards: Feature Availability, Delivery Deadlines, Performance Commitments.
* **Spoken Track:**
  > "It targets three distinct failure modes: features promised before they exist, delivery timelines that contradict historical benchmarks, and performance commitments that exceed validated lab specs."

---

### Part 2: Live Demo Walkthrough (0:45 – 2:15)

#### [0:45 – 1:00] Slide 4 ➔ Switch to Live Demo & Feature Flag
* **Action:** Press `Right Arrow` to Slide 4 ("Live Demo"). Click the blue **"Launch Live Web Demo ↗"** button (or switch to Tab 2).
* **On Screen:** The clean, dark-themed Reality Check live web demo.
* **Stage Direction:** Point out the top header, then pause on the **Evidence Retrieval** card.
* **Spoken Track:**
  > "Let's see it live on AWS. 
  >
  > Here is our live demo, running serverless on AWS Lambda Graviton. Notice the Evidence Retrieval toggle: it's backed by AWS SSM Parameter Store. While full-corpus works for small sets, our pgvector semantic retrieval indexes the Box corpus and slashes prompt tokens by 95% on larger document sets."

#### [1:00 – 1:30] Run the Northstar Contract Review
* **Action:** Click the primary blue button: **"Review the Northstar archive contract"**.
* **On Screen:** Review output renders instantly with summary banner (5 claims, 3 High risks, 1 Medium risk, 1 OK).
* **Stage Direction:** Slowly scroll down through the claim cards, highlighting the citations.
* **Spoken Track:**
  > "Let's review the Northstar Enterprise Archive contract. Instantly, the agent parses the agreement and surfaces 3 high-severity risks:
  >
  > First, in Section 2, the contract promises 'Smart Archive Search at launch' — but Box source B2 shows this feature is currently exploratory with no release date.
  >
  > Second, in Section 3, the contract promises migration completion within 30 days — but Box source B3 shows comparable 400 to 800 terabyte migrations took 58 to 72 days.
  >
  > Third, in Section 4, a 10 gigabyte per second throughput promise is flagged because our validated Box benchmark B1 only supports 5 gigabytes per second."

#### [1:30 – 1:55] Sourcing Exceptions & One-Click PDF Risk Report
* **Action:** Scroll to Section 5 ("LegacyVault direct import"), then scroll up and click **"Open PDF Risk Report"** (`/report.pdf?contract=northstar`).
* **On Screen:** The generated PDF opens in a new browser tab with clean executive styling, claim tables, and Box document citations.
* **Stage Direction:** Scroll smoothly down the 1-page PDF.
* **Spoken Track:**
  > "In Section 5, it even flags when a claim has *no* evidence in Box — prompt engineers often miss ungrounded claims, but Reality Check flags that LegacyVault direct import has no documented source.
  >
  > With one click, the field rep can generate this executive PDF Risk Report, or have the agent post these findings as Box comments directly on the draft file."

#### [1:55 – 2:15] Frugal Cost Economics & Architecture
* **Action:** Switch back to the demo tab and highlight the **Live Bedrock cost estimate** card.
* **Stage Direction:** Circle mouse around the token count and per-run cost figure.
* **Spoken Track:**
  > "Notice the cost transparency card: running this review on Amazon Bedrock costs approximately $0.0003 on Amazon Nova Lite, and about 3 cents on Claude 3.5 Sonnet.
  >
  > The entire infrastructure is frugal serverless: Graviton Lambdas with idle auto-parking, Aurora Serverless v2 scaling to zero ACU, and CloudWatch telemetry ensuring complete cost transparency."

---

### Part 3: Business Value & Future Vision (2:15 – 3:00)

#### [2:15 – 2:40] Slide 5 — Concrete Business Outcomes
* **Action:** Switch back to Tab 1. Press `Right Arrow` to Slide 5.
* **Stage Direction:** Gestured over the 3 outcome boxes: Identify Risks, Make Informed Commitments, Reduce Manual Checking.
* **Spoken Track:**
  > "Bringing this back to the business: Reality Check delivers three tangible outcomes:
  >
  > First, it protects the company by identifying risky commitments before contracts are signed.
  >
  > Second, it empowers field reps with verified, cited evidence for customer conversations.
  >
  > And third, it saves hours previously wasted chasing product and engineering teams across Slack and email."

#### [2:40 – 3:00] Slide 6 — Three Future Enhancements & Wrap-Up
* **Action:** Press `Right Arrow` to Slide 6.
* **Stage Direction:** Point out Suggested Wording, Owner Approvals, and Continuous Rechecks.
* **Spoken Track:**
  > "Looking ahead, we see three natural expansions:
  >
  > 1. Generating safe, evidence-backed fallback contract wording.
  > 2. Automatically routing risk exceptions to designated product owners in Box for expedited sign-off.
  > 3. And continuous rechecks that alert deal teams whenever roadmaps or capability benchmarks update in Box.
  >
  > Reality Check bridges sales velocity with ground-truth reality. Thank you!"

---

## 3. Quick Reference Card (Print / Split Screen)

```
[0:00 - 0:20]  SLIDE 1  The Problem: Sales commitments vs. scattered docs
[0:20 - 0:35]  SLIDE 2  Solution: 1 contract in -> 3 Box evidence tiers (B1, B2, B3)
[0:35 - 0:45]  SLIDE 3  3 Failure Modes: Features, Delivery dates, Performance
[0:45 - 1:00]  DEMO     AWS Lambda Demo + SSM Feature Flag (pgvector token savings)
[1:00 - 1:30]  DEMO     Run Northstar review: 3 High Risks (B2 roadmap, B3 benchmark, B1 spec)
[1:30 - 1:55]  DEMO     LegacyVault ungrounded claim + One-Click PDF Risk Report
[1:55 - 2:15]  DEMO     Bedrock Cost Card ($0.03/run) + Serverless-first architecture
[2:15 - 2:40]  SLIDE 5  Business Outcomes: Risk avoidance, evidence citations, time saved
[2:40 - 3:00]  SLIDE 6  Future Vision: Safe wording, Box owner approval routing, continuous rechecks
```

---

## 4. Judge Q&A Cheat Sheet

* **Q: How does Reality Check integrate with Box?**  
  *A:* "It connects via Box API v10 using Client Credentials Grant (CCG) scoped strictly to designated Contracts, Evidence, and Reports folders. The agent reads draft PDFs, scans Box evidence docs, posts threaded comments on contract files, and uploads the generated risk report PDF back to Box."

* **Q: Why are severity levels not decided by the LLM?**  
  *A:* "To prevent hallucinated risk assessments. Claude extracts the claims and matches them against evidence, but severity logic (e.g., Exploratory roadmap item = High risk; unbenchmarked throughput = High risk) is codified deterministically in our Python rule engine."

* **Q: How does this scale cost-wise to hundreds of evidence files?**  
  *A:* "We implemented pgvector semantic chunk retrieval (documented in `DESIGN_pgvector_evidence_retrieval.md`). Instead of stuffing the entire corpus into the prompt, we embed evidence once and retrieve only the top-k relevant chunks per claim, reducing prompt tokens by ~95%."

* **Q: What is the idle cost of this infrastructure?**  
  *A:* "Near zero. The web demo runs on an arm64 Lambda with idle auto-shutdown after 3 days of inactivity. The daily cost tracker collector runs on EventBridge Scheduler and writes to an Aurora Serverless v2 PostgreSQL cluster configured to auto-pause at 0 ACU."
