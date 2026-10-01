# Madison & Main — Solicitation Register: Expansion Roadmap (v2)

Agreed to build in parts: (1) suggestions noted → (2) add APIs for new bots → (3) build.
This file is the noted-down plan.

## 1. Scope expansion — ALL sectors, not just COTS
Old framework made Construction / SF-1442 / on-site services an INSTANT fatal NO-BID.
New rule: nothing is auto-dumped. Every solicitation — COTS, Services, Construction —
runs the same gate:
  "Am I eligible? → NO = NO-BID (cite the blocker). YES = how do I fulfill it?"
- COTS  → fulfill by source-and-ship (current model).
- Services / Construction → fulfill via a local partner / subcontractor / JV (M&M as prime/broker).
  Harder bar (bonding, licensing, past-perf, on-ground) → most land in BID-NO-BID (conditional
  on securing a partner) or NO-BID (truly unfulfillable). Assessed, never skipped.
- TRUE fatal triggers kept: ITAR/weapons/dual-use, set-aside we can't meet, can't source,
  no margin, illegal. Construction/services are NO LONGER fatal by themselves.
- Each record tagged with sector: COTS | SERVICES | CONSTRUCTION.

## 2. Updated BID / NO-BID engine (strict + flexible)
Encode the uploaded 4-stage framework into the adjudicator, extended per sector:
  Stage 1 Fatal Triggers (hard NO-BID) — the TRUE blockers only (above).
  Stage 2 Core Fit — sector-specific checklist (COTS = current 7; Services/Construction = partner,
          license, bonding, past-perf, local presence).
  Stage 3 Gap Resolution — workarounds (reseller letter, teaming, local partner, handyman,
          offshore Class Deviation for SAM) → claw back to BID if executable before deadline.
  Stage 4 Margin & Sanity — landed cost, margin ≥ floor (~15-20%), float, timeline, docs.
  Standing rule: when unsure + no fatal + margin exists → BID.
"Strict" = fatal triggers + margin floor. "Flexible" = Stage 3 workaround matrix.

## 3. AI bot pool — 20-30+ free models, pooled & rotated
Each (provider × model) = one "bot". Round-robin across ALL for max aggregate free quota.
Have: Gemini, Groq, OpenRouter, Mistral(429/needs-activation).
Add (free):
  - OpenRouter: auto-expand to ALL its free models (one key → ~15-20 bots). No new signup.
  - GitHub Models: free in Actions (PAT, models:read) → GPT-4o-mini, Llama, Phi, Mistral…
  - Cerebras: free, very fast Llama 3.3 70B.
  - SambaNova: free, fast.
  - Cloudflare Workers AI: free tier (acct id + token), many models.
Dynamic model discovery already live (self-heals against model churn).

## 4. Coordination — bots don't redo each other's work
- Shared DONE-LEDGER: fingerprint (url + content hash) of every adjudicated solicitation,
  carried in data.json. Any bot/worker skips anything already in the ledger.
- Parallelism: GitHub Actions matrix — split 166 embassies + SAM + UN into N shards;
  up to ~20 concurrent jobs, each owns its shard (no overlap by design), writes its own
  shard file; a merge job dedupes by fingerprint into data.json. Avoids commit conflicts.

## 5. Platforms + navigation
- Top tabs: [ US GOV ]  [ UN ].
- US GOV sub-buttons: [ Overseas (SAM + SAM/Site, outside US) ]  [ Domestic (inside US) ].
- UN sub-sections per agency: UNGM, IOM, ILO, UNDP, … — shown ONLY if ≥1 active solicitation
  (no empty boxes, no clutter).
- Within each tier (BID / BID-NO-BID / NO-BID): sub-group by sector, order COTS → Services → Construction.

## 6. UN integration (UNGM / IOM / ILO / UNDP)
- Phase 1 (safe, no lockout): scrape PUBLIC tender notices. UNGM public search covers many
  agencies (UNDP, IOM, ILO, UNICEF, WFP…). No login needed.
- Phase 2 (optional, deeper): authenticated access to full docs / express-interest via the
  hold-the-door flow — user logs in with built-in browser present; light keep-alive ping to
  avoid timeout. Automated cloud login avoided (2FA / lockout / ToS risk).
- Lockout / human-touch: bot raises HELP (red banner) naming the platform + what it needs;
  user unlocks or does the human step; bot resumes.

## 7. UI standards (locked)
Black & white, Arial, only red/orange/green for status. Boxes, tables, aligned columns
(horizontal + vertical), bold + italic where useful. No clutter, no empty department boxes.
FIXED: Est Value no longer overlaps Status.

## Build order (phase 3, once APIs are in)
1. Rewrite adjudicator for all sectors + 4-stage framework + sector tag.
2. Expand AI pool (OpenRouter-all-free + GitHub Models + Cerebras + SambaNova + Cloudflare).
3. Shared ledger + matrix sharding (parallel bots, no overlap).
4. Add UN public sources (UNGM/IOM/UNDP/ILO) + platform/agency tags.
5. Add SAM domestic vs overseas split.
6. UI: platform tabs, agency sub-sections (non-empty only), sector sub-grouping.
