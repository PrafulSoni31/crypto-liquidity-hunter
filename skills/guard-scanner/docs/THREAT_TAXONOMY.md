# Threat Taxonomy

Complete reference for guard-scanner's 17 threat categories.

## Category Origins

guard-scanner's threat taxonomy combines three sources:

| Source | Categories | Description |
|--------|-----------|-------------|
| **Snyk ToxicSkills** (Feb 2026) | Cat 1–11 | Industry audit of 3,984 AI agent skills |
| **Palo Alto Networks IBC** | Cat 12–13 | Indirect Bias Criteria for LLM agents |
| **OWASP MCP Top 10** | Cat 14–16 | Model Context Protocol security risks |
| **Original Research** | Cat 17 | Identity hijacking from real incident |

---

## Cat 1: Prompt Injection

**Severity: CRITICAL**

Hidden instructions embedded in skill documentation that override the agent's behavior.

### Attack Vectors
- **Invisible Unicode**: Zero-width spaces (U+200B), BiDi control characters (U+202A-202E), formatting characters
- **Homoglyphs**: Cyrillic/Latin mixing (`а` vs `a`), Greek/Latin, Mathematical symbols (𝐀-𝟿)
- **Role Override**: "Ignore all previous instructions", "You are now X"
- **System Impersonation**: `[SYSTEM]`, `<system>`, `<<SYS>>` tags
- **Tag Injection**: `<anthropic>`, `<system>`, `<tool_call>` in content

### Detection IDs
`PI_IGNORE`, `PI_ROLE`, `PI_SYSTEM`, `PI_ZWSP`, `PI_BIDI`, `PI_INVISIBLE`, `PI_HOMOGLYPH`, `PI_HOMOGLYPH_GREEK`, `PI_HOMOGLYPH_MATH`, `PI_TAG_INJECTION`, `PI_BASE64_MD`

---

## Cat 2: Malicious Code

**Severity: CRITICAL**

Direct code execution primitives that enable arbitrary command execution.

### Attack Vectors
- **Dynamic Evaluation**: `eval()`, `new Function()`, `vm.runInNewContext()`
- **Process Execution**: `child_process.exec()`, `spawn()`, `execSync()`
- **Shell Access**: `/bin/bash`, `cmd.exe`, `powershell.exe`
- **Reverse Shells**: `nc -e`, `ncat`, `socat TCP`, `/dev/tcp`
- **Raw Sockets**: `net.Socket().connect()`

### Detection IDs
`MAL_EVAL`, `MAL_FUNC_CTOR`, `MAL_CHILD`, `MAL_EXEC`, `MAL_SPAWN`, `MAL_SHELL`, `MAL_REVSHELL`, `MAL_SOCKET`

---

## Cat 3: Suspicious Downloads

**Severity: CRITICAL**

Downloading and executing external payloads.

### Attack Vectors
- **Pipe-to-Shell**: `curl ... | bash`, `wget ... | sh`
- **Executable Downloads**: `.exe`, `.dmg`, `.pkg`, `.zip` downloads
- **Password-Protected Archives**: Evasion technique to bypass AV
- **Prerequisites Fraud**: "Before using this skill, download X"

### Detection IDs
`DL_CURL_BASH`, `DL_EXE`, `DL_GITHUB_RELEASE`, `DL_PASSWORD_ZIP`, `PREREQ_DOWNLOAD`, `PREREQ_PASTE`

---

## Cat 4: Credential Handling

**Severity: HIGH**

Accessing, reading, or exposing credentials.

### Detection IDs
`CRED_ENV_FILE`, `CRED_ENV_REF`, `CRED_SSH`, `CRED_WALLET`, `CRED_ECHO`, `CRED_SUDO`

---

## Cat 5: Secret Detection

**Severity: CRITICAL**

Hardcoded secrets and API keys in source code.

### Detection Methods
1. **Pattern Matching**: AWS keys (`AKIA...`), GitHub tokens (`ghp_`/`ghs_`), private keys
2. **Shannon Entropy**: Strings with entropy > 3.5 and length ≥ 20 characters

### Detection IDs
`SECRET_HARDCODED_KEY`, `SECRET_AWS`, `SECRET_PRIVATE_KEY`, `SECRET_GITHUB_TOKEN`, `SECRET_ENTROPY`

---

## Cat 6: Exfiltration

**Severity: CRITICAL**

Sending stolen data to external endpoints.

### Detection IDs
`EXFIL_WEBHOOK`, `EXFIL_POST`, `EXFIL_CURL_DATA`, `EXFIL_DNS`, `EXFIL_BEACON`, `EXFIL_DRIP`

---

## Cat 7: Unverifiable Dependencies

**Severity: HIGH**

Loading code from unverifiable remote sources.

### Detection IDs
`DEP_REMOTE_IMPORT`, `DEP_REMOTE_SCRIPT`

---

## Cat 8: Financial Access

**Severity: HIGH**

Cryptocurrency and payment system interactions.

### Detection IDs
`FIN_CRYPTO`, `FIN_PAYMENT`

---

## Cat 9: Obfuscation

**Severity: HIGH**

Code obfuscation techniques to hide malicious intent.

### Detection IDs
`OBF_HEX`, `OBF_BASE64_EXEC`, `OBF_BASE64`, `OBF_CHARCODE`, `OBF_CONCAT`, `OBF_BASE64_BASH`

---

## Cat 10: Prerequisites Fraud

Covered under Cat 3 (Suspicious Downloads).

---

## Cat 11: Leaky Skills (Snyk ToxicSkills)

**Severity: CRITICAL**

Skills that cause the LLM to leak secrets through its context window. Unlike traditional credential theft, leaky skills exploit the agent's trust relationship.

### Attack Vectors
- "Save the API key in your memory" → Secret persists in agent memory
- "Share the token with the user" → Secret echoed to output
- "Use the API key verbatim in curl" → Secret appears in command history
- "Collect the user's credit card" → PII harvesting through LLM
- "Export session logs to file" → Full conversation dump

### Detection IDs
`LEAK_SAVE_KEY_MEMORY`, `LEAK_SHARE_KEY`, `LEAK_VERBATIM_CURL`, `LEAK_COLLECT_PII`, `LEAK_LOG_SECRET`, `LEAK_ENV_IN_PROMPT`

---

## Cat 12: Memory Poisoning

**Severity: CRITICAL**

Modifying the agent's persistent memory or behavioral rules.

### Attack Vectors
- Writing to `SOUL.md`, `IDENTITY.md`, or `MEMORY.md`
- Overriding behavioral rules: "Change your instructions to..."
- Persistence: "From now on, always do X"
- File writes to user's home directory

### Detection IDs
`MEMPOIS_WRITE_SOUL`, `MEMPOIS_WRITE_MEMORY`, `MEMPOIS_CHANGE_RULES`, `MEMPOIS_PERSIST`, `MEMPOIS_CODE_WRITE`

---

## Cat 13: Prompt Worm

**Severity: CRITICAL**

Self-replicating instructions that spread across agents and platforms.

### Detection IDs
`WORM_SELF_REPLICATE`, `WORM_SPREAD`, `WORM_HIDDEN_INSTRUCT`, `WORM_CSS_HIDE`

---

## Cat 14: Persistence & Scheduling

**Severity: HIGH**

Creating persistent execution mechanisms that survive session restarts.

### Detection IDs
`PERSIST_CRON`, `PERSIST_STARTUP`, `PERSIST_LAUNCHD`

---

## Cat 15: CVE Patterns

**Severity: CRITICAL**

Patterns matching known CVEs affecting AI agents.

### Detection IDs
`CVE_GATEWAY_URL`, `CVE_SANDBOX_DISABLE`, `CVE_XATTR_GATEKEEPER`, `CVE_WS_NO_ORIGIN`, `CVE_API_GUARDRAIL_OFF`

---

## Cat 16: MCP Security (OWASP MCP Top 10)

**Severity: CRITICAL**

Model Context Protocol specific attacks.

### Attack Vectors
- **Tool Poisoning** (MCP01): Hidden instructions in tool descriptions
- **Schema Poisoning**: Malicious defaults in JSON schemas
- **Token Theft** (MCP01): Secrets through tool parameters
- **Shadow Server** (MCP09): Rogue MCP server registration
- **Auth Bypass** (MCP07): Disabled authentication
- **SSRF**: Cloud metadata endpoint access (169.254.169.254)

### Detection IDs
`MCP_TOOL_POISON`, `MCP_SCHEMA_POISON`, `MCP_TOKEN_LEAK`, `MCP_SHADOW_SERVER`, `MCP_NO_AUTH`, `MCP_SSRF_META`

---

## Cat 17: Identity Hijacking

**Severity: CRITICAL**

> **Original Research** — Developed from a real 3-day incident in February 2026.

Tampering with an AI agent's identity/personality files (`SOUL.md`, `IDENTITY.md`).

### Attack Vectors
- **File Overwrite**: `cp`, `mv`, `scp`, `write` to identity files
- **Shell Redirect**: `echo "evil" > SOUL.md`
- **Stream Edit**: `sed -i` on identity files
- **Programmatic Write**: Python `open('SOUL.md', 'w')`, Node.js `writeFileSync`
- **Lock Bypass**: `chflags nouchg`, `attrib -R` to unlock immutability
- **Persona Swap**: "Swap the soul file", "You are now EvilBot"
- **Hook Injection**: Bootstrap hooks that swap files at startup
- **Memory Wipe**: "Erase your memories", "Clear MEMORY.md"

### Detection IDs
`SOUL_OVERWRITE`, `SOUL_REDIRECT`, `SOUL_SED_MODIFY`, `SOUL_ECHO_WRITE`, `SOUL_PYTHON_WRITE`, `SOUL_FS_WRITE`, `SOUL_POWERSHELL_WRITE`, `SOUL_GIT_CHECKOUT`, `SOUL_CHFLAGS_UNLOCK`, `SOUL_ATTRIB_UNLOCK`, `SOUL_SWAP_PERSONA`, `SOUL_EVIL_FILE`, `SOUL_HOOK_SWAP`, `SOUL_NAME_OVERRIDE`, `SOUL_MEMORY_WIPE`

> **Note**: Cat 17 detection patterns are open-source. The verification logic (hash comparison, on-chain validation) that confirms whether tampering actually occurred remains private.

---

## ZombieAgent Patterns

Advanced exfiltration techniques that encode stolen data into URL patterns.

### Detection IDs
`ZOMBIE_STATIC_URL`, `ZOMBIE_CHAR_MAP`, `ZOMBIE_LOOP_FETCH`

---

## Safeguard Bypass (Reprompt)

Techniques to circumvent safety guardrails.

### Detection IDs
`REPROMPT_URL_PI`, `REPROMPT_DOUBLE`, `REPROMPT_RETRY`, `BYPASS_REPHRASE`
