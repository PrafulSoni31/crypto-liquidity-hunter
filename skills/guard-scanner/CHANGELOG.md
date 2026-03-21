# Changelog

## [2.1.0] - 2026-02-18

### 🆕 PII Exposure Detection (OWASP LLM02 / LLM06)

New `pii-exposure` threat category with 13 patterns covering four attack vectors:

#### New
- **Hardcoded PII detection** (context-aware): `PII_HARDCODED_CC`, `PII_HARDCODED_SSN`, `PII_HARDCODED_PHONE`, `PII_HARDCODED_EMAIL`
- **PII output/logging**: `PII_LOG_SENSITIVE`, `PII_SEND_NETWORK`, `PII_STORE_PLAINTEXT`
- **Shadow AI detection**: `SHADOW_AI_OPENAI`, `SHADOW_AI_ANTHROPIC`, `SHADOW_AI_GENERIC` — detects unauthorized LLM API calls
- **PII collection instructions** (doc scanning): `PII_ASK_ADDRESS`, `PII_ASK_DOB`, `PII_ASK_GOV_ID` (supports マイナンバー)
- **3 risk amplifiers**: pii+exfiltration (×3), pii+shadow-ai (×2.5), pii+credential (×2)
- **8 new tests** for PII exposure detection and risk amplification
- PII recommendation in JSON output

#### Fixed
- **VERSION constant** was stuck at `1.1.0` since initial release — now correctly reads `2.1.0`

#### Stats
- Patterns: 115 → 129
- Categories: 20 → 21
- Scanner tests: 56 → 64
- Total tests (scanner + plugin): 99

## [2.0.0] - 2026-02-18

### 🆕 Plugin Hook Runtime Guard — Actual Blocking!

The runtime guard has been rewritten as a **Plugin Hook** (`plugin.ts`) using OpenClaw's native `before_tool_call` Plugin Hook API. Unlike the legacy Internal Hook version, this can **actually block** dangerous tool calls.

#### Breaking Changes
- Runtime guard is now a Plugin Hook (`plugin.ts`) instead of Internal Hook (`handler.ts`)
- Installation method changed: copy `plugin.ts` to `~/.openclaw/plugins/`

#### New
- **`plugin.ts`**: Plugin Hook API version with `block`/`blockReason` support
- **3 enforcement modes**: `monitor` (log only), `enforce` (block CRITICAL), `strict` (block HIGH + CRITICAL)
- **Config via `openclaw.json`**: Set mode in `plugins.guard-scanner.mode`
- **35 new tests** (`plugin.test.js`): blocking, mode switching, clean passthrough, all 12 patterns

#### Deprecated
- **`handler.ts`**: Legacy Internal Hook version — warn only, cannot block. Still available for backward compatibility
- **`HOOK.md`**: Internal Hook manifest — only needed for legacy handler

#### Documentation
- README.md updated with Plugin Hook setup instructions
- Architecture diagram updated to show both plugin.ts and handler.ts
- GuavaSuite comparison table updated (runtime blocking now ✅)

## [1.1.1] - 2026-02-17

### Fixed
- **Runtime Guard hook**: Rewritten to use official OpenClaw `InternalHookEvent` / `InternalHookHandler` types (v2026.2.15)
- **Removed broken import**: Replaced `import type { HookHandler } from "../../src/hooks/hooks.js"` with inline type definitions matching the official API
- **Blocking behaviour**: `event.cancel` does not exist in `InternalHookEvent` — all detection modes now warn via `event.messages` instead of falsely claiming to block. Blocking logic preserved as comments for when cancel API is added
- **Documentation accuracy**: README.md and SKILL.md updated to reflect that Runtime Guard currently warns only (cancel API pending)
- **Version consistency**: Fixed stale v1.0.0 references in README terminal output, handler.ts JSDoc, SKILL.md stats (186+/20/55), `_meta.json`, and CHANGELOG test count (55, not 56)

---

## [1.1.0] - 2026-02-17

### 🆕 New Features — Issue #18677 Feedback

#### Skill Manifest Validation (`sandbox-validation` category)
- **Dangerous binary detection**: Flags SKILL.md `requires.bins` entries like `sudo`, `rm`, `curl`, `ssh` (23 tool blocklist)
- **Overly broad file scope**: Detects `files: ["**/*"]` and similar wildcard patterns in manifest
- **Sensitive env var requirements**: Flags SECRET, PASSWORD, PRIVATE_KEY, AWS_SECRET etc. in `requires.env`
- **Exec/network capability declaration**: Warns when skills declare unrestricted exec/network access

#### Code Complexity Metrics (`complexity` category)
- **File length check**: Flags code files exceeding 1000 lines
- **Deep nesting detection**: Detects nesting depth > 5 levels via brace tracking
- **eval/exec density**: Flags high concentration of eval/exec calls (> 2% of lines, minimum 3 calls)

#### Config Impact Analysis (`config-impact` category)
- **openclaw.json write detection**: Detects code that directly writes to OpenClaw configuration
- **Exec approval bypass**: Flags `exec.approvals = "off"` and similar patterns
- **Exec host gateway**: Detects `tools.exec.host = "gateway"` (sandbox bypass)
- **Internal hooks modification**: Flags changes to `hooks.internal.entries`
- **Network wildcard**: Detects `network.allowedDomains = "*"` patterns

### Enhanced
- **6 new patterns** in `config-impact` category for pattern-based detection
- **Risk scoring**: Added multipliers for `config-impact` (x2), `sandbox-validation` combo (min 70), `complexity` + malicious-code combo (x1.5)
- **Recommendations**: Added sandbox, complexity, and config-impact recommendations to JSON output
- **Categories**: 17 → 20 categories, 170+ → 186 patterns

### Testing
- **11 new test cases** across 3 new test sections (Manifest Validation, Complexity, Config Impact)
- **3 new test fixtures**: `dangerous-manifest/`, `complex-skill/`, `config-changer/`
- Total: 55 tests across 13 sections

---

## [1.0.0] - 2026-02-17

### 🎉 Initial Release

Extracted from GuavaGuard v9.0.0 as the open-source component.

### Features
- **17 threat categories** based on Snyk ToxicSkills taxonomy + OWASP MCP Top 10
- **170+ detection patterns** covering prompt injection, malicious code, credential leaks, exfiltration, obfuscation, memory poisoning, identity hijacking, and more
- **IoC database** with known malicious IPs, domains, URLs, usernames, and typosquat skill names
- **Multiple output formats**: Text, JSON, SARIF, HTML
- **Entropy-based secret detection** (Shannon entropy analysis)
- **Lightweight JS data flow analysis** (secret read → network/exec chain detection)
- **Cross-file analysis** (phantom refs, base64 fragment assembly, load+exec chains)
- **Dependency chain scanning** (risky packages, lifecycle scripts, pinned versions)
- **Plugin API** for custom detection rules
- **Custom rules** via JSON file
- **Ignore files** (`.guard-scanner-ignore` / `.guava-guard-ignore`)
- **Zero dependencies** — runs on Node.js 18+, nothing else

### Architecture
- `src/scanner.js` — Core scanner engine (GuardScanner class)
- `src/patterns.js` — Threat pattern database
- `src/ioc-db.js` — Indicators of Compromise
- `src/cli.js` — CLI entry point

### What's NOT included (Private — GuavaSuite)
- Soul Lock integrity verification
- SoulChain on-chain verification
- Hash-based identity file watchdog
- Polygon blockchain integration
