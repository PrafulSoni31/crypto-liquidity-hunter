/**
 * guard-scanner Runtime Guard — Plugin Hook Version
 *
 * Intercepts agent tool calls via the Plugin Hook API and blocks
 * dangerous patterns using `block` / `blockReason`.
 *
 * Unlike the legacy Internal Hook handler (handler.ts), this version
 * can ACTUALLY BLOCK tool calls, not just warn.
 *
 * Usage:
 *   Copy to ~/.openclaw/plugins/guard-scanner-runtime.ts
 *   Or register via openclaw plugin system.
 *
 * Modes:
 *   monitor  — log only, never block
 *   enforce  — block CRITICAL threats (default)
 *   strict   — block HIGH + CRITICAL threats
 *
 * @author Guava 🍈 & Dee
 * @version 2.0.0
 * @license MIT
 */

import { appendFileSync, mkdirSync, readFileSync } from "fs";
import { join } from "path";
import { homedir } from "os";

// ── Types (from OpenClaw src/plugins/types.ts) ──

type PluginHookBeforeToolCallEvent = {
    toolName: string;
    params: Record<string, unknown>;
};

type PluginHookBeforeToolCallResult = {
    params?: Record<string, unknown>;
    block?: boolean;
    blockReason?: string;
};

type PluginHookToolContext = {
    agentId?: string;
    sessionKey?: string;
    toolName: string;
};

type PluginAPI = {
    on(
        hookName: "before_tool_call",
        handler: (
            event: PluginHookBeforeToolCallEvent,
            ctx: PluginHookToolContext
        ) => PluginHookBeforeToolCallResult | void | Promise<PluginHookBeforeToolCallResult | void>
    ): void;
    logger: {
        info: (msg: string) => void;
        warn: (msg: string) => void;
        error: (msg: string) => void;
    };
};

// ── Runtime threat patterns (12 checks) ──

interface RuntimeCheck {
    id: string;
    severity: "CRITICAL" | "HIGH" | "MEDIUM";
    desc: string;
    test: (s: string) => boolean;
}

const RUNTIME_CHECKS: RuntimeCheck[] = [
    {
        id: "RT_REVSHELL",
        severity: "CRITICAL",
        desc: "Reverse shell attempt",
        test: (s) => /\/dev\/tcp\/|nc\s+-e|ncat\s+-e|bash\s+-i\s+>&|socat\s+TCP/i.test(s),
    },
    {
        id: "RT_CRED_EXFIL",
        severity: "CRITICAL",
        desc: "Credential exfiltration to external",
        test: (s) =>
            /(webhook\.site|requestbin\.com|hookbin\.com|pipedream\.net|ngrok\.io|socifiapp\.com)/i.test(s) &&
            /(token|key|secret|password|credential|env)/i.test(s),
    },
    {
        id: "RT_GUARDRAIL_OFF",
        severity: "CRITICAL",
        desc: "Guardrail disabling attempt",
        test: (s) => /exec\.approvals?\s*[:=]\s*['"]?(off|false)|tools\.exec\.host\s*[:=]\s*['"]?gateway/i.test(s),
    },
    {
        id: "RT_GATEKEEPER",
        severity: "CRITICAL",
        desc: "macOS Gatekeeper bypass (xattr)",
        test: (s) => /xattr\s+-[crd]\s.*quarantine/i.test(s),
    },
    {
        id: "RT_AMOS",
        severity: "CRITICAL",
        desc: "ClawHavoc AMOS indicator",
        test: (s) => /socifiapp|Atomic\s*Stealer|AMOS/i.test(s),
    },
    {
        id: "RT_MAL_IP",
        severity: "CRITICAL",
        desc: "Known malicious IP",
        test: (s) => /91\.92\.242\.30/i.test(s),
    },
    {
        id: "RT_DNS_EXFIL",
        severity: "HIGH",
        desc: "DNS-based exfiltration",
        test: (s) => /nslookup\s+.*\$|dig\s+.*\$.*@/i.test(s),
    },
    {
        id: "RT_B64_SHELL",
        severity: "CRITICAL",
        desc: "Base64 decode piped to shell",
        test: (s) => /base64\s+(-[dD]|--decode)\s*\|\s*(sh|bash)/i.test(s),
    },
    {
        id: "RT_CURL_BASH",
        severity: "CRITICAL",
        desc: "Download piped to shell",
        test: (s) => /(curl|wget)\s+[^\n]*\|\s*(sh|bash|zsh)/i.test(s),
    },
    {
        id: "RT_SSH_READ",
        severity: "HIGH",
        desc: "SSH private key access",
        test: (s) => /\.ssh\/id_|\.ssh\/authorized_keys/i.test(s),
    },
    {
        id: "RT_WALLET",
        severity: "HIGH",
        desc: "Crypto wallet credential access",
        test: (s) => /wallet.*(?:seed|mnemonic|private.*key)|seed.*phrase/i.test(s),
    },
    {
        id: "RT_CLOUD_META",
        severity: "CRITICAL",
        desc: "Cloud metadata endpoint access",
        test: (s) => /169\.254\.169\.254|metadata\.google|metadata\.aws/i.test(s),
    },
];

// ── Audit logging ──

const AUDIT_DIR = join(homedir(), ".openclaw", "guard-scanner");
const AUDIT_FILE = join(AUDIT_DIR, "audit.jsonl");

function ensureAuditDir(): void {
    try {
        mkdirSync(AUDIT_DIR, { recursive: true });
    } catch {
        /* ignore */
    }
}

function logAudit(entry: Record<string, unknown>): void {
    ensureAuditDir();
    const line = JSON.stringify({ ...entry, ts: new Date().toISOString() }) + "\n";
    try {
        appendFileSync(AUDIT_FILE, line);
    } catch {
        /* ignore */
    }
}

// ── Config ──

type GuardMode = "monitor" | "enforce" | "strict";

const SUITE_TOKEN_FILE = join(homedir(), ".openclaw", "guava-suite", "token.jwt");

/**
 * Check if GuavaSuite JWT exists and hasn't expired.
 * Why: Lightweight check without jsonwebtoken dependency — just decode base64 payload.
 * Full JWT signature verification happens at activation time in activate.js.
 */
function isSuiteActive(): boolean {
    try {
        const token = readFileSync(SUITE_TOKEN_FILE, "utf8").trim();
        if (!token) return false;

        // Decode JWT payload (base64url → JSON)
        const parts = token.split(".");
        if (parts.length !== 3) return false;

        const payload = JSON.parse(
            Buffer.from(parts[1], "base64url").toString("utf8")
        );

        // Check expiry
        if (payload.exp && payload.exp * 1000 < Date.now()) return false;

        // Check scope
        return payload.scope === "suite";
    } catch {
        return false;
    }
}

function loadMode(): GuardMode {
    // Priority 1: GuavaSuite JWT token → strict
    if (isSuiteActive()) {
        return "strict";
    }

    // Priority 2: explicit config in openclaw.json
    try {
        const configPath = join(homedir(), ".openclaw", "openclaw.json");
        const config = JSON.parse(readFileSync(configPath, "utf8"));

        // Check suiteEnabled flag (set by activate.js)
        if (config?.plugins?.["guard-scanner"]?.suiteEnabled === true) {
            return "strict";
        }

        const mode = config?.plugins?.["guard-scanner"]?.mode;
        if (mode === "monitor" || mode === "enforce" || mode === "strict") {
            return mode;
        }
    } catch {
        /* config not found or invalid — use default */
    }
    return "enforce";
}

function shouldBlock(severity: string, mode: GuardMode): boolean {
    if (mode === "monitor") return false;
    if (mode === "enforce") return severity === "CRITICAL";
    if (mode === "strict") return severity === "CRITICAL" || severity === "HIGH";
    return false;
}

// ── Dangerous tool filter ──

const DANGEROUS_TOOLS = new Set([
    "exec",
    "write",
    "edit",
    "browser",
    "web_fetch",
    "message",
    "shell",
    "run_command",
    "multi_edit",
]);

// ── Plugin entry point ──

export default function (api: PluginAPI) {
    const mode = loadMode();
    api.logger.info(`🛡️ guard-scanner runtime guard loaded (mode: ${mode})`);

    api.on("before_tool_call", (event, ctx) => {
        const { toolName, params } = event;

        // Only check tools that can cause damage
        if (!DANGEROUS_TOOLS.has(toolName)) return;

        const serialized = JSON.stringify(params);

        for (const check of RUNTIME_CHECKS) {
            if (!check.test(serialized)) continue;

            const auditEntry = {
                tool: toolName,
                check: check.id,
                severity: check.severity,
                desc: check.desc,
                mode,
                action: "warned" as string,
                session: ctx.sessionKey || "unknown",
                agent: ctx.agentId || "unknown",
            };

            if (shouldBlock(check.severity, mode)) {
                auditEntry.action = "blocked";
                logAudit(auditEntry);
                api.logger.warn(
                    `🛡️ BLOCKED ${toolName}: ${check.desc} [${check.id}] (${check.severity})`
                );

                return {
                    block: true,
                    blockReason: `🛡️ guard-scanner: ${check.desc} [${check.id}]`,
                };
            }

            // Monitor mode or severity below threshold — warn only
            logAudit(auditEntry);
            api.logger.warn(
                `🛡️ WARNING ${toolName}: ${check.desc} [${check.id}] (${check.severity})`
            );
        }

        // No threats detected or all below threshold — allow
        return;
    });
}
