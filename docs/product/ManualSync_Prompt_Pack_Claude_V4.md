# ManualSync - Claude Prompt Pack V3

Use the prompts sequentially. Do not implement broad changes before Prompt 0 and Prompt 1 are complete.

## Global product doctrine
ManualSync must become a complete technical-documentation platform. Translation is a first-class pillar, but the ManualSync vendor is not a translation agency. Customers may execute translation through their own agency, CAT/TMS, internal linguists or customer-configured AI/MT.

The strategic goal is NOT "clone Paligo". Reach parity on table-stakes and establish measurable superiority in legacy-first onboarding, revision/change intelligence, explainable technical-safe reuse, zero-training UX and layout-aware publishing.

Never claim a capability is complete because tests compile. Never claim "better than X" without a reproducible benchmark.

---

## Prompt 0 - Freeze and baseline
Read the full V3 handoff and Final Candidate. Do not modify files.
1. Map architecture, data model, launch/install/update, persistence, core modules, UI and tests.
2. Run all baseline tests available in the environment.
3. Separate: genuinely available; backend-only; partial UX; missing; commercial blocker.
4. Produce a BASELINE REPORT with exact PASS/FAIL and file paths.
5. If the Final Candidate still lacks the final Windows gate, do not start broad refactoring.

Deliverable: BASELINE_V3.md.

---

## Prompt 1 - Evidence-based competitor and objection audit
Research current official documentation plus credible user reviews for Paligo, Tridion Docs, SCHEMA ST4/Quanos, Heretto, MadCap IXIA/Flare, EKR Orchestra, FrameEditor NEXT and other directly relevant products.

Analyze:
- structured authoring/reuse;
- variables, conditions and variants;
- revision/change management;
- legacy migration/onboarding;
- translation exact/context/fuzzy/TM workflows;
- technical safety and explainability;
- publishing/layout;
- collaboration/integrations;
- setup, onboarding and learning curve.

Explicitly test the two objections:
A. "We already have something similar."
B. "We do not have time to train staff on another system."

Do not code. Produce:
1. parity requirements;
2. competitor weaknesses supported by evidence;
3. areas where competitors are already strong and ManualSync must not underestimate them;
4. five differentiation bets;
5. measurable UX/adoption acceptance criteria.

Deliverable: COMPETITIVE_AND_ADOPTION_GAP_V3.md.

---

## Prompt 2 - Zero-training UX architecture
Design ManualSync so a normal user can work without knowing XML, DITA, XLIFF, TM internals, repository IDs or workflow jargon.

Requirements:
- guided mode as default;
- advanced mode for specialists;
- one "recommended next action" based on project state;
- auto-detect language, document role, revision and likely old/new/translation relationships;
- inline explanation of every blocked state with a concrete next action;
- no dead-end buttons;
- role-oriented Home for writer, reviewer, translation coordinator and release manager;
- progressive disclosure;
- bulk actions for repetitive tasks;
- undo/recovery where safe.

Acceptance targets:
- first useful result <= 15 minutes on prepared legacy documents;
- 90%+ first-attempt completion of 5 core tasks in novice usability testing;
- zero requirement to understand XML/DITA/XLIFF for standard flows;
- all blocking errors contain cause + remedy.

Do not implement until wireflow/state-machine design and tests are reviewed.

---

## Prompt 3 - Legacy onboarding and coexistence
Build a "bring what you already have" onboarding path.

Inputs:
- Word/DOCX;
- PDF;
- IDML/InDesign template/source where available;
- folders with old/new revisions and language variants;
- existing TMX/XLIFF/TM SQLite later.

The system should:
- inspect files;
- infer likely language, revision, publication, source/translation relationships;
- propose a project structure;
- show confidence and ask only on ambiguous items;
- preserve source provenance;
- allow value before a full migration.

Design coexistence adapters so ManualSync can augment an existing CCMS/TMS instead of requiring immediate replacement.

Acceptance: a legacy project can be onboarded without manual repository engineering.

---

## Prompt 4 - Professional Authoring Studio
Design and implement a production authoring experience for manuals, procedures, service content and technical datasheets.

Capabilities:
- topics/sections/blocks;
- reusable components;
- warning/caution/note/procedure/task blocks;
- native tables;
- figures/assets/captions;
- variables;
- conditional content;
- product/model/market/audience variants;
- metadata, review status and revision history;
- search and reuse browser;
- comments/review workflow;
- source evidence/provenance.

Reuse current IndustrialRepository/CCMS structures where sound. Do not break R18 invariants without migration tests.

Acceptance: a technical writer can create and update a complete small manual without editing raw files or IDs.

---

## Prompt 5 - Grounded Authoring integrated into the editor
Turn Grounded Authoring into an in-editor technical writing assistant.

Modes:
- draft paragraph;
- procedure;
- warning/caution;
- troubleshooting;
- technical description;
- revision summary;
- datasheet narrative.

Every proposal must expose:
- source evidence;
- technical values/codes used;
- conflicts;
- unsupported claims;
- approval state.

Never auto-approve safety-critical content.
Acceptance tests must cover changed numbers, conflicting sources, missing evidence and multilingual evidence.

---

## Prompt 6 - Revision Intelligence and content-impact graph
Build a visual revision experience, not a raw diff.

Old source vs new source:
- unchanged;
- moved;
- rewritten;
- inserted;
- deleted;
- technical value changed;
- structure split/merged.

Show impact on:
- reused components;
- publications;
- variants;
- languages;
- open jobs;
- assets;
- release readiness.

Add an explainable impact graph and "what must I do next?" summary.

Acceptance: for a known revision fixture, every labeled change is classified with measurable precision and no technical change is hidden by textual similarity.

---

## Prompt 7 - Historical Alignment Review
Build professional old-source vs old-translation alignment.

Support:
- auto alignment with confidence;
- side-by-side view;
- search/filter by confidence;
- accept/reject/remap;
- 1:N and N:1 merge/split;
- heading/page/context cues;
- technical signature comparison;
- audit;
- rebuild project TM from approved pairs.

Important: alignment confidence is NOT a fuzzy translation score.
Measure both coverage and precision.

Acceptance: labeled industrial corpora report precision by confidence band and false automatic pairings.

---

## Prompt 8 - Translation Intelligence 3.0
Make this subsystem strong enough to be a genuine differentiator.

Implement and benchmark:
1. protected/non-translatable classification;
2. exact repetitions and propagation;
3. in-context exact;
4. 100% TM with provenance;
5. fuzzy matching with raw score, effective score and penalties;
6. context-aware ranking;
7. technical signatures for numbers, codes, units and parameters;
8. terminology;
9. stale-source detection;
10. 1:N/N:1 reuse;
11. safe subsegment reuse where evidence supports it;
12. source-target language plausibility QA;
13. duplicate/repetition leverage;
14. explainability for every reuse decision.

No fuzzy score may override a technical mismatch.
Do not choose score thresholds from intuition. Build a labeled benchmark and report precision/recall by band.

Business metrics:
- words protected/non-translatable;
- words avoided by exact/repetition;
- words auto-reused;
- words requiring linguistic review;
- words requiring technical review;
- new translation;
- false AUTO_REUSE rate;
- cost/time avoided.

---

## Prompt 9 - Translation execution: complete but customer-operated
ManualSync must be complete without requiring the vendor to translate customer content.

Design a provider-neutral execution layer:
- XLIFF/TMX;
- CAT/TMS connectors;
- agency package exchange;
- customer-owned AI/MT credentials;
- internal translator workflow.

Include:
- job status;
- provider;
- due date;
- terminology;
- cost estimate;
- provenance;
- returned-file validation;
- QA and human approval.

The user must be able to finish the workflow without leaving ManualSync except where the chosen external provider requires it.

---

## Prompt 10 - Publishing and Layout Engine
The workflow must end in a real deliverable, not an XLIFF.

Priorities:
1. DOCX/DOTX template mapping;
2. IDML/InDesign template/source mapping;
3. PDF output;
4. HTML later.

Capabilities:
- semantic styles;
- reflow;
- overset/overflow detection;
- native tables;
- figures and anchored assets;
- captions;
- numbering;
- cross-references;
- TOC/index;
- headers/footers;
- revision tables;
- localized assets;
- preflight;
- fail-closed production on unresolved layout issues.

Do not promise arbitrary pixel-perfect reconstruction from flattened PDF. If only PDF exists, show confidence/review requirements.

Acceptance: predefined layout fixtures produce zero unresolved overset and stable visual regression results.

---

## Prompt 11 - Datasheet and product-data engine
Create a canonical model for typed technical specifications:
- value;
- unit;
- range/tolerance;
- source/provenance;
- product/model applicability;
- revision/effective date;
- approval state.

Bind specifications into text, tables, variants and datasheet templates.
A product-data change must produce deterministic document impact.

Later adapters: CSV, ERP, PIM, PLM, API.

---

## Prompt 12 - Existing-system interoperability
Make "we already have a system" a weaker objection.

Design import/export/API integration for:
- existing CCMS;
- TMS/CAT;
- PIM/PLM/ERP;
- DAM;
- InDesign/IDML;
- file shares.

ManualSync should support:
A. intelligence-layer coexistence;
B. progressive migration;
C. eventual full replacement where justified.

Document data ownership and round-trip guarantees.

---

## Prompt 13 - Performance and bulk operations
Benchmark small/medium/large industrial projects.

Measure:
- startup;
- import/extraction;
- alignment;
- fuzzy matching;
- database operations;
- bulk revision analysis;
- XLIFF;
- publishing;
- memory;
- package size.

Add bulk operations where users currently repeat the same action.
Do not optimize without before/after numbers.

---

## Prompt 14 - Enterprise readiness
Only after core product maturity:
- multi-user;
- roles/permissions;
- SSO;
- audit centralization;
- cloud/on-prem options;
- APIs/webhooks;
- project locking/concurrency;
- backup/restore at scale;
- tenant separation where applicable.

Do not let enterprise architecture slow down the core desktop product prematurely.

---

## Prompt 15 - Competitive benchmark harness
Create a repeatable benchmark suite for claims.

Benchmark categories:
- novice time-to-value;
- task completion without training;
- revision detection precision;
- alignment precision/coverage;
- exact/fuzzy technical-safe reuse;
- false AUTO_REUSE;
- non-translatable precision;
- XLIFF round-trip safety;
- publishing/preflight;
- end-to-end elapsed time.

The benchmark must distinguish:
- synthetic fixtures;
- industrial fixtures;
- customer pilots.

No marketing claim may exceed the evidence.

---

## Prompt 16 - Final Commercial Candidate gate
Create one exact-artifact acceptance protocol.

Must test:
- clean Windows install/update/rollback;
- Desktop launch/reopen;
- project creation/persistence;
- legacy onboarding;
- authoring;
- revision analysis;
- alignment review;
- reuse/matching;
- customer-operated translation workflow;
- corrupted/stale/wrong-language XLIFF rejection;
- technical mismatch blocking;
- publishing/layout preflight;
- internal release;
- Produzione cliente fail-closed;
- backup/restore;
- exact delivered ZIP re-extracted and tested.

Do not call it commercial-ready until the exact customer artifact passes every gate.


---

## Prompt 17 - Objection-driven product hardening
Treat the following commercial objections as product requirements, not copywriting exercises:

1. "Why should we buy it? We already have Paligo / another CCMS."
2. "We already have technical-writing and translation suppliers and are happy with them."
3. "We are a small company with few manuals."
4. "Our technical department does everything internally."
5. "We do not have time to train staff on another program."
6. "We work in InDesign."
7. "We work in Word."
8. "We only have PDFs."
9. "We do not want to migrate our documentation."
10. "We do not want to change translation agency, CAT or TMS."
11. "Our manuals rarely change."
12. "We already have PIM/PLM/ERP/DMS."
13. "We do not trust AI with technical content."
14. "Our data cannot leave the company."
15. "The ROI is unclear / it costs too much."
16. "We translate little or only into one language."

For each objection:
- determine when the objection is objectively valid;
- define the correct product response;
- define the capability needed to make that response true;
- mark AVAILABLE / PARTIAL / ROADMAP;
- create acceptance criteria and a measurable before/after KPI;
- explicitly define when ManualSync should NOT be sold.

Then create:
A. OBJECTION_TO_PRODUCT_REQUIREMENTS.md
B. PILOT_QUALIFICATION_SCORECARD.md
C. ZERO_TRAINING_USABILITY_TEST.md
D. COEXISTENCE_INTEGRATION_PLAN.md

Do not write marketing slogans before the product evidence exists.
