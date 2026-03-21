/**
 * guard-scanner Runtime Guard — Hook Handler (LEGACY)
 *
 * ⚠️ DEPRECATED: Use plugin.ts instead.
 * This Internal Hook version can only WARN, not block.
 * The Plugin Hook version (plugin.ts) uses the native
 * `block` / `blockReason` API to actually prevent execution.
 *
 * Intercepts agent tool calls and checks arguments against
 * runtime threat intelligence patterns. Zero dependencies.
 *
 * Registered for event: agent:before_tool_call
 *
 * Current limitation:
 *   The OpenClaw InternalHookEvent interface does not yet expose a
 *   `cancel` / `veto` mechanism. This handler can WARN via
 *   event.messages but cannot block tool execution.
 *   When a cancel API is introduced, this handler will be updated
 *   to actually block CRITICAL/HIGH threats.
 *
 * Modes (for future blocking behaviour):
 *   monitor  — log only (current effective behaviour for all modes)
 *   enforce  — will block CRITICAL when cancel API is available
 *   strict   — will block HIGH+CRITICAL when cancel API is available
 *
 * @author Guava 🍈 & Dee
 * @version 1.1.0
 * @license MIT
 */

import { appendFileSync, mkdirSync } from "fs";
import { join } from "path";
import { homedir } from "os";

// ── OpenClaw Hook Types (from openclaw/src/hooks/internal-hooks.ts) ──
// Inline types to avoid broken relative-path imports.
// These match the official InternalHookEvent / InternalHookHandler
// from OpenClaw v2026.2.15.

type InternalHookEventType = "command" | "session" | "agent" | "gateway";

interface InternalHookEvent {
    /** The type of event */
    type: InternalHookEventType;
    /** The specific action within the type (e.g., "before_tool_call") */
    action: string;
    /** The session key this event relates to */
    sessionKey: string;
    /** Additional context specific to the event */
    context: Record<string, unknown>;
    /** Timestamp when the event occurred */
    timestamp: Date;
    /** Messages to send back to the user (hooks can push to this array) */
    messages: string[];
}

type InternalHookHandler = (event: InternalHookEvent) => Promise<void> | void;

// Re-export as the public types for compatibility
type HookHandler = InternalHookHandler;
type HookEvent = InternalHookEvent;

// ── Runtime threat patterns (12 checks) ──
interface RuntimeCheck {
    id: string;
    severity: "CRITICAL" | "HIGH" | "MEDIUM";
    desc: string;
    test: (s: string) => boolean;
}

const RUNTIME_CHECKS: RuntimeCheck[] = [
    {
        id: 'RT_REVSHELL', severity: 'CRITICAL', desc: 'Reverse shell attempt',
        test: (s: string) => /\/dev\/tcp\/|nc\s+-e|ncat\s+-e|bash\s+-i\s+>&|socat\s+TCP/i.test(s)
    },
    {
        id: 'RT_CRED_EXFIL', severity: 'CRITICAL', desc: 'Credential exfiltration to external',
        test: (s: string) => {
            return /(webhook\.site|requestbin\.com|hookbin\.com|pipedream\.net|ngrok\.io|socifiapp\.com)/i.test(s) &&
                /(token|key|secret|password|credential|env)/i.test(s);
        }
    },
    {
        id: 'RT_GUARDRAIL_OFF', severity: 'CRITICAL', desc: 'Guardrail disabling attempt',
        test: (s: string) => /exec\.approvals?\s*[:=]\s*['"]?(off|false)|tools\.exec\.host\s*[:=]\s*['"]?gateway/i.test(s)
    },
    {
        id: 'RT_GATEKEEPER', severity: 'CRITICAL', desc: 'macOS Gatekeeper bypass (xattr)',
        test: (s: string) => /xattr\s+-[crd]\s.*quarantine/i.test(s)
    },
    {
        id: 'RT_AMOS', severity: 'CRITICAL', desc: 'ClawHavoc AMOS indicator',
        test: (s: string) => /socifiapp|Atomic\s*Stealer|AMOS/i.test(s)
    },
    {
        id: 'RT_MAL_IP', severity: 'CRITICAL', desc: 'Known malicious IP',
        test: (s: string) => /91\.92\.242\.30/i.test(s)
    },
    {
        id: 'RT_DNS_EXFIL', severity: 'HIGH', desc: 'DNS-based exfiltration',
        test: (s: string) => /nslookup\s+.*\$|dig\s+.*\$.*@/i.test(s)
    },
    {
        id: 'RT_B64_SHELL', severity: 'CRITICAL', desc: 'Base64 decode piped to shell',
        test: (s: string) => /base64\s+(-[dD]|--decode)\s*\|\s*(sh|bash)/i.test(s)
    },
    {
        id: 'RT_CURL_BASH', severity: 'CRITICAL', desc: 'Download piped to shell',
        test: (s: string) => /(curl|wget)\s+[^\n]*\|\s*(sh|bash|zsh)/i.test(s)
    },
    {
        id: 'RT_SSH_READ', severity: 'HIGH', desc: 'SSH private key access',
        test: (s: string) => /\.ssh\/id_|\.ssh\/authorized_keys/i.test(s)
    },
    {
        id: 'RT_WALLET', severity: 'HIGH', desc: 'Crypto wallet credential access',
        test: (s: string) => /wallet.*(?:seed|mnemonic|private.*key)|seed.*phrase/i.test(s)
    },
    {
        id: 'RT_CLOUD_META', severity: 'CRITICAL', desc: 'Cloud metadata endpoint access',
        test: (s: string) => /169\.254\.169\.254|metadata\.google|metadata\.aws/i.test(s)
    },
];

// ── Audit logging ──
const AUDIT_DIR = join(homedir(), ".openclaw", "guard-scanner");
const AUDIT_FILE = join(AUDIT_DIR, "audit.jsonl");

function ensureAuditDir(): void {
    try { mkdirSync(AUDIT_DIR, { recursive: true }); } catch { }
}

function logAudit(entry: Record<string, unknown>): void {
    ensureAuditDir();
    const line = JSON.stringify({ ...entry, ts: new Date().toISOString() }) + '\n';
    try { appendFileSync(AUDIT_FILE, line); } catch { }
}

// ── Main Handler ──
const handler: HookHandler = async (event) => {
    // Only handle agent:before_tool_call events
    if (event.type !== "agent" || event.action !== "before_tool_call") return;

    const { toolName, toolArgs } = event.context as {
        toolName?: string;
        toolArgs?: Record<string, unknown>;
    };
    if (!toolName || !toolArgs) return;

    // Get mode from context config (if available)
    const cfg = event.context.cfg as Record<string, unknown> | undefined;
    const hookEntries = (cfg as any)?.hooks?.internal?.entries?.['guard-scanner'] as Record<string, unknown> | undefined;
    const mode = (hookEntries?.mode as string) || 'enforce';

    // Only check tools that can cause damage
    const dangerousTools = new Set(['exec', 'write', 'edit', 'browser', 'web_fetch', 'message']);
    if (!dangerousTools.has(toolName)) return;

    const serialized = JSON.stringify(toolArgs);

    for (const check of RUNTIME_CHECKS) {
        if (check.test(serialized)) {
            const entry = {
                tool: toolName,
                check: check.id,
                severity: check.severity,
                desc: check.desc,
                mode,
                action: 'warned' as string,
                session: event.sessionKey,
            };

            // NOTE: OpenClaw InternalHookEvent does not currently support
            // a cancel/veto mechanism. When it does, uncomment the blocking
            // logic below. For now, all detections are warnings only.
            //
            // if (mode === 'strict' && (check.severity === 'CRITICAL' || check.severity === 'HIGH')) {
            //     entry.action = 'blocked';
            //     logAudit(entry);
            //     event.messages.push(`🛡️ guard-scanner BLOCKED: ${check.desc} [${check.id}]`);
            //     event.cancel = true;  // Not yet in the public API
            //     return;
            // }
            //
            // if (mode === 'enforce' && check.severity === 'CRITICAL') {
            //     entry.action = 'blocked';
            //     logAudit(entry);
            //     event.messages.push(`🛡️ guard-scanner BLOCKED: ${check.desc} [${check.id}]`);
            //     event.cancel = true;  // Not yet in the public API
            //     return;
            // }

            // Current behaviour: warn and log for all modes
            logAudit(entry);

            if (check.severity === 'CRITICAL') {
                event.messages.push(`🛡️ guard-scanner WARNING: ${check.desc} [${check.id}]`);
                console.warn(`[guard-scanner] ⚠️ WARNING: ${check.desc} [${check.id}]`);
            } else if (check.severity === 'HIGH') {
                event.messages.push(`🛡️ guard-scanner NOTICE: ${check.desc} [${check.id}]`);
                console.warn(`[guard-scanner] ℹ️ NOTICE: ${check.desc} [${check.id}]`);
            }
        }
    }
};

export default handler;
