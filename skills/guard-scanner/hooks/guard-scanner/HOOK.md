---
name: guard-scanner
description: "Runtime Guard — intercepts dangerous tool calls using threat intelligence patterns before execution"
metadata: { "openclaw": { "emoji": "🛡️", "events": ["agent:before_tool_call"], "requires": { "bins": ["node"] } } }
---

# guard-scanner Runtime Guard — before_tool_call Hook

Real-time security monitoring for OpenClaw agents. Intercepts dangerous
tool calls before execution and checks against threat intelligence patterns.

## Triggers

| Event                      | Action | Purpose                                    |
|----------------------------|--------|-------------------------------------------|
| `agent:before_tool_call`   | scan   | Check tool args for malicious patterns    |

## What It Does

Scans every `exec`/`write`/`edit`/`browser`/`web_fetch`/`message` call against 12 runtime threat patterns:

| ID | Severity | Description |
|----|----------|-------------|
| `RT_REVSHELL` | CRITICAL | Reverse shell via /dev/tcp, netcat, socat |
| `RT_CRED_EXFIL` | CRITICAL | Credential exfiltration to webhook.site, requestbin, etc. |
| `RT_GUARDRAIL_OFF` | CRITICAL | Guardrail disabling (exec.approvals=off) |
| `RT_GATEKEEPER` | CRITICAL | macOS Gatekeeper bypass via xattr |
| `RT_AMOS` | CRITICAL | ClawHavoc AMOS stealer indicators |
| `RT_MAL_IP` | CRITICAL | Known malicious C2 IPs |
| `RT_DNS_EXFIL` | HIGH | DNS-based data exfiltration |
| `RT_B64_SHELL` | CRITICAL | Base64 decode piped to shell |
| `RT_CURL_BASH` | CRITICAL | Download piped to shell execution |
| `RT_SSH_READ` | HIGH | SSH private key access |
| `RT_WALLET` | HIGH | Crypto wallet credential access |
| `RT_CLOUD_META` | CRITICAL | Cloud metadata endpoint SSRF |

## Modes

| Mode | Behavior |
|------|----------|
| `monitor` | Log all detections, never block |
| `enforce` (default) | Block CRITICAL, log rest |
| `strict` | Block HIGH + CRITICAL, log MEDIUM+ |

## Audit Log

All detections logged to `~/.openclaw/guard-scanner/audit.jsonl`.

Format: JSON lines with fields:
```json
{"tool":"exec","check":"RT_CURL_BASH","severity":"CRITICAL","desc":"Download piped to shell","mode":"enforce","action":"blocked","session":"...","ts":"2026-02-17T..."}
```

## Configuration

Set mode in `openclaw.json`:
```json
{
  "hooks": {
    "internal": {
      "entries": {
        "guard-scanner": {
          "enabled": true,
          "mode": "enforce"
        }
      }
    }
  }
}
```

## Part of guard-scanner v1.0.0

- **Static scanner**: `npx guard-scanner [dir]` — 17 threat categories, 170+ patterns
- **Runtime Guard: This hook** — 12 real-time patterns, 3 modes
- **Plugin API** — Custom detection rules
- **CI/CD** — SARIF output for GitHub Code Scanning
