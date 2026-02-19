# Pharaoh PR Guard — PRD-Lites & Claude Code Session Prompts

> **Architecture:** [PLAN-eliminate-unwired-code-v4.md](PLAN-eliminate-unwired-code-v4.md)
>
> **Repo:** `0xUXDesign/pharaoh`
> **Stack:** TypeScript, Neo4j, MCP SDK, tree-sitter
> **Graph stats (reference):** 392 files, 2,074 functions, 17 modules, 150K LOC, 760 IMPORTS, 456 CALLS, 23 endpoints, 5 crons
>
> **How to use:** Each session is self-contained. Copy the CC prompt into Claude Code, run it, ship the PR. `/clear` between sessions.

---

## Defense Stack Overview

PR Guard doesn't rely on a single detection layer. Five independent layers, each using a different mechanism:

| Layer | When | Mechanism | Bypassable? |
|-------|------|-----------|-------------|
| **Wiring contract** | PRD-Lite design time | Human declares call sites before code is written | No (human-authored) |
| **Stop hook** | End of CC session | `check_reachability` MCP call blocks session completion | No (deterministic hook) |
| **Knip** | Pre-commit + CI | Static unused export detection | No (CI gate) |
| **PR Guard** | PR review | Graph reachability + reference-count two-layer check | No (GitHub required check) |
| **Post-merge sweep** | After merge | Full reachability analysis on default branch | Reactive (safety net) |

---

## Execution Order & Dependencies

```
Phase 1 (parallel: 1A + 1B + 1C) ─── foundation ──── ~6 hrs
     │
Phase 2 (2A + 2B + 2C) ─── tools + validation gate ── ~8 hrs
     │
     ├── Phase 5A (post-merge sweep) ─── can start here
     │
Phase 3 (3A → 3B → 3C → 3D) ─── allowlist then PR Guard ── ~11 hrs
     │
Phase 4 (pricing) ─── can start anytime, no code dependency ── ~4 hrs
     │
Phase 5B-5C (stop hook + Claude Code integration) ── ~2 hrs
```

**Critical path: Phase 1 → Phase 2 → Phase 3 = ~25 hrs active work.**

---

# Phase 1A: File Classification (`is_test` on File Nodes)

## Goal

Add `is_test: boolean` to File nodes so queries can distinguish test files from production files.

## Context

The Cartographer walks files via `src/parser/file-walker.ts` (`walkFiles()` → `WalkedFile[]`), parses them into `ParsedFile` objects (via `src/parser/tree-sitter.ts`), and writes File nodes via `src/graph/graph-writer.ts` (`writeGraph(client, opts: WriteOptions)`).

File nodes currently have: `uid, path, name, language, loc, last_modified, change_frequency, active`. No test classification.

The `writeGraph` function in `graph-writer.ts` builds `fileData` from `parsedFiles` around line 80, then writes with:
```cypher
MERGE (f:File {uid: file.uid})
SET f.path = file.path, f.name = file.name, ... f.active = true
```

## File Scope

**ALLOWED:**
```
src/parser/file-classifier.ts       # NEW — isTestFile() function
src/parser/types.ts                 # ADD is_test to ParsedFile
src/parser/tree-sitter.ts           # SET is_test during parsing
src/parser/python-tree-sitter.ts    # SET is_test during parsing
src/graph/graph-writer.ts           # ADD is_test to File node SET clause
tests/parser/file-classifier.test.ts  # NEW
```

**FORBIDDEN:** `.claude/settings.json`, `lefthook.yml`, `.github/workflows/ci.yml`, `biome.json`, `src/mcp/`, `src/github/`

## Steps

### 1. Create `src/parser/file-classifier.ts`

Export `isTestFile(relativePath: string): boolean`. Classification rules:

- `/\.(test|spec)\.(ts|tsx|js|jsx)$/` → true
- `__tests__/` or `__mocks__/` in path → true
- `/\/tests?\//` or starts with `test/` or `tests/` → true
- `/fixtures/` in path → true
- `/\.stories\.(ts|tsx)$/` → true
- Everything else → false

### 2. Add `is_test: boolean` to `ParsedFile` in `src/parser/types.ts`

### 3. Set `is_test` during parsing

In `src/parser/tree-sitter.ts` and `src/parser/python-tree-sitter.ts`, import `isTestFile` and set `is_test: isTestFile(relativePath)` on the `ParsedFile` return object.

### 4. Write `is_test` to Neo4j

In `src/graph/graph-writer.ts`, add `is_test: f.is_test ?? false` to the `fileData` mapping (~line 80). Add `f.is_test = file.is_test` to the File node SET clause (~line 96).

### 5. Write tests for `isTestFile()`

Cover: `.test.ts`, `.spec.ts`, `__tests__/`, `__mocks__/`, `tests/`, `test/`, `fixtures/`, `.stories.tsx` → true. `src/index.ts`, `src/config.ts`, `src/mcp/server.ts` → false.

## Acceptance Criteria

- [ ] `isTestFile()` passes all test cases
- [ ] `ParsedFile` interface has `is_test: boolean`
- [ ] Both parsers set `is_test` on output
- [ ] File node SET clause includes `is_test`
- [ ] `pnpm run lint && pnpm run build` pass
- [ ] Existing tests pass

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 1A: File Classification" and implement it exactly.

Summary: Add is_test: boolean to File nodes. Create src/parser/file-classifier.ts with isTestFile(relativePath). Add is_test to ParsedFile interface. Set it in tree-sitter.ts and python-tree-sitter.ts. Write it in graph-writer.ts File node SET clause. Write tests.

File scope is strict — only touch files listed in ALLOWED. Do not touch src/mcp/, src/github/, or config files.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 1B: Reachability Cypher Queries

## Goal

Add three reachability query functions to `src/mcp/neo4j-queries.ts` that trace from production entry points (endpoints + crons) through CALLS edges, excluding test files.

## Context

`src/mcp/neo4j-queries.ts` (~18K, ~500 lines) exports functions returning `{ cypher: string, params: Record<string, unknown> }`. Other tools import these.

Entry points in the graph:
- **Endpoint handlers:** `Endpoint.handler_function` stores the Function UID. File-[:EXPOSES]->Endpoint edges.
- **Cron handlers:** Function-[:HANDLES]->CronJob edges.

`get_blast_radius` already traces callers outward. Reachability is the reverse — entry points inward.

## File Scope

**ALLOWED:**
```
src/mcp/neo4j-queries.ts     # ADD three query functions
```

**FORBIDDEN:** Everything else.

## Steps

### 1. Add `entryPointsQuery(repo: string)`

Returns all entry-point functions (endpoint handlers + cron handlers) with type classification.

### 2. Add `checkReachabilityQuery(repo: string, functionUids: string[])`

Given specific function UIDs, determines reachability status for each:
- `entry_point` — is itself an entry point
- `reachable` — reachable from entry points through production CALLS edges (is_test: false)
- `test_only` — lives in a test file
- `unreachable` — not reachable from any production entry point

Uses `[:CALLS*0..10]` variable-length path bounded at 10 hops. Filters all nodes in path to production files only (`is_test: false`).

### 3. Add `allUnreachableQuery(repo: string, module?: string)`

Finds ALL exported functions in production files that are NOT reachable from entry points. Returns function name, complexity, file path, module, and what references each (distinguishing test vs production callers). Optional module filter.

## Acceptance Criteria

- [ ] Three new exported functions in neo4j-queries.ts
- [ ] Queries filter on `is_test: false` for production file traversal
- [ ] Path depth bounded at 10 hops
- [ ] `pnpm run lint && pnpm run build` pass

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 1B: Reachability Cypher Queries" and implement it exactly.

Summary: Add three query functions to src/mcp/neo4j-queries.ts — entryPointsQuery, checkReachabilityQuery, allUnreachableQuery. These trace from entry points (endpoints + crons) through CALLS edges in production files (is_test: false). Follow the existing pattern of returning { cypher, params } objects.

Only modify src/mcp/neo4j-queries.ts. Touch nothing else.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 1C: Event Handler Detector

## Goal

Create a new detector that finds `.on('event', handler)`, `.addListener()`, `.use()`, and `.emit()` patterns, producing EventBinding nodes and HANDLES_EVENT/EMITS_EVENT edges. This closes the false-positive gap where event-driven code appears unreachable.

## Context

Existing detectors follow a pattern in `src/detectors/`:
- `endpoint-detector.ts` — finds route handlers, creates Endpoint nodes + EXPOSES edges
- `cron-detector.ts` — finds scheduled jobs, creates CronJob nodes + HANDLES edges

Both export a function that takes parsed AST data and returns `DetectedNode[]` + `DetectedEdge[]` (types from `src/detectors/types.ts`).

The Cartographer registers detectors in `src/cli/run-cartographer.ts` and the graph writer processes their output.

## File Scope

**ALLOWED:**
```
src/detectors/event-handler-detector.ts   # NEW
src/detectors/types.ts                    # ADD EventBinding node type + edge types if needed
src/cli/run-cartographer.ts               # REGISTER new detector
src/graph/graph-writer.ts                 # WRITE EventBinding nodes + edges (follow existing detector output pattern)
tests/detectors/event-handler-detector.test.ts  # NEW
```

**FORBIDDEN:** `.claude/settings.json`, `lefthook.yml`, `.github/workflows/ci.yml`, `biome.json`, `src/mcp/`

## Steps

### 1. Extend types in `src/detectors/types.ts`

Add `EventBinding` to the `DetectedNode` type (or add it as a new node kind). Properties: `event_name`, `pattern` (e.g., `"emitter.on"`, `"app.use"`).

Add edge types: `HANDLES_EVENT` (Function → EventBinding), `EMITS_EVENT` (Function → EventBinding).

### 2. Create `src/detectors/event-handler-detector.ts`

Detect patterns using tree-sitter AST:

**EventEmitter patterns:**
- `*.on('eventName', handlerFn)` → creates EventBinding + HANDLES_EVENT edge
- `*.once('eventName', handlerFn)` → same
- `*.addListener('eventName', handlerFn)` → same
- `*.emit('eventName')` → creates EMITS_EVENT edge

**Middleware patterns:**
- `app.use(middleware)` → creates EventBinding with pattern "middleware" + HANDLES_EVENT
- `router.use('/path', middleware)` → same with path in event_name

Export function matching the detector pattern used by existing detectors.

### 3. Register in `src/cli/run-cartographer.ts`

Import and register the detector alongside existing endpoint/cron detectors.

### 4. Write EventBinding nodes to Neo4j

In `src/graph/graph-writer.ts`, add handling for EventBinding nodes following the pattern used for Endpoint and CronJob nodes.

### 5. Write tests

Test against representative patterns: `emitter.on('data', process)`, `app.use(cors())`, `eventBus.on('user.created', sendEmail)`, `socket.emit('message', data)`.

## Acceptance Criteria

- [ ] EventBinding nodes created for detected event patterns
- [ ] HANDLES_EVENT edges connect handler functions to EventBinding nodes
- [ ] EMITS_EVENT edges connect emitter functions to EventBinding nodes
- [ ] Detector registered in Cartographer pipeline
- [ ] Graph writer handles EventBinding output
- [ ] Tests pass for EventEmitter + middleware patterns
- [ ] `pnpm run lint && pnpm run build` pass

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 1C: Event Handler Detector" and implement it exactly.

Summary: Create src/detectors/event-handler-detector.ts following the existing detector pattern (endpoint-detector, cron-detector). Detect .on(), .once(), .addListener(), .emit(), app.use() patterns. Create EventBinding nodes with HANDLES_EVENT and EMITS_EVENT edges. Register in run-cartographer.ts. Add graph writing in graph-writer.ts. Write tests.

File scope is strict — only touch files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 2A: `check_reachability` MCP Tool

## Goal

Expose reachability queries (from Phase 1B) as a new MCP tool that AI agents can call to verify their code is wired into production.

## Context

Every MCP tool lives in `src/mcp/tools/{name}.ts` and exports `registerXxx(server: McpServer, client: Neo4jClient, defaultRepo: string)`. Registration happens in `src/mcp/server.ts` at lines 199-208:

```typescript
registerGetCodebaseMap(server, client, repo);
registerGetModuleContext(server, client, repo);
// ... etc
registerGetTestCoverage(server, client, repo);
```

Helpers: `resolveRepo()`, `textResponse()`, `toNum()` from `src/mcp/types.ts`.

## File Scope

**ALLOWED:**
```
src/mcp/tools/check-reachability.ts   # NEW
src/mcp/server.ts                     # ADD import + registration
tests/mcp/check-reachability.test.ts  # NEW
```

**FORBIDDEN:** Everything else. Do not modify neo4j-queries.ts (done in 1B).

## Steps

### 1. Create `src/mcp/tools/check-reachability.ts`

Tool name: `check_reachability`

Parameters:
- `repo` — optional, defaults to defaultRepo
- `functions` — optional string array of function names to check (checks all exported if omitted)
- `module` — optional module filter
- `show_entry_points` — optional boolean to list detected entry points
- `include_paths` — optional boolean to include reachability paths

**Behavior:**
- If `functions` provided: resolve names to UIDs, run `checkReachabilityQuery`, report per-function status (✅ reachable, ⚠️ test_only, ❌ unreachable)
- If no `functions`: run `allUnreachableQuery`, report all unreachable exported functions grouped by module
- If `show_entry_points`: run `entryPointsQuery` and prepend entry point list

### 2. Register in `src/mcp/server.ts`

Import `registerCheckReachability` and call it after `registerGetTestCoverage`.

### 3. Write tests

## Acceptance Criteria

- [ ] Tool appears when MCP server starts
- [ ] Calling with no args returns all unreachable exported functions
- [ ] Calling with function names returns per-function status
- [ ] Module filter scopes correctly
- [ ] Entry points listed when requested
- [ ] `pnpm run lint && pnpm run build` pass

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 2A: check_reachability MCP Tool" and implement it exactly.

Summary: Create src/mcp/tools/check-reachability.ts following the exact pattern of existing tools (registerXxx, server.tool with zod schema). Import reachability queries from neo4j-queries.ts. Register in server.ts after registerGetTestCoverage. Supports checking specific functions or finding all unreachable exports.

File scope is strict — only touch files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 2B: Enhance `get_unused_code` with Reachability

## Goal

Upgrade `get_unused_code` from "zero callers" to "not reachable from production entry points." This catches functions called only by tests or re-exported but never consumed.

## File Scope

**ALLOWED:**
```
src/mcp/tools/get-unused-code.ts   # REWRITE query logic
```

**FORBIDDEN:** Everything else.

## Steps

### 1. Rewrite with two passes

**Pass 1 — Unexported, zero callers (same as before + is_test filter):**
```cypher
WHERE NOT (:Function)-[:CALLS]->(fn) AND f.is_test = false
```

**Pass 2 — Exported, unreachable from entry points:**
Import and use `allUnreachableQuery` from neo4j-queries.ts.

### 2. Update tool description

From "functions with no callers" to "functions unreachable from any production entry point." Update the description to explain what it catches: test-only callers, barrel re-exports, dead call chains.

### 3. Rename `include_exported` to `exported_only`

Default false (show both unexported zero-callers and unreachable exports).

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 2B: Enhance get_unused_code" and implement it exactly.

Summary: Rewrite src/mcp/tools/get-unused-code.ts to use reachability instead of raw caller count. Two passes: unexported with zero callers (add is_test: false filter), exported unreachable from entry points (use allUnreachableQuery). Update tool description. Rename include_exported to exported_only.

Only modify src/mcp/tools/get-unused-code.ts.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 2C: Validation Gate

## Goal

Run `check_reachability` against the Pharaoh repo itself, cross-reference with Knip, and verify accuracy before building the PR gate. **This is a manual phase — do not proceed to Phase 3 until validation passes.**

## Process

### 1. Re-map the repo

```bash
pharaoh refresh pharaoh
```

This populates `is_test` on File nodes and EventBinding nodes.

### 2. Run `check_reachability` via MCP

Call with no args to get all unreachable exported functions. Review top 50.

### 3. Cross-reference with Knip

```bash
pnpm run knip
```

Compare results:

| Knip | Pharaoh | Verdict |
|------|---------|---------|
| Unused | Unreachable | **High confidence dead code** — delete it |
| Unused | Reachable | Investigate — Pharaoh may have false CALLS edge |
| Used | Unreachable | Investigate — Pharaoh may have missing CALLS edge |
| Used | Reachable | ✅ No action |

### 4. Measure accuracy

- Count false positives (reachable code flagged as unreachable)
- Count false negatives (unreachable code flagged as reachable)
- Target: **<5% false positive rate**

### 5. Fix systematic issues

If CALLS edges are systematically missing for certain patterns (callbacks, HOFs), document them and decide whether to improve the Cartographer or rely on the reference-count backup layer (Phase 3C).

**Gate: Do not proceed to Phase 3 until false positive rate is acceptable.**

---

# Phase 3A: Allowlist (`.pharaoh.yml` Parser)

## Goal

Parse `.pharaoh.yml` from customer repos to support allowed orphans, custom entry points, and test-file overrides. Ships BEFORE the PR gate goes live — without this, the first false positive kills adoption.

## File Scope

**ALLOWED:**
```
src/config/pharaoh-yml.ts           # NEW
tests/config/pharaoh-yml.test.ts    # NEW
```

**FORBIDDEN:** Everything else.

## Steps

### 1. Create `src/config/pharaoh-yml.ts`

Parse `.pharaoh.yml` format:

```yaml
pr_guard:
  allowed_orphans:
    - "src/utils/debug-helpers.ts:*"        # All exports in file
    - "src/tools/widget.ts:debugWidgetState" # Specific function
    - "src/scripts/*"                        # Entire directory

  entry_points:
    - "src/cli/*.ts:main"                    # CLI entry points
    - "src/workers/*.ts:handler"             # Worker entry points

  not_test:
    - "src/test-utils/factories.ts"          # Override: treat as production
```

Export three functions:
- `isAllowedOrphan(functionName: string, filePath: string, config: PharaohConfig): boolean`
- `getCustomEntryPoints(config: PharaohConfig): { pattern: string; functionName: string }[]`
- `isNotTest(filePath: string, config: PharaohConfig): boolean`

Support:
- Exact `file:function` match
- `file:*` wildcard (all exports in file)
- Glob patterns with `*` and `**`

### 2. Write comprehensive tests

Test every pattern type. Test missing file (returns empty config). Test malformed YAML (returns empty config, doesn't throw).

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 3A: Allowlist" and implement it exactly.

Summary: Create src/config/pharaoh-yml.ts that parses .pharaoh.yml config with pr_guard.allowed_orphans, pr_guard.entry_points, and pr_guard.not_test sections. Export isAllowedOrphan, getCustomEntryPoints, isNotTest functions. Support exact match, file:* wildcard, and glob patterns. Write comprehensive tests.

Only create the two files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 3B: `pull_request` Webhook Handler

## Goal

Add `pull_request` webhook handling with debouncing. On opened/synchronize, enqueue a PR analysis job. On close, do nothing (in-memory analysis leaves no state to clean up).

## Context

`src/github/webhooks.ts` handles events in a switch inside `handleWebhookEvent(eventType, payload)`. Current cases: `"installation"`, `"installation_repositories"`, `"push"`.

`src/github/types.ts` defines event types. No `PullRequestEvent` exists.

The `handlePush` function resolves tenant by installation ID, checks ref, and calls `enqueueJob()`.

## File Scope

**ALLOWED:**
```
src/github/types.ts            # ADD PullRequestEvent
src/github/webhooks.ts         # ADD pull_request case + handler + debouncing
src/queue/refresh-queue.ts     # ADD check-pr job type if typed
```

**FORBIDDEN:** Everything else. No graph changes, no MCP changes, no parser changes.

## Steps

### 1. Add `PullRequestEvent` to `src/github/types.ts`

```typescript
export interface PullRequestEvent {
  action: "opened" | "synchronize" | "closed" | "reopened";
  number: number;
  pull_request: {
    number: number;
    head: { ref: string; sha: string; };
    base: { ref: string; };
    merged: boolean;
  };
  repository: GitHubRepository;
  installation?: { id: number };
}
```

### 2. Add `pull_request` case to webhook switch

```typescript
case "pull_request": {
  const event = payload as unknown as PullRequestEvent;
  if (event.action === "opened" || event.action === "synchronize" || event.action === "reopened") {
    await handlePullRequestUpdate(event);
  }
  // closed: no cleanup needed — in-memory analysis leaves no state
  return true;
}
```

### 3. Implement `handlePullRequestUpdate` with debouncing

- Store `{ prNumber, headSha, timestamp }` in a pending map
- After 30s delay, check if stored SHA still matches (no newer push)
- If match, enqueue `check-pr` job with `tenantId`, `repoSlug`, `installationId`, `prNumber`, `headRef`, `headSha`
- If newer SHA exists, skip

### 4. Note for GitHub App config

Add comment: The GitHub App must subscribe to "Pull requests" events in its settings.

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 3B: pull_request Webhook Handler" and implement it exactly.

Summary: Add PullRequestEvent type to src/github/types.ts. Add pull_request case to the webhook switch in src/github/webhooks.ts. Implement handlePullRequestUpdate with 30-second debouncing for synchronize events. Enqueue check-pr job. No cleanup on close (in-memory analysis leaves no state).

File scope is strict — only touch files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 3C: In-Memory PR Analysis Pipeline

## Goal

The core analysis engine. Clone PR branch, parse in memory (no Neo4j writes), diff new exports against main branch graph, compute reachability, and run reference-count backup. Returns structured results for the GitHub Check.

## Architecture (from v4 plan)

```
Clone PR head → Parse in memory → Diff against main graph → Reachability check → Reference-count backup → Return results
```

**No graph nodes created. Nothing persisted. All intermediate data discarded.**

## File Scope

**ALLOWED:**
```
src/analysis/pr-guard.ts             # NEW — main pipeline orchestrator
src/analysis/reference-checker.ts    # NEW — backup layer (text-based reference count)
src/mcp/neo4j-queries.ts            # ADD diffNewExportsQuery
```

**FORBIDDEN:** `src/graph/` (no graph writes), `.claude/settings.json`, `lefthook.yml`, `biome.json`

## Steps

### 1. Add `diffNewExportsQuery` to `neo4j-queries.ts`

Finds exported functions in the main branch graph for a given repo. Used to compare against the PR's in-memory parse to identify new/newly-exported functions.

### 2. Create `src/analysis/reference-checker.ts`

The backup layer (~20 lines of actual logic):

```typescript
export function hasAnyProductionReference(
  functionName: string,
  definingFile: string,
  allFiles: ParsedFile[],
): boolean {
  if (functionName.length < 4) return false; // too generic
  for (const file of allFiles) {
    if (file.path === definingFile) continue;
    if (isTestFile(file.path)) continue;
    const re = new RegExp(`\\b${escapeRegex(functionName)}\\b`);
    if (re.test(file.source)) return true;
  }
  return false;
}
```

### 3. Create `src/analysis/pr-guard.ts`

Main orchestrator:

```typescript
export interface PrGuardResult {
  reachable: { name: string; file: string; entryPath?: string }[];
  likelyReachable: { name: string; file: string; note: string }[];
  unreachable: { name: string; file: string; referencedBy: string[] }[];
  entryPointCount: number;
  newExportCount: number;
}

export async function analyzePr(opts: {
  tenantId: string;
  repoSlug: string;
  installationId: number;
  prNumber: number;
  headRef: string;
  headSha: string;
  neo4jClient: Neo4jClient;
}): Promise<PrGuardResult>
```

Pipeline steps:
1. **Clone** PR head branch using installation token (shallow clone, `--depth 100` for git metadata)
2. **Parse** using existing Cartographer functions (`walkFiles`, tree-sitter parser) — produces `ParsedFile[]` in memory
3. **Identify new exports** — compare PR's exported functions against main branch via `diffNewExportsQuery`
4. **Load .pharaoh.yml** from cloned repo — apply allowlist, custom entry points
5. **Check reachability** — query main branch graph for entry points, then check if new exports connect to them via CALLS edges (combining in-memory call chains with graph lookup)
6. **Reference-count backup** — for any export flagged unreachable by graph, run `hasAnyProductionReference` against in-memory parsed files
7. **Classify** into three tiers:
   - Graph says reachable → **reachable** (green)
   - Graph says unreachable + has production reference → **likelyReachable** (yellow)
   - Graph says unreachable + no production reference → **unreachable** (red)
8. **Cleanup** — delete cloned repo, discard all intermediate data

### 4. Handle the `check-pr` job

In whatever processes the job queue, add handling for the `check-pr` job type that calls `analyzePr()` and then posts the GitHub Check (Phase 3D).

## Acceptance Criteria

- [ ] `analyzePr()` clones, parses, diffs, checks reachability, and returns structured results
- [ ] Reference-count backup correctly identifies production references
- [ ] Three-tier classification works (reachable / likely reachable / unreachable)
- [ ] `.pharaoh.yml` allowlist is applied before classification
- [ ] No Neo4j writes during analysis
- [ ] Clone directory cleaned up after analysis
- [ ] `pnpm run lint && pnpm run build` pass

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 3C: In-Memory PR Analysis Pipeline" and implement it exactly.

Summary: Create the core PR Guard engine. src/analysis/pr-guard.ts orchestrates: clone PR branch → parse in memory (use existing walkFiles + tree-sitter) → diff new exports against main graph → check reachability → run reference-count backup → classify into three tiers (reachable / likely reachable / unreachable) → cleanup. src/analysis/reference-checker.ts provides the backup layer (word-boundary text search for function names in production files). Add diffNewExportsQuery to neo4j-queries.ts.

CRITICAL: No Neo4j writes. Everything is in-memory. Clone directory must be cleaned up.

File scope is strict — only touch files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 3D: GitHub Check Posting + Wiring

## Goal

Post PR Guard results as a GitHub Check Run. Wire the full pipeline: webhook → debounce → analysis → check.

## File Scope

**ALLOWED:**
```
src/github/checks.ts            # NEW — GitHub Check posting
src/queue/refresh-queue.ts      # MODIFY — wire check-pr job to analysis + check posting
```

**FORBIDDEN:** Everything else.

## Steps

### 1. Create `src/github/checks.ts`

Post GitHub Check Runs via the App API:

```
POST /repos/{owner}/{repo}/check-runs
Authorization: Bearer {installation_token}
```

Format the three-tier output as markdown:

- **Reachable** section with entry point paths (green)
- **Likely reachable** section with reference locations (yellow, advisory)
- **Unreachable** section with issue description + fix guidance (red)

Check conclusions:
- All reachable → `success`
- Some likely reachable, none unreachable → `neutral`
- Any unreachable → `failure` (blocks merge if configured as required check)

### 2. Wire in `refresh-queue.ts`

When `check-pr` job runs:
1. Check `pr_guard_enabled` flag on the tenant_repo (if column exists, else default to enabled for dogfooding)
2. Call `analyzePr()` from `src/analysis/pr-guard.ts`
3. Call `postReachabilityCheck()` from `src/github/checks.ts`
4. Log results

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 3D: GitHub Check Posting + Wiring" and implement it exactly.

Summary: Create src/github/checks.ts to post GitHub Check Runs with three-tier output (reachable/likely reachable/unreachable). Wire the check-pr job in refresh-queue.ts: check pr_guard_enabled flag → analyzePr() → postReachabilityCheck(). Format output as markdown tables.

File scope is strict — only touch files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 4: Pricing & Enablement

## Goal

Make PR Guard a purchasable per-repo add-on. First repo free with any paid plan.

## File Scope

**ALLOWED:**
```
src/db/schema.ts                   # ADD pr_guard_enabled to tenant_repos
src/db/repo-store.ts               # ADD enable/disable functions
src/stripe/client.ts               # ADD PR Guard price ID
src/web/server.ts or landing.ts    # UPDATE checkout flow
src/mcp/tools/pharaoh-account.ts   # ADD PR Guard toggle display
```

**FORBIDDEN:** `.claude/settings.json`, `lefthook.yml`, `biome.json`, `src/analysis/`, `src/github/webhooks.ts`

## Steps

### 1. Add `pr_guard_enabled BOOLEAN DEFAULT false` to `tenant_repos` table
### 2. Add enable/disable functions to repo-store.ts
### 3. Create Stripe Price: `pr_guard`, $10/month, `recurring[usage_type]=licensed`
### 4. First-repo-free logic: count enabled repos, quantity = max(0, count - 1)
### 5. Update `pharaoh_account` tool to show PR Guard status per repo
### 6. Update landing page pricing ($39 Personal / $99 Team + $10/repo PR Guard)

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 4: Pricing & Enablement" and implement it exactly.

Summary: Add pr_guard_enabled boolean to tenant_repos table. Add Stripe PR Guard price ($10/repo/month, first free). Update pharaoh_account tool to show PR Guard status. Update landing page pricing.

File scope is strict — only touch files listed in ALLOWED.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 5A: Post-Merge Sweep

## Goal

After every default branch push + Cartographer re-map, run reachability analysis on the full graph and log results.

## File Scope

**ALLOWED:**
```
src/queue/refresh-queue.ts   # ADD post-mapping hook
```

**FORBIDDEN:** Everything else.

## Steps

After `map-repo` job completes successfully:
1. Run `allUnreachableQuery` against the freshly-mapped graph
2. Load `.pharaoh.yml` from cloned repo (if available) and filter allowed orphans
3. Log findings as structured JSON to stderr
4. Expose results via the enhanced `get_unused_code` tool (already uses reachability from Phase 2B)

No auto-ticketing in v4. The data is available via MCP tools. Teams create tickets manually.

## CC Prompt

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 5A: Post-Merge Sweep" and implement it exactly.

Summary: In src/queue/refresh-queue.ts, after map-repo job completes, run allUnreachableQuery, filter against .pharaoh.yml allowed_orphans, and log findings as structured JSON to stderr. No auto-ticketing.

Only modify src/queue/refresh-queue.ts.

This repo uses pnpm. Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json.
```

---

# Phase 5B: Stop Hook + Claude Code Integration

## Goal

Add `check_reachability` to the Claude Code Stop hook so CC physically cannot finish a session with unreachable exports. Update the `/wire-check` slash command. Update CLAUDE.md.

## Context

The existing stop hook pattern (from the framework):

```json
{
  "hooks": {
    "Stop": [{
      "command": "bash scripts/hooks/stop-check.sh",
      "timeout": 60000
    }]
  }
}
```

The stop hook runs tsc, biome, knip, and the orphan check. This adds a Pharaoh reachability check.

## Scope

This applies to **customer repos** (DanBot, etc.), not the Pharaoh repo itself.

### Changes to customer repos

**1. Update `scripts/hooks/stop-check.sh`**

Add after the existing orphan check:

```bash
# Pharaoh reachability check (if Pharaoh MCP is available)
# Uses check_reachability to verify new exports are wired into production
if command -v pharaoh &> /dev/null; then
  echo "Running Pharaoh reachability check..."
  # Get files changed in this session via git diff
  CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null || true)
  if [ -n "$CHANGED_FILES" ]; then
    # Extract new exported function names from changed files
    NEW_EXPORTS=$(grep -h '^export ' $CHANGED_FILES 2>/dev/null | grep -oP '(?<=function |const |class |type |interface )\w+' || true)
    if [ -n "$NEW_EXPORTS" ]; then
      # Call check_reachability via MCP (Claude Code has access)
      echo "Checking reachability for: $NEW_EXPORTS"
      echo "PHARAOH_CHECK_EXPORTS=$NEW_EXPORTS" >&2
      # The actual MCP call happens through Claude Code's tool use
      # This message signals CC to call check_reachability before completing
    fi
  fi
fi
```

**Note:** The stop hook can't directly call MCP tools — it signals to Claude Code that a reachability check is needed. The more reliable approach is adding the instruction to CLAUDE.md and the stop hook message.

**2. Update `/wire-check` slash command**

```markdown
# /wire-check
Before opening a PR, verify all new code is wired into production.

1. Call `check_reachability` with `include_paths: true`
2. Review any unreachable or likely-reachable functions
3. For each unreachable function:
   a. Wire it into a production code path
   b. Add to `.pharaoh.yml` allowlist with justification
   c. Delete it if unnecessary
4. Cross-reference with `pnpm run knip` for double confirmation
5. Re-run until all exports are reachable or explicitly allowed
```

**3. Add to CLAUDE.md**

```markdown
## Wiring Contract

Every PRD-Lite that creates a new exported function must specify where it gets called from:

| New Export | Called From | Entry Point |
|------------|------------ |-------------|
| processWidget() | src/routes/slack.ts:handleSlackEvent | POST /api/slack |

If you can't fill in this table, the function probably shouldn't be exported.

## Post-Implementation Verification

After implementing any new exported function:
1. Call `check_reachability` with the names of new/modified exports
2. If any function is unreachable — fix before completing
3. Do NOT mark a task as complete with unreachable exports

This check takes <2 seconds. There is no reason to skip it.
```

## CC Prompt (for DanBot or customer repos)

```
Read docs/pharaoh-reachability-prd-lites.md — find "Phase 5B: Stop Hook + Claude Code Integration" and implement it.

NOTE: This session targets the CUSTOMER repo (e.g., DanBot), NOT the Pharaoh repo.

Summary: Three changes:
1. Update scripts/hooks/stop-check.sh to signal Pharaoh reachability check needed for changed files
2. Update .claude/commands/wire-check.md to use check_reachability MCP tool + Knip cross-reference
3. Update CLAUDE.md with Wiring Contract table requirement and Post-Implementation Verification instructions

Do NOT modify any source code files in src/.

Do NOT weaken any checks or modify .claude/settings.json, lefthook.yml, .github/workflows/ci.yml, or biome.json rule severity.
```

---

# Appendix: Wiring Contract Template

For inclusion in PRD-Lites. Every session that creates new exports must include this table:

```markdown
## Wiring Contract

| New Export | Called From | Entry Point |
|------------|-------------|-------------|
| `functionName()` | `src/path/to/caller.ts:callingFunction` | `METHOD /api/route` or `cron: schedule` |

If a function appears in this PRD-Lite but has no row in this table, it should not be exported.
```

This prevents unwired code at design time — before a single line is written.