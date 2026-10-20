# Reality Check — Hackathon Submission Dossier

**Sales Contract Risk Review against Box Ground Truth, Powered by Claude on Amazon Bedrock**

---

## 1. Project Overview & Quick Links

| Category | Details / Link |
| :--- | :--- |
| **Project Name** | **Reality Check** |
| **Tagline** | Ground draft sales contract commitments against verified Box evidence before you sign. |
| **Live Web Demo** | [https://2c5gs4u2lsxjzi66dbxbr7iuzu0xhila.lambda-url.us-east-2.on.aws/](https://2c5gs4u2lsxjzi66dbxbr7iuzu0xhila.lambda-url.us-east-2.on.aws/) |
| **Interactive Pitch Deck** | [https://sethusrinivasan.github.io/box_hackathon/](https://sethusrinivasan.github.io/box_hackathon/) (GitHub Pages) |
| **Video Recording Script** | [`DEMO_SCRIPT.md`](./DEMO_SCRIPT.md) (Exact 3:00 timeline + Judge Q&A) |
| **Pitch Deck Notes** | [`PRESENTATION.md`](./PRESENTATION.md) (6-slide executive deck) |
| **Product Requirements** | [`PRD.md`](./PRD.md) (Complete product specification) |
| **GitHub Repository** | [https://github.com/sethusrinivasan/box_hackathon](https://github.com/sethusrinivasan/box_hackathon) |
| **Primary Tracks** | Box AI · Enterprise Productivity · Best Use of Box Platform · Frugal Serverless AI |

---

## 2. Elevator Pitch (The 60-Second Summary)

Sales teams frequently need to make commitments about features, delivery dates, and performance in draft customer contracts. Today, verifying those promises requires searching through scattered documents and chasing product and operations teams across Slack and email. Because that process is fragmented and slow, commitments get made before the real delivery risks are spotted.

**Reality Check** transforms Box into an active ground-truth review engine. A field rep submits a single draft contract. The agent extracts every commitment, verifies it against three prioritized evidence sources stored in Box (approved product docs, roadmaps, and delivery benchmarks), categorizes risks deterministically, and writes findings as threaded comments directly on the draft file in Box, alongside a generated one-page executive PDF risk report.

---

## 3. The Problem & Business Context

In high-stakes enterprise sales (e.g., enterprise cloud storage, large-scale data migrations, multi-year software licensing), sales reps face intense deal pressure. To win the deal, draft contracts often incorporate custom customer requirements:
* Promised features that are still in early discovery or backlog.
* Aggressive migration completion schedules (e.g., 30 days) that ignore historical benchmarks (e.g., 60+ days).
* Throughput or performance SLAs that exceed validated operational limits.

### Why Manual Checking Fails:
1. **Information Silos:** Product docs live in one folder, roadmaps in another, and delivery post-mortems in an operations archive.
2. **High Latency:** Reps spend hours chasing busy product managers and delivery engineers for ad hoc answers.
3. **The Commitment Trap:** Risks slip through unnoticed until *after* the agreement is signed, resulting in costly SLA penalties, executive escalations, and damaged customer trust.

---

## 4. What Reality Check Does

Reality Check requires only **one user input**: a draft sales contract. The system takes care of the rest:

```
[ Draft Contract in Box ]
           │
           ▼
[ Reality Check Agent (Claude on Bedrock) ]
           │
           ├─ Reads 3 Prioritized Box Ground-Truth Sources:
           │    • B1: Approved Product Docs (What can be promised today)
           │    • B2: Roadmap & Release Plans (What is planned vs. exploratory)
           │    • B3: Delivery Reviews & Benchmarks (What similar projects took)
           │
           ├─ Deterministic Rule Engine: Severity fixed in code (never guessed by LLM)
           │
           ├─ Box Integration: Threaded comments posted on contract claims
           └─ Box Reports: 1-page executive PDF risk report uploaded to Box
```

### Three Real Failure Modes Caught
1. **Feature Availability Risks:** Promises a capability that is unsupported or marked as "Exploratory" on the roadmap (e.g., Smart Archive Search promised at launch when no release date exists).
2. **Delivery Deadline Risks:** Promises a delivery timeline that contradicts historical project benchmarks (e.g., promising 30-day migration when comparable 400–800 TB migrations took 58–72 days).
3. **Performance Commitment Risks:** Promises throughput or latency exceeding documented engineering benchmarks (e.g., promising 10 GB/s when specs validate 5 GB/s).
4. **Ungrounded Claims ("No Source Found"):** Promises capabilities with zero documentation in Box (e.g., LegacyVault direct import), immediately flagging an unverified promise before signing.

---

## 5. Architectural Highlights & Key Differentiators

### A. Deterministic Severity (Zero LLM Hallucination on Risk Classification)
In enterprise compliance, allowing an LLM to subjectively assign risk severity leads to inconsistency and hallucination. Reality Check strictly decouples the AI model from the severity engine:
* **The LLM (Claude 3.5 Sonnet on Bedrock):** Reads contract text and evidence passages, extracts discrete claims, and matches claims to supporting or refuting text.
* **The Python Rule Engine:** Applies deterministic classification logic. If an evidence passage contains "Exploratory", it is classified as `High` severity. If throughput is unbenchmarked, it is `High`. If grounded, it is `No risk`.

### B. Scalable Semantic Evidence Retrieval (`pgvector`)
Stuffing an entire enterprise document library into an LLM context window is expensive and hits token limits.
* We designed and implemented a **pgvector semantic retrieval pipeline** ([`docs/DESIGN_pgvector_evidence_retrieval.md`](./docs/DESIGN_pgvector_evidence_retrieval.md)).
* Evidence documents are chunked and embedded once; reviews retrieve only the top-k relevant passages per claim.
* **Result:** **~95% fewer evidence prompt tokens** on a 100-document corpus.
* Managed via a feature flag backed by **AWS Systems Manager (SSM) Parameter Store**, allowing instant runtime toggling without redeployment.

### C. Frugal, Serverless-First Infrastructure (Near-$0 Idle Cost)
The entire solution is defined in **AWS CDK v2** across 4 modular stacks:
1. **`RealityCheckWebDemo`**: Public AWS-native web portal on an AWS Lambda Function URL backed by Amazon S3 (`contracts/`, `evidence/`, `reports/`) and live Bedrock reasoning. Features drag-and-drop contract PDF uploading, instant review execution, Graviton (arm64) runtime, reserved concurrency 5, and an **idle auto-shutdown** mechanism that parks the function after 3 days of zero traffic.
2. **`RealityCheckObservability`**: Dedicated CloudWatch dashboard tracking request rates, p99 latencies, Bedrock costs, and synthetic canary health.
3. **`RealityCheckCanary`**: Periodic automated synthetic health checks running against all endpoints.
4. **`RealityCheckCostTracker`**: Daily holistic AWS cost collection via EventBridge Scheduler writing directly to **Aurora Serverless v2 PostgreSQL (express configuration, min 0 ACU)** using **IAM database authentication** (no stored passwords, no Secrets Manager fees).

### D. Real-World Bedrock Economics
* Cost per review on Amazon Nova Lite: **~$0.0003**
* Cost per review on Anthropic Claude 3.5 Sonnet: **~$0.03**
* Live cost estimation card integrated directly into the web UI for complete financial transparency.

---

## 6. Technologies Used

* **Box Platform:** Box Python SDK v10, Client Credentials Grant (CCG), Box File Comments API, Box Folder Search & Content Download API.
* **AI & LLM Services:** Anthropic Claude 3.5 Sonnet / Amazon Nova Lite via Amazon Bedrock Converse API (`submit_review` tool use).
* **Cloud Infrastructure (AWS):** AWS Lambda (Graviton arm64), Amazon Aurora Serverless v2 PostgreSQL, Amazon CloudWatch, AWS Systems Manager (SSM) Parameter Store, AWS EventBridge Scheduler.
* **Infrastructure as Code:** AWS CDK v2 (Python).
* **Document Engine:** Pure-Python dependency-free PDF generator ([`agent/minipdf.py`](file:///home/home/github/box_hackathon/agent/minipdf.py)).
* **Security & Testing:** CodeQL, Bandit AST security analysis, GitHub SBOM, Python `unittest`, 100% clean CI workflows.

---

## 7. Technical Challenges Overcome

1. **CloudFormation / CDK Gap with Aurora Express Configuration:**
   * *Problem:* Aurora PostgreSQL "express configuration" (IAM-only, internet access gateway, no NAT gateway) is available in Console/CLI (`WithExpressConfiguration=true`), but AWS CloudFormation (`AWS::RDS::DBCluster`) lacks this property.
   * *Solution:* Built an `AwsCustomResource` in CDK to invoke `rds:CreateDBCluster` directly, avoiding expensive NAT gateways ($32/month) and maintaining a frugal architecture. Documented as [GitHub Issue #3](https://github.com/sethusrinivasan/box_hackathon/issues/3) and drafted an official feature request in [`docs/aws-feature-request-express-config-cfn.md`](./docs/aws-feature-request-express-config-cfn.md).
2. **Aurora Serverless v2 Cold-Resume Latency:**
   * *Problem:* Scaling Aurora Serverless v2 down to 0 ACU saves compute costs when idle, but resuming takes ~15 seconds on initial connect.
   * *Solution:* Implemented an automatic retry handler with fresh IAM auth token generation to ride through resumes gracefully, and instrumented `DbConnectLatencyMs` metrics on our CloudWatch dashboard.
3. **Pure-Python PDF Generation in Lambda:**
   * *Problem:* Standard PDF rendering tools (wkhtmltopdf, Puppeteer, ReportLab C-extensions) bloat Lambda container images and increase cold starts.
   * *Solution:* Developed [`agent/minipdf.py`](file:///home/home/github/box_hackathon/agent/minipdf.py) — an ultra-lightweight, zero-dependency pure-Python PDF writer that compiles clean executive reports in sub-millisecond execution times.

---

## 8. Accomplishments & Verification

* **Full Fork & Upstream Alignment:** PR #4 and PR #6 merged into `main`; upstream PR #15 (`bearbearpanpan/box_hackathon#15`) polished with CloudWatch tags, security suppression annotations, and comprehensive documentation.
* **100% Test Suite Pass:** 44 unit and integration tests passing across agent logic, S3 storage, custom uploads, PDF generation, cost calculations, canary checks, and telemetry.
* **Live, High-Availability Deployments:**
  * Public AWS Lambda Web Demo verified live and responsive.
  * Standalone 16:9 interactive Pitch Deck deployed to GitHub Pages with built-in Presenter Mode (`P`), slide timers, and live demo launching.
* **Security & Clean Code:** Zero CodeQL vulnerabilities, clean Bandit security scans, and passing SBOM pipelines.

---

## 9. Future Roadmap

1. **Evidence-Backed Alternative Wording:** Generate safe, contractually sound fallback clauses directly citing verified Box evidence when a risk is flagged.
2. **Automated Box Owner Approvals:** Integrate with Box Relay or custom webhooks to route flagged risk exceptions directly to designated product managers or operations leads for expedited sign-off.
3. **Continuous Rechecks via Box Webhooks:** Automatically re-evaluate pending draft contracts whenever ground-truth evidence documents (roadmaps or benchmarks) are updated in Box.

---

## 10. The Submission Checklist

- [x] Working Live Demo deployed on AWS Lambda URL
- [x] Interactive Presentation Pitch Deck deployed on GitHub Pages
- [x] Timed 3-Minute Video Recording Playbook & Judge Q&A (`DEMO_SCRIPT.md`)
- [x] Detailed Product Requirements Document (`PRD.md`)
- [x] Complete Architectural & Frugal Cost Documentation (`README.md`)
- [x] Semantic Evidence Retrieval Design (`docs/DESIGN_pgvector_evidence_retrieval.md`)
- [x] All 38 Unit & Integration Tests Passing
- [x] CI/CD Workflows Green (CodeQL, Security, SBOM, Pages)
- [x] Upstream Pull Request Polished (`bearbearpanpan/box_hackathon#15`)
