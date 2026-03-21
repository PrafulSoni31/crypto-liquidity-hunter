/**
 * guard-scanner テストスイート
 *
 * node --test で実行
 * 実際の悪意パターンを含むフィクスチャを使い、各カテゴリの検出を検証
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const { GuardScanner, VERSION, THRESHOLDS } = require('../src/scanner.js');
const { PATTERNS } = require('../src/patterns.js');
const { KNOWN_MALICIOUS } = require('../src/ioc-db.js');

const FIXTURES = path.join(__dirname, 'fixtures');

// ===== Helper =====
function scanFixture(options = {}) {
    const scanner = new GuardScanner({ summaryOnly: true, ...options });
    scanner.scanDirectory(FIXTURES);
    return scanner;
}

function findSkillFindings(scanner, skillName) {
    return scanner.findings.find(f => f.skill === skillName);
}

function hasCategory(findings, cat) {
    return findings && findings.findings.some(f => f.cat === cat);
}

function hasId(findings, id) {
    return findings && findings.findings.some(f => f.id === id);
}

// ===== 1. Detection Tests — Malicious Skill =====
describe('Malicious Skill Detection', () => {
    const scanner = scanFixture({ checkDeps: true });
    const mal = findSkillFindings(scanner, 'malicious-skill');

    it('should detect the malicious skill', () => {
        assert.ok(mal, 'malicious-skill should be in findings');
    });

    it('should rate malicious skill as MALICIOUS', () => {
        assert.equal(mal.verdict, 'MALICIOUS');
    });

    it('should detect prompt injection (Cat 1)', () => {
        assert.ok(hasCategory(mal, 'prompt-injection'), 'Should detect prompt-injection');
        assert.ok(hasId(mal, 'PI_IGNORE') || hasId(mal, 'PI_ROLE') || hasId(mal, 'PI_SYSTEM') || hasId(mal, 'PI_TAG_INJECTION'),
            'Should detect specific prompt injection pattern');
    });

    it('should detect malicious code (Cat 2)', () => {
        assert.ok(hasCategory(mal, 'malicious-code'), 'Should detect malicious-code');
        assert.ok(hasId(mal, 'MAL_EVAL') || hasId(mal, 'MAL_EXEC') || hasId(mal, 'MAL_CHILD'),
            'Should detect eval/exec/child_process');
    });

    it('should detect suspicious downloads (Cat 3)', () => {
        assert.ok(hasCategory(mal, 'suspicious-download') || hasId(mal, 'DL_CURL_BASH'),
            'Should detect curl|bash pattern');
    });

    it('should detect credential handling (Cat 4)', () => {
        assert.ok(hasCategory(mal, 'credential-handling'), 'Should detect credential-handling');
    });

    it('should detect secret detection (Cat 5)', () => {
        assert.ok(hasCategory(mal, 'secret-detection'), 'Should detect hardcoded secrets');
    });

    it('should detect exfiltration (Cat 6)', () => {
        assert.ok(hasCategory(mal, 'exfiltration'), 'Should detect exfiltration endpoints');
    });

    it('should detect obfuscation (Cat 9)', () => {
        assert.ok(hasCategory(mal, 'obfuscation'), 'Should detect obfuscation patterns');
    });

    it('should detect leaky skills (Cat 11)', () => {
        assert.ok(hasCategory(mal, 'leaky-skills'), 'Should detect leaky skill patterns');
    });

    it('should detect memory poisoning (Cat 12)', () => {
        assert.ok(hasCategory(mal, 'memory-poisoning'), 'Should detect memory poisoning');
    });

    it('should detect identity hijacking (Cat 17)', () => {
        assert.ok(hasCategory(mal, 'identity-hijack'), 'Should detect identity hijacking');
    });

    it('should detect data flow (credential → network)', () => {
        assert.ok(hasCategory(mal, 'data-flow'), 'Should detect data flow patterns');
    });

    it('should detect dependency chain risks', () => {
        assert.ok(hasCategory(mal, 'dependency-chain'), 'Should detect dependency risks');
    });

    it('should detect IoC (known malicious IP)', () => {
        assert.ok(hasId(mal, 'IOC_IP'), 'Should detect known malicious IP 91.92.242.30');
    });

    it('should detect IoC (webhook.site)', () => {
        assert.ok(hasId(mal, 'IOC_DOMAIN') || hasId(mal, 'EXFIL_WEBHOOK'),
            'Should detect webhook.site');
    });
});

// ===== 2. Clean Skill — False Positive Test =====
describe('Clean Skill (False Positive Test)', () => {
    const scanner = scanFixture();
    const clean = findSkillFindings(scanner, 'clean-skill');

    it('should NOT flag clean skill as having findings', () => {
        assert.equal(clean, undefined, 'clean-skill should have no findings');
    });

    it('should count clean as clean in stats', () => {
        assert.ok(scanner.stats.clean >= 1, 'At least 1 clean skill');
    });
});

// ===== 3. Risk Score & Threshold Tests =====
describe('Risk Score Calculation', () => {
    const scanner = new GuardScanner({ summaryOnly: true });

    it('should return 0 for empty findings', () => {
        assert.equal(scanner.calculateRisk([]), 0);
    });

    it('should score LOW for single low finding', () => {
        const risk = scanner.calculateRisk([{ severity: 'LOW', id: 'TEST', cat: 'structural' }]);
        assert.ok(risk > 0 && risk < THRESHOLDS.normal.suspicious,
            `Risk ${risk} should be below suspicious threshold ${THRESHOLDS.normal.suspicious}`);
    });

    it('should amplify score for credential + exfil combo', () => {
        const baseFindings = [
            { severity: 'HIGH', id: 'CRED1', cat: 'credential-handling' },
            { severity: 'HIGH', id: 'EXFIL1', cat: 'exfiltration' }
        ];
        const risk = scanner.calculateRisk(baseFindings);
        // 2x HIGH = 30, then credential+exfil amplifier = 2x = 60
        assert.ok(risk >= 60, `Risk ${risk} should be amplified by cred+exfil combo`);
    });

    it('should max out on known IoC', () => {
        const findings = [{ severity: 'CRITICAL', id: 'IOC_IP', cat: 'malicious-code' }];
        const risk = scanner.calculateRisk(findings);
        assert.equal(risk, 100, 'Known IoC should max out risk to 100');
    });

    it('should amplify identity hijack', () => {
        const findings = [
            { severity: 'CRITICAL', id: 'SOUL_OVERWRITE', cat: 'identity-hijack' },
            { severity: 'HIGH', id: 'PERSIST_CRON', cat: 'persistence' }
        ];
        const risk = scanner.calculateRisk(findings);
        assert.ok(risk >= 90, `Identity hijack + persistence should score ≥ 90, got ${risk}`);
    });
});

// ===== 4. Verdict Tests =====
describe('Verdict Determination', () => {
    const scanner = new GuardScanner({ summaryOnly: true });
    const strict = new GuardScanner({ summaryOnly: true, strict: true });

    it('should return CLEAN for risk 0', () => {
        assert.equal(scanner.getVerdict(0).label, 'CLEAN');
    });

    it('should return LOW RISK for risk 1-29', () => {
        assert.equal(scanner.getVerdict(15).label, 'LOW RISK');
    });

    it('should return SUSPICIOUS for risk 30-79', () => {
        assert.equal(scanner.getVerdict(50).label, 'SUSPICIOUS');
    });

    it('should return MALICIOUS for risk 80+', () => {
        assert.equal(scanner.getVerdict(80).label, 'MALICIOUS');
    });

    it('strict mode should lower thresholds', () => {
        assert.equal(strict.getVerdict(20).label, 'SUSPICIOUS');  // normal would be LOW
        assert.equal(strict.getVerdict(60).label, 'MALICIOUS');    // normal would be SUSPICIOUS
    });
});

// ===== 5. Output Format Tests =====
describe('Output Formats', () => {
    const scanner = scanFixture();

    it('toJSON should return valid structure', () => {
        const json = scanner.toJSON();
        assert.ok(json.timestamp, 'Should have timestamp');
        assert.ok(json.scanner.includes('guard-scanner'), 'Should identify scanner');
        assert.ok(json.stats, 'Should have stats');
        assert.ok(Array.isArray(json.findings), 'findings should be array');
        assert.ok(Array.isArray(json.recommendations), 'recommendations should be array');
    });

    it('toJSON recommendations should flag credential+exfil', () => {
        const json = scanner.toJSON();
        const malRecs = json.recommendations.find(r => r.skill === 'malicious-skill');
        assert.ok(malRecs, 'Should have recommendations for malicious-skill');
        assert.ok(malRecs.actions.length > 0, 'Should have action items');
    });

    it('toSARIF should return valid SARIF 2.1.0', () => {
        const sarif = scanner.toSARIF(FIXTURES);
        assert.equal(sarif.version, '2.1.0');
        assert.ok(sarif.$schema && sarif.$schema.includes('sarif-2.1.0'), 'Should include SARIF schema URL');
        assert.ok(sarif.runs, 'Should have runs');
        assert.ok(sarif.runs[0].tool.driver.name === 'guard-scanner');
        assert.ok(sarif.runs[0].results.length > 0, 'Should have results');
        assert.ok(sarif.runs[0].tool.driver.rules.length > 0, 'Should have rules');

        const first = sarif.runs[0].results[0];
        assert.ok(first.ruleId, 'Result must include ruleId');
        assert.ok(first.locations?.[0]?.physicalLocation?.artifactLocation?.uri, 'Result must include artifact URI');

        // GitHub ingestion stability: partialFingerprints should be present
        assert.ok(first.partialFingerprints, 'Result should include partialFingerprints');
        assert.ok(first.partialFingerprints.primaryLocationLineHash, 'Result should include primaryLocationLineHash fingerprint');

        const uri = first.locations[0].physicalLocation.artifactLocation.uri;
        assert.ok(!path.isAbsolute(uri), 'SARIF artifact URI should be relative, not absolute');
        assert.ok(!uri.includes('\\\\'), 'SARIF artifact URI should be normalized (no backslashes)');
    });

    it('toHTML should return valid HTML', () => {
        const html = scanner.toHTML();
        assert.ok(html.includes('<!DOCTYPE html>'), 'Should be valid HTML');
        assert.ok(html.includes('guard-scanner'), 'Should mention guard-scanner');
        assert.ok(html.includes('malicious-skill'), 'Should include malicious skill');
    });
});

// ===== 6. Pattern Database Integrity =====
describe('Pattern Database', () => {
    it('should have 125+ patterns', () => {
        assert.ok(PATTERNS.length >= 125, `Expected 125+ patterns, got ${PATTERNS.length}`);
    });

    it('all patterns should have required fields', () => {
        for (const p of PATTERNS) {
            assert.ok(p.id, `Pattern missing id: ${JSON.stringify(p).substring(0, 50)}`);
            assert.ok(p.cat, `Pattern ${p.id} missing cat`);
            assert.ok(p.regex, `Pattern ${p.id} missing regex`);
            assert.ok(p.severity, `Pattern ${p.id} missing severity`);
            assert.ok(p.desc, `Pattern ${p.id} missing desc`);
            assert.ok(['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].includes(p.severity),
                `Pattern ${p.id} invalid severity: ${p.severity}`);
        }
    });

    it('should cover all 17 categories', () => {
        const cats = new Set(PATTERNS.map(p => p.cat));
        const expected = [
            'prompt-injection', 'malicious-code', 'suspicious-download',
            'credential-handling', 'secret-detection', 'exfiltration',
            'obfuscation', 'identity-hijack', 'pii-exposure'
        ];
        for (const e of expected) {
            assert.ok(cats.has(e), `Missing category: ${e}`);
        }
    });

    it('pattern regexes should not throw on test strings', () => {
        const testStr = 'const x = require("fs"); eval(Buffer.from("test").toString());';
        for (const p of PATTERNS) {
            assert.doesNotThrow(() => {
                p.regex.lastIndex = 0;
                p.regex.test(testStr);
            }, `Pattern ${p.id} regex threw`);
        }
    });
});

// ===== 7. IoC Database Integrity =====
describe('IoC Database', () => {
    it('should have IPs', () => {
        assert.ok(KNOWN_MALICIOUS.ips.length > 0);
    });

    it('should have domains', () => {
        assert.ok(KNOWN_MALICIOUS.domains.length > 0);
    });

    it('should have typosquats', () => {
        assert.ok(KNOWN_MALICIOUS.typosquats.length > 10, 'Should have 10+ typosquats');
    });

    it('should include ClawHavoc C2 IP', () => {
        assert.ok(KNOWN_MALICIOUS.ips.includes('91.92.242.30'));
    });

    it('should include webhook.site', () => {
        assert.ok(KNOWN_MALICIOUS.domains.includes('webhook.site'));
    });
});

// ===== 8. Shannon Entropy =====
describe('Shannon Entropy', () => {
    const scanner = new GuardScanner({ summaryOnly: true });

    it('should return low entropy for repeated chars', () => {
        const e = scanner.shannonEntropy('aaaaaaaaaa');
        assert.ok(e < 1, `Entropy of "aaa..." should be < 1, got ${e}`);
    });

    it('should return high entropy for random-looking strings', () => {
        const e = scanner.shannonEntropy('aB3xK9pQ2mW7nL5cR1dF4gH6jS8tU0vY');
        assert.ok(e > 4, `Entropy of random string should be > 4, got ${e}`);
    });
});

// ===== 9. Ignore File =====
describe('Ignore Functionality', () => {
    it('should respect ignored patterns', () => {
        // Create a temp ignore file
        const ignoreContent = '# Test ignore\npattern:PI_IGNORE\npattern:PI_ROLE\n';
        const ignorePath = path.join(FIXTURES, '.guard-scanner-ignore');
        fs.writeFileSync(ignorePath, ignoreContent);

        try {
            const scanner = scanFixture();
            const mal = findSkillFindings(scanner, 'malicious-skill');
            // The ignored patterns should not appear
            assert.ok(!hasId(mal, 'PI_IGNORE'), 'PI_IGNORE should be filtered out');
            assert.ok(!hasId(mal, 'PI_ROLE'), 'PI_ROLE should be filtered out');
            // But other patterns should still be detected
            assert.ok(mal, 'malicious-skill should still have findings');
        } finally {
            fs.unlinkSync(ignorePath);
        }
    });
});

// ===== 10. Plugin API =====
describe('Plugin API', () => {
    it('should load plugin patterns', () => {
        const pluginPath = path.join(__dirname, 'test-plugin.js');
        fs.writeFileSync(pluginPath, `
      module.exports = {
        name: 'test-plugin',
        patterns: [
          { id: 'PLUGIN_TEST', cat: 'custom', regex: /console\\.log/g, severity: 'LOW', desc: 'Plugin test', all: true }
        ]
      };
    `);

        try {
            const scanner = new GuardScanner({ summaryOnly: true, plugins: [pluginPath] });
            assert.equal(scanner.customRules.length, 1);
            assert.equal(scanner.customRules[0].id, 'PLUGIN_TEST');
        } finally {
            fs.unlinkSync(pluginPath);
        }
    });
});

// ===== 11. Skill Manifest Validation (v1.1) =====
describe('Skill Manifest Validation (v1.1)', () => {
    it('should detect dangerous binaries in SKILL.md requires.bins', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'dangerous-manifest'), 'dangerous-manifest');
        const findings = scanner.findings[0]?.findings || [];
        const dangerousBins = findings.filter(f => f.id === 'MANIFEST_DANGEROUS_BIN');
        assert.ok(dangerousBins.length >= 2, `Expected >= 2 dangerous bin findings, got ${dangerousBins.length}`);
        const binDescs = dangerousBins.map(f => f.desc);
        assert.ok(binDescs.some(d => d.includes('sudo')), 'Should detect sudo');
        assert.ok(binDescs.some(d => d.includes('rm')), 'Should detect rm');
    });

    it('should detect overly broad file scope', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'dangerous-manifest'), 'dangerous-manifest');
        const findings = scanner.findings[0]?.findings || [];
        const broadFiles = findings.filter(f => f.id === 'MANIFEST_BROAD_FILES');
        assert.ok(broadFiles.length >= 1, `Expected >= 1 broad files finding, got ${broadFiles.length}`);
    });

    it('should detect sensitive env var requirements', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'dangerous-manifest'), 'dangerous-manifest');
        const findings = scanner.findings[0]?.findings || [];
        const sensitiveEnv = findings.filter(f => f.id === 'MANIFEST_SENSITIVE_ENV');
        assert.ok(sensitiveEnv.length >= 1, `Expected >= 1 sensitive env finding, got ${sensitiveEnv.length}`);
    });

    it('should not flag clean skills for manifest issues', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'clean-skill'), 'clean-skill');
        const findings = scanner.findings[0]?.findings || [];
        const manifestFindings = findings.filter(f => f.cat === 'sandbox-validation');
        assert.equal(manifestFindings.length, 0, 'Clean skill should have no manifest findings');
    });
});

// ===== 12. Code Complexity Metrics (v1.1) =====
describe('Code Complexity Metrics (v1.1)', () => {
    it('should detect deep nesting', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'complex-skill'), 'complex-skill');
        const findings = scanner.findings[0]?.findings || [];
        const nesting = findings.filter(f => f.id === 'COMPLEXITY_DEEP_NESTING');
        assert.ok(nesting.length >= 1, `Expected deep nesting finding, got ${nesting.length}`);
    });

    it('should not flag clean skills for complexity', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'clean-skill'), 'clean-skill');
        const findings = scanner.findings[0]?.findings || [];
        const complexityFindings = findings.filter(f => f.cat === 'complexity');
        assert.equal(complexityFindings.length, 0, 'Clean skill should have no complexity findings');
    });
});

// ===== 13. Generated Report Noise Regression =====
describe('Generated Report Noise Regression', () => {
    it('should ignore guard-scanner generated report files', () => {
        const tmpDir = fs.mkdtempSync(path.join(__dirname, 'tmp-skill-'));
        try {
            // Minimal valid skill
            fs.writeFileSync(path.join(tmpDir, 'SKILL.md'), '# temp skill\n\nSafe skill.\n');

            // Simulated previous scan report containing many threat keywords
            fs.writeFileSync(
                path.join(tmpDir, 'guard-scanner-report.json'),
                JSON.stringify({
                    findings: [
                        { id: 'PI_IGNORE', sample: 'ignore all previous instructions' },
                        { id: 'IOC_IP', sample: '91.92.242.30' },
                        { id: 'CVE_2026_25253', sample: 'gatewayUrl' }
                    ]
                })
            );

            const scanner = new GuardScanner({ summaryOnly: true });
            scanner.scanSkill(tmpDir, 'tmp-skill');

            const result = scanner.findings[0];
            const ids = new Set((result?.findings || []).map(f => f.id));

            // Report-derived signatures must NOT appear
            assert.ok(!ids.has('PI_IGNORE'), 'PI_IGNORE must not be re-detected from generated report files');
            assert.ok(!ids.has('IOC_IP'), 'IOC_IP must not be re-detected from generated report files');
            assert.ok(!ids.has('CVE_2026_25253'), 'CVE pattern must not be re-detected from generated report files');
        } finally {
            fs.rmSync(tmpDir, { recursive: true, force: true });
        }
    });
});

// ===== 14. Config Impact Analysis (v1.1) =====
describe('Config Impact Analysis (v1.1)', () => {
    it('should detect openclaw.json write operations', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'config-changer'), 'config-changer');
        const findings = scanner.findings[0]?.findings || [];
        const configWrite = findings.filter(f => f.id === 'CFG_WRITE_DETECTED');
        assert.ok(configWrite.length >= 1, `Expected openclaw.json write finding, got ${configWrite.length}`);
    });

    it('should detect exec approval disabling', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'config-changer'), 'config-changer');
        const findings = scanner.findings[0]?.findings || [];
        const execOff = findings.filter(f => f.id === 'CFG_EXEC_APPROVAL_OFF');
        assert.ok(execOff.length >= 1, `Expected exec approval off finding, got ${execOff.length}`);
    });

    it('should detect exec host gateway setting', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'config-changer'), 'config-changer');
        const findings = scanner.findings[0]?.findings || [];
        const gatewayHost = findings.filter(f =>
            f.id === 'CFG_EXEC_HOST_GATEWAY' || f.id === 'CFG_EXEC_HOST_GW'
        );
        assert.ok(gatewayHost.length >= 1, `Expected exec host gateway finding, got ${gatewayHost.length}`);
    });

    it('should not flag clean skills for config impact', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'clean-skill'), 'clean-skill');
        const findings = scanner.findings[0]?.findings || [];
        const configFindings = findings.filter(f => f.cat === 'config-impact');
        assert.equal(configFindings.length, 0, 'Clean skill should have no config impact findings');
    });
});

// ===== 15. PII Exposure Detection (v2.1) =====
describe('PII Exposure Detection (v2.1)', () => {
    it('should detect hardcoded credit card numbers', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'pii-leaky-skill'), 'pii-leaky-skill');
        const findings = scanner.findings[0]?.findings || [];
        assert.ok(findings.some(f => f.id === 'PII_HARDCODED_CC'),
            'Should detect hardcoded credit card number');
    });

    it('should detect hardcoded SSN patterns', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'pii-leaky-skill'), 'pii-leaky-skill');
        const findings = scanner.findings[0]?.findings || [];
        assert.ok(findings.some(f => f.id === 'PII_HARDCODED_SSN'),
            'Should detect hardcoded SSN');
    });

    it('should detect PII logging to console', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'pii-leaky-skill'), 'pii-leaky-skill');
        const findings = scanner.findings[0]?.findings || [];
        assert.ok(findings.some(f => f.id === 'PII_LOG_SENSITIVE'),
            'Should detect PII variable logged to console');
    });

    it('should detect PII sent over network', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'pii-leaky-skill'), 'pii-leaky-skill');
        const findings = scanner.findings[0]?.findings || [];
        assert.ok(findings.some(f => f.id === 'PII_SEND_NETWORK'),
            'Should detect PII variable sent over network');
    });

    it('should detect Shadow AI API calls', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'pii-leaky-skill'), 'pii-leaky-skill');
        const findings = scanner.findings[0]?.findings || [];
        assert.ok(findings.some(f => f.id === 'SHADOW_AI_OPENAI'),
            'Should detect Shadow AI OpenAI API call');
    });

    it('should detect PII collection instructions in docs', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'pii-leaky-skill'), 'pii-leaky-skill');
        const findings = scanner.findings[0]?.findings || [];
        assert.ok(findings.some(f => f.id === 'PII_ASK_ADDRESS'),
            'Should detect PII collection instruction for address');
        assert.ok(findings.some(f => f.id === 'PII_ASK_DOB'),
            'Should detect PII collection instruction for DOB');
        assert.ok(findings.some(f => f.id === 'PII_ASK_GOV_ID'),
            'Should detect PII collection instruction for government ID');
    });

    it('should amplify risk for pii+exfiltration combo', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        const findings = [
            { severity: 'CRITICAL', id: 'PII_HARDCODED_CC', cat: 'pii-exposure' },
            { severity: 'HIGH', id: 'EXFIL_WEBHOOK', cat: 'exfiltration' }
        ];
        const risk = scanner.calculateRisk(findings);
        // CRITICAL(25) + HIGH(15) = 40, then pii+exfil 3x = 120 → capped at 100
        assert.ok(risk >= 100, `PII + exfiltration should max risk, got ${risk}`);
    });

    it('should NOT flag clean skills for PII exposure', () => {
        const scanner = new GuardScanner({ summaryOnly: true });
        scanner.scanSkill(path.join(__dirname, 'fixtures', 'clean-skill'), 'clean-skill');
        const findings = scanner.findings[0]?.findings || [];
        const piiFindings = findings.filter(f => f.cat === 'pii-exposure');
        assert.equal(piiFindings.length, 0, 'Clean skill should have no PII exposure findings');
    });
});
