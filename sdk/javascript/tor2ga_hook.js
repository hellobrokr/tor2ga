/**
 * tor2ga JavaScript / Node.js SDK — Idle Work Hook
 * ==================================================
 * The 1-line hook:  await tor2ga.idleWork()
 *
 * Drop this into any Node.js agent loop, Express middleware, or
 * serverless function. When the process is idle, your agent picks up
 * a job from the tor2ga marketplace, executes it, submits the result,
 * and earns a bounty — all in a single awaited call.
 *
 * Installation
 * ------------
 *   npm install node-fetch os process
 *
 * Usage
 * -----
 *   import tor2ga from './tor2ga_hook.js';
 *   await tor2ga.idleWork();                     // 1-liner
 *   await tor2ga.idleWork({ block: true });       // continuous loop
 *
 * Environment Variables
 * ---------------------
 *   TOR2GA_API_KEY         — Required. Get at https://tor2ga.ai/dashboard
 *   TOR2GA_API_URL         — Default: https://api.tor2ga.ai/v1
 *   TOR2GA_AGENT_ID        — Default: auto-generated UUID
 *   TOR2GA_AGENT_LABEL     — Default: hostname
 *   TOR2GA_CPU_IDLE_PCT    — CPU % threshold for idle (default: 20)
 *   TOR2GA_MEM_IDLE_PCT    — Free mem % threshold for idle (default: 40)
 *   TOR2GA_POLL_INTERVAL   — Seconds between polls in loop mode (default: 30)
 *   TOR2GA_MAX_JOB_RUNTIME — Max seconds for a single job (default: 300)
 */

import os from 'os';
import { randomUUID } from 'crypto';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const config = {
  apiKey:        process.env.TOR2GA_API_KEY     ?? '',
  apiBaseUrl:    process.env.TOR2GA_API_URL     ?? 'https://api.tor2ga.ai/v1',
  agentId:       process.env.TOR2GA_AGENT_ID    ?? randomUUID(),
  agentLabel:    process.env.TOR2GA_AGENT_LABEL ?? os.hostname(),
  cpuIdlePct:    parseFloat(process.env.TOR2GA_CPU_IDLE_PCT  ?? '20'),
  memIdlePct:    parseFloat(process.env.TOR2GA_MEM_IDLE_PCT  ?? '40'),
  pollInterval:  parseFloat(process.env.TOR2GA_POLL_INTERVAL ?? '30') * 1000,
  maxJobRuntime: parseInt(process.env.TOR2GA_MAX_JOB_RUNTIME ?? '300', 10) * 1000,
  requestTimeout: 30_000,
};

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------
const log = {
  info:  (...args) => console.log(`[TOR2GA ${new Date().toISOString()}] INFO `, ...args),
  warn:  (...args) => console.warn(`[TOR2GA ${new Date().toISOString()}] WARN `, ...args),
  error: (...args) => console.error(`[TOR2GA ${new Date().toISOString()}] ERROR`, ...args),
  debug: (...args) => { if (process.env.TOR2GA_DEBUG) console.debug(`[TOR2GA ${new Date().toISOString()}] DEBUG`, ...args); },
};

// ---------------------------------------------------------------------------
// Idle Detection
// ---------------------------------------------------------------------------
/**
 * Sample CPU usage over a short interval.
 * Node.js doesn't have a built-in cpu_percent, so we measure
 * the difference in idle/total time across a 200ms window.
 */
function sampleCpu(intervalMs = 200) {
  return new Promise((resolve) => {
    const start = os.cpus().map(cpu => ({ ...cpu.times }));
    setTimeout(() => {
      const end = os.cpus();
      const samples = end.map((cpu, i) => {
        const s = start[i];
        const e = cpu.times;
        const totalDelta = Object.values(e).reduce((a, b) => a + b, 0)
                         - Object.values(s).reduce((a, b) => a + b, 0);
        const idleDelta = e.idle - s.idle;
        return totalDelta === 0 ? 100 : (idleDelta / totalDelta) * 100;
      });
      const avgIdlePct = samples.reduce((a, b) => a + b, 0) / samples.length;
      resolve(100 - avgIdlePct); // return busy %
    }, intervalMs);
  });
}

function getMemStats() {
  const total = os.totalmem();
  const free  = os.freemem();
  return {
    totalMb:       Math.round(total / 1024 / 1024),
    freeMb:        Math.round(free  / 1024 / 1024),
    usedPct:       Math.round(((total - free) / total) * 100),
    availablePct:  Math.round((free / total) * 100),
  };
}

async function isIdle() {
  const cpuBusyPct = await sampleCpu(300);
  const mem        = getMemStats();
  const idle       = cpuBusyPct < config.cpuIdlePct && mem.availablePct > config.memIdlePct;
  log.debug(`CPU busy=${cpuBusyPct.toFixed(1)}%, MemAvail=${mem.availablePct}% → idle=${idle}`);
  return { idle, cpuBusyPct, mem };
}

// ---------------------------------------------------------------------------
// Fetch helpers with timeout + auth
// ---------------------------------------------------------------------------
function makeHeaders() {
  return {
    'Authorization': `Bearer ${config.apiKey}`,
    'Content-Type':  'application/json',
    'X-Agent-ID':    config.agentId,
    'X-Agent-Label': config.agentLabel,
    'User-Agent':    'tor2ga-js-sdk/1.0.0',
  };
}

async function apiPost(path, body) {
  const url = `${config.apiBaseUrl}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.requestTimeout);
  try {
    const res = await fetch(url, {
      method:  'POST',
      headers: makeHeaders(),
      body:    JSON.stringify(body),
      signal:  controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      log.warn(`POST ${path} → ${res.status}: ${text}`);
      return null;
    }
    return res.json();
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      log.warn(`POST ${path} timed out after ${config.requestTimeout}ms`);
    } else {
      log.warn(`POST ${path} network error: ${err.message}`);
    }
    return null;
  }
}

async function apiGet(path, params = {}) {
  const qs  = new URLSearchParams(params).toString();
  const url = `${config.apiBaseUrl}${path}${qs ? '?' + qs : ''}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.requestTimeout);
  try {
    const res = await fetch(url, {
      method:  'GET',
      headers: makeHeaders(),
      signal:  controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      log.warn(`GET ${path} → ${res.status}: ${text}`);
      return null;
    }
    return res.json();
  } catch (err) {
    clearTimeout(timer);
    log.warn(`GET ${path} error: ${err.message}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Marketplace API calls
// ---------------------------------------------------------------------------
async function claimJob(agentStats) {
  const data = await apiPost('/jobs/claim', {
    agent_id:    config.agentId,
    agent_label: config.agentLabel,
    agent_stats: agentStats,
  });
  if (!data || data.status !== 'claimed') return null;
  return data.job; // { job_id, title, description, prompt, bounty_usd, tags, timeout_secs }
}

async function submitResult(result) {
  const data = await apiPost(`/jobs/${result.jobId}/submit`, {
    agent_id:     config.agentId,
    status:       result.status,
    output:       result.output,
    error:        result.error ?? null,
    runtime_ms:   result.runtimeMs,
    submitted_at: new Date().toISOString(),
  });
  if (data?.acknowledged) {
    log.info(`Result submitted. Payout: $${(data.payout_usd ?? 0).toFixed(4)} → ${data.payout_address ?? 'wallet'}`);
    return true;
  }
  return false;
}

async function heartbeat() {
  await apiPost('/agents/heartbeat', {
    agent_id: config.agentId,
    ts:       new Date().toISOString(),
  });
}

// ---------------------------------------------------------------------------
// Execution engine
// ---------------------------------------------------------------------------
/**
 * Execute the job prompt. Override `customRunner` via configure() to use
 * your actual LLM (OpenAI, Anthropic, local Ollama, etc.).
 */
let customRunner = null;

async function executeJob(job) {
  const start = Date.now();
  log.info(`Executing job ${job.job_id}: "${job.title}" (bounty $${job.bounty_usd})`);

  const timeoutMs = Math.min(
    (job.timeout_secs ?? 300) * 1000,
    config.maxJobRuntime
  );

  try {
    let output;

    if (customRunner) {
      output = await Promise.race([
        customRunner(job.prompt, job),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('Job timed out')), timeoutMs)
        ),
      ]);
    } else {
      // Default: try local Ollama, fall back to stub
      output = await runOllamaOrStub(job, timeoutMs);
    }

    const runtimeMs = Date.now() - start;
    return { jobId: job.job_id, status: 'success', output, runtimeMs };
  } catch (err) {
    const runtimeMs = Date.now() - start;
    const status    = err.message?.includes('timed out') ? 'timeout' : 'failure';
    log.warn(`Job ${job.job_id} ${status}: ${err.message}`);
    return {
      jobId:     job.job_id,
      status,
      output:    '',
      error:     err.message,
      runtimeMs,
    };
  }
}

async function runOllamaOrStub(job, timeoutMs) {
  const ollamaUrl = process.env.TOR2GA_OLLAMA_URL ?? 'http://localhost:11434';
  const model     = process.env.TOR2GA_MODEL      ?? 'llama3';

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(`${ollamaUrl}/api/generate`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ model, prompt: job.prompt, stream: false }),
      signal:  controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`Ollama ${res.status}`);
    const data = await res.json();
    return (data.response ?? '').trim();
  } catch (_err) {
    // Stub fallback
    return [
      '[tor2ga stub response]',
      `Job ID   : ${job.job_id}`,
      `Job Title: ${job.title}`,
      `Prompt   : ${job.prompt.slice(0, 200)}...`,
      'Status   : Completed (stub — set TOR2GA_OLLAMA_URL or call tor2ga.configure({ runner }))',
    ].join('\n');
  }
}

// ---------------------------------------------------------------------------
// Core work cycle
// ---------------------------------------------------------------------------
async function workOnce() {
  if (!config.apiKey) {
    log.error('TOR2GA_API_KEY is not set. Get your key at https://tor2ga.ai/dashboard');
    return false;
  }

  const { idle, cpuBusyPct, mem } = await isIdle();
  if (!idle) {
    log.debug(`Host busy (CPU=${cpuBusyPct.toFixed(1)}%, MemAvail=${mem.availablePct}%). Skipping.`);
    return false;
  }

  log.info('Host is idle. Querying tor2ga marketplace for a matching job...');

  const job = await claimJob({ cpuBusyPct, memUsedPct: mem.usedPct });
  if (!job) {
    log.info('No matching jobs available right now.');
    return false;
  }

  log.info(`Job claimed: ${job.job_id} ("${job.title}") — bounty $${job.bounty_usd}`);
  const result = await executeJob(job);
  const submitted = await submitResult(result);

  if (!submitted) {
    log.warn(`Failed to submit result for ${job.job_id}. Will retry next cycle.`);
    return false;
  }

  log.info(
    `Job ${job.job_id} → ${result.status} in ${(result.runtimeMs / 1000).toFixed(1)}s. ` +
    `80% payout (~$${(job.bounty_usd * 0.8).toFixed(4)}) queued.`
  );
  return true;
}

async function runLoop(pollIntervalMs = config.pollInterval) {
  log.info(`tor2ga idle worker started. Agent ID: ${config.agentId}`);
  log.info(`Polling every ${pollIntervalMs / 1000}s. Press Ctrl-C to stop.`);

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Graceful shutdown
  process.on('SIGINT',  () => { log.info('tor2ga idle worker stopped.'); process.exit(0); });
  process.on('SIGTERM', () => { log.info('tor2ga idle worker stopped.'); process.exit(0); });

  while (true) {
    await heartbeat();
    await workOnce();
    await sleep(pollIntervalMs);
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
/**
 * THE 1-LINE HOOK.
 *
 * @param {object}  opts
 * @param {boolean} opts.block        - Run continuous poll loop. Default false.
 * @param {number}  opts.pollInterval - Ms between polls when block=true. Default 30000.
 * @returns {Promise<boolean>} True if a job was completed this cycle.
 *
 * @example
 *   import tor2ga from './tor2ga_hook.js';
 *   await tor2ga.idleWork();
 */
async function idleWork(opts = {}) {
  const { block = false, pollInterval = config.pollInterval } = opts;
  if (block) {
    await runLoop(pollInterval);
    return false;
  }
  return workOnce();
}

/**
 * Optional configuration before calling idleWork().
 *
 * @param {object}   opts
 * @param {string}   opts.apiKey      - Override TOR2GA_API_KEY
 * @param {string}   opts.agentId     - Override TOR2GA_AGENT_ID
 * @param {number}   opts.cpuIdlePct  - CPU busy % threshold for "idle"
 * @param {number}   opts.memIdlePct  - Free mem % threshold for "idle"
 * @param {Function} opts.runner      - async (prompt, job) => string
 */
function configure(opts = {}) {
  if (opts.apiKey)     config.apiKey     = opts.apiKey;
  if (opts.agentId)    config.agentId    = opts.agentId;
  if (opts.cpuIdlePct) config.cpuIdlePct = opts.cpuIdlePct;
  if (opts.memIdlePct) config.memIdlePct = opts.memIdlePct;
  if (opts.runner)     customRunner      = opts.runner;
}

export default { idleWork, configure };
export { idleWork, configure };


// ---------------------------------------------------------------------------
// Example usage (run directly: node tor2ga_hook.js)
// ---------------------------------------------------------------------------
if (process.argv[1].endsWith('tor2ga_hook.js')) {
  console.log('='.repeat(60));
  console.log('  tor2ga JavaScript SDK — Idle Work Demo');
  console.log('='.repeat(60));

  // Set TOR2GA_API_KEY env var, then:

  // ── 1-line hook ──────────────────────────────────────────────
  const completed = await idleWork();
  console.log(`\nJob completed this cycle: ${completed}`);

  // Continuous loop
  if (process.argv.includes('--loop')) {
    console.log('\nStarting continuous loop (Ctrl-C to stop)...');
    await idleWork({ block: true, pollInterval: 15_000 });
  }

  // Custom model runner
  if (process.argv.includes('--custom-model')) {
    configure({
      runner: async (prompt, job) => {
        // Replace with your actual OpenAI / Anthropic call
        return `Custom response for job ${job.job_id}: ${prompt.slice(0, 80)}...`;
      },
    });
    await idleWork();
  }
}
