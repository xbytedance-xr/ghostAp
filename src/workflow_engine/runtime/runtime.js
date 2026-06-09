/**
 * GhostAP Workflow Runtime Harness
 *
 * Communicates with the Python host via JSON-RPC 2.0 over stdin/stdout (NDJSON).
 * Exposes orchestration primitives (agent, parallel, pipeline, phase, log, workflow)
 * as globals to user workflow scripts.
 */

import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// ---------------------------------------------------------------------------
// Path sanitization (strip absolute paths and stack traces from messages)
// ---------------------------------------------------------------------------

function sanitizePath(msg) {
  if (typeof msg !== 'string') return msg || '';
  // Replace absolute paths, keeping only the last filename component
  let cleaned = msg.replace(/(?:\/[\w.\-]+){2,}\/([\w.\-]+)/g, '<path>/$1');
  // Remove Node.js stack trace lines
  cleaned = cleaned.replace(/\n\s+at .+/g, '');
  return cleaned.trim();
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let requestId = 0;
const pendingRequests = new Map(); // id -> { resolve, reject }
let cancelled = false;

// Concurrency cap for parallel(). Matches the Python ThreadPoolExecutor
// size so both sides of the bridge agree on the upper bound. A falsy
// or non-positive value disables the cap (unbounded).
let maxConcurrent = 0;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

class CancelledError extends Error {
  constructor() {
    super('Workflow cancelled by host');
    this.name = 'CancelledError';
  }
}

class JsonRpcError extends Error {
  constructor({ code, message, data } = {}) {
    super(message || 'JSON-RPC error');
    this.name = 'JsonRpcError';
    this.code = code;
    this.data = data;
  }

  toJSON() {
    return { name: this.name, message: this.message, code: this.code, data: this.data };
  }
}

// ---------------------------------------------------------------------------
// Transport (NDJSON over stdin/stdout)
// ---------------------------------------------------------------------------

function send(obj) {
  const line = JSON.stringify(obj) + '\n';
  process.stdout.write(line);
}

function sendNotification(method, params = {}) {
  send({ jsonrpc: '2.0', method, params });
}

function sendRequest(method, params = {}) {
  if (cancelled) {
    return Promise.reject(new CancelledError());
  }

  const id = ++requestId;
  return new Promise((resolve, reject) => {
    pendingRequests.set(id, { resolve, reject });
    send({ jsonrpc: '2.0', id, method, params });
  });
}

// ---------------------------------------------------------------------------
// Incoming message dispatcher
// ---------------------------------------------------------------------------

function handleMessage(line) {
  if (!line.trim()) return;

  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    debugLog(`[runtime] Failed to parse incoming message: ${line}`);
    return;
  }

  // Response to a request we sent
  if (msg.id != null && pendingRequests.has(msg.id)) {
    const { resolve, reject } = pendingRequests.get(msg.id);
    pendingRequests.delete(msg.id);

    if (msg.error) {
      // Preserve JSON-RPC error code/data so the JS orchestration layer
      // (workflow(), agent(), etc.) can respond to structured failure
      // payloads such as `{ kind: "missing_tools", ... }` produced by the
      // Python bridge. Falling back to `new Error(msg.error.message)`
      // would silently drop these attachments.
      reject(
        new JsonRpcError({
          code: msg.error.code,
          message: msg.error.message,
          data: msg.error.data,
        }),
      );
    } else {
      resolve(msg.result);
    }
    return;
  }

  // Notification from Python host (no id)
  if (msg.method && msg.id == null) {
    switch (msg.method) {
      case 'cancel':
        cancelled = true;
        // Reject all pending requests
        for (const [id, { reject }] of pendingRequests) {
          reject(new CancelledError());
        }
        pendingRequests.clear();
        break;

      case 'init':
        // Handled separately in main() via initPromise
        if (initResolve) {
          initResolve(msg.params);
          initResolve = null;
        }
        break;

      default:
        debugLog(`[runtime] Unknown notification: ${msg.method}`);
    }
    return;
  }
}

// ---------------------------------------------------------------------------
// Init synchronisation
// ---------------------------------------------------------------------------

let initResolve = null;

function waitForInit() {
  return new Promise((resolve) => {
    initResolve = resolve;
  });
}

// ---------------------------------------------------------------------------
// Debug logging (stderr only)
// ---------------------------------------------------------------------------

function debugLog(msg) {
  process.stderr.write(`${msg}\n`);
}

// ---------------------------------------------------------------------------
// Meta validation
// ---------------------------------------------------------------------------

function validateMeta(meta) {
  if (!meta || typeof meta !== 'object') {
    throw new Error('Script must export `const meta` object');
  }
  if (typeof meta.name !== 'string' || !meta.name) {
    throw new Error('meta.name must be a non-empty string');
  }
  if (typeof meta.description !== 'string' || !meta.description) {
    throw new Error('meta.description must be a non-empty string');
  }
  if (!Array.isArray(meta.phases) || meta.phases.length === 0) {
    throw new Error('meta.phases must be a non-empty array');
  }
  for (let i = 0; i < meta.phases.length; i++) {
    const p = meta.phases[i];
    if (!p || typeof p.title !== 'string' || typeof p.detail !== 'string') {
      throw new Error(`meta.phases[${i}] must have string "title" and "detail" fields`);
    }
  }
  if (meta.maxConcurrent != null && typeof meta.maxConcurrent !== 'number') {
    throw new Error('meta.maxConcurrent must be a number if provided');
  }
  if (meta.tools != null && !Array.isArray(meta.tools)) {
    throw new Error('meta.tools must be an array if provided');
  }
}

// ---------------------------------------------------------------------------
// Schema validation (shallow structural check)
// ---------------------------------------------------------------------------

function matchesSchema(value, schema) {
  if (!schema || typeof schema !== 'object') return true;
  if (typeof value !== 'object' || value === null) return false;

  for (const key of Object.keys(schema)) {
    if (!(key in value)) return false;
    const expectedType = schema[key];
    if (typeof expectedType === 'string' && typeof value[key] !== expectedType) {
      return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

async function agent(promptOrOpts, opts = {}) {
  if (cancelled) throw new CancelledError();

  // Support both agent("prompt", {opts}) and agent({prompt, ...opts})
  let prompt;
  if (typeof promptOrOpts === 'object' && promptOrOpts !== null) {
    const { prompt: p, ...rest } = promptOrOpts;
    prompt = p;
    opts = rest;
  } else {
    prompt = promptOrOpts;
  }

  const params = {
    prompt,
    ...(opts.tool && { tool: opts.tool }),
    ...(opts.model && { model: opts.model }),
    ...(opts.role && { role: opts.role }),
    ...(opts.schema && { schema: opts.schema }),
    ...(opts.label && { label: opts.label }),
    ...(opts.phase && { phase: opts.phase }),
    ...(opts.timeout && { timeout: opts.timeout }),
  };

  const maxAttempts = opts.schema ? 3 : 1;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    let result;
    try {
      result = await sendRequest('agent_call', params);
    } catch (err) {
      if (err instanceof CancelledError) throw err;
      return { error: err.message };
    }

    // Schema validation with retry
    if (opts.schema) {
      const payload = typeof result === 'object' && result !== null && 'data' in result
        ? result.data
        : result;

      if (matchesSchema(payload, opts.schema)) {
        return payload;
      }

      if (attempt < maxAttempts - 1) {
        debugLog(`[runtime] Schema mismatch on attempt ${attempt + 1}, retrying...`);
        continue;
      }
      // Final attempt failed schema - return what we have
      return payload;
    }

    // No schema - return raw
    if (typeof result === 'object' && result !== null && 'data' in result) {
      return result.data;
    }
    return result;
  }
}

// NOTE: parallel() runs N agent-call descriptors truly concurrently via
// Promise.all when `maxConcurrent` is unset/zero. When a cap is supplied, a
// lightweight in-flight semaphore keeps the JS side aligned with the Python
// ThreadPoolExecutor size so that neither side starves the other. The cap is
// controlled by the host via the init message (`params.max_concurrent`),
// which itself is bounded by HARD_MAX_CONCURRENT on the Python side. pipeline()
// is intentionally NOT parallel across items at the outer level — it iterates
// each item through its stages sequentially — so do not mistake it for an
// unbounded concurrency primitive.
async function parallel(items) {
  if (!Array.isArray(items)) {
    throw new TypeError('parallel() expects an array');
  }

  // When a concurrency cap is configured, bound the number of in-flight
  // promises with a simple semaphore so both sides of the bridge agree
  // on the parallelism upper bound. Without this cap, Promise.all would
  // start N HTTP/subprocess trips at once and starve downstream limits.
  const cap = typeof maxConcurrent === 'number' && maxConcurrent > 0
    ? Math.min(maxConcurrent, items.length || 1)
    : 0;

  if (!cap) {
    return Promise.all(items.map((item) => {
      if (typeof item === 'function') return item();
      if (typeof item === 'object' && item !== null && item.prompt) {
        return agent(item);
      }
      throw new TypeError('parallel() items must be functions or agent-call descriptors');
    }));
  }

  let inFlight = 0;
  let cursor = 0;
  const results = new Array(items.length);
  let rejector = null;

  return new Promise((resolve, reject) => {
    rejector = reject;

    function launch(index) {
      if (inFlight >= cap) return;
      if (index >= items.length) return;
      const item = items[index];
      let p;
      if (typeof item === 'function') {
        try {
          p = item();
        } catch (err) {
          rejector(err);
          return;
        }
      } else if (typeof item === 'object' && item !== null && item.prompt) {
        p = agent(item);
      } else {
        rejector(new TypeError('parallel() items must be functions or agent-call descriptors'));
        return;
      }
      inFlight += 1;
      Promise.resolve(p).then((value) => {
        results[index] = value;
        inFlight -= 1;
        if (cursor < items.length) launch(cursor++);
        else if (inFlight === 0) resolve(results);
      }, (err) => {
        rejector(err);
      });
    }

    // Seed up to `cap` in-flight tasks.
    const initial = Math.min(cap, items.length);
    for (let i = 0; i < initial; i++) {
      cursor = i + 1;
      launch(i);
    }
    // All items smaller than cap; in-flight completion will resolve.
    if (cursor >= items.length && inFlight === 0) resolve(results);
  });
}

async function pipeline(items, ...args) {
  // pipeline(items, ...stages) — parallel/map: items run concurrently via Promise.all;
  // each item flows through stages sequentially (map-reduce style).
  // NOTE: this is NOT an outer-level concurrent primitive for stage fan-out —
  // use parallel() if you need heterogeneous tasks fanned out concurrently.
  // For strictly sequential item processing use sequence(...) / serialPipeline(...).
  if (!Array.isArray(items)) {
    throw new TypeError('pipeline() expects an array of items as first argument');
  }

  // Extract options from last argument if it's an object (not a function)
  let stages = args;
  let options = {};
  if (args.length > 0 && typeof args[args.length - 1] === 'object' && args[args.length - 1] !== null) {
    options = args[args.length - 1];
    stages = args.slice(0, -1);
  }

  const continueOnFailure = options.continueOnFailure || options.continue_on_failure || false;

  const results = await Promise.all(
    items.map(async (item) => {
      let current = item;
      for (let i = 0; i < stages.length; i++) {
        const stage = stages[i];
        try {
          current = await stage(current);
        } catch (err) {
          if (continueOnFailure) {
            debugLog(`[runtime] Pipeline stage ${i} failed for item, continuing: ${err.message}`);
            return {
              error: sanitizePath(err.message),
              failedAtStage: i,
              partialResult: current,
            };
          }
          throw err;
        }
      }
      return current;
    }),
  );

  return results;
}

function phase(title) {
  sendNotification('phase', { title });
}

function log(msg) {
  sendNotification('log', { message: String(msg) });
}

async function workflow(nameOrOpts, args = {}) {
  if (cancelled) throw new CancelledError();

  // NOTE: Only `name` (template identifier) is accepted. Direct script_path
  // is rejected on the Python side for security — all sub-workflow resolution
  // must go through the templates module validate_template_name +
  // resolve_template_path to enforce scoping (user/project/allowlisted global
  // /builtin) and prevent path traversal.
  let name;
  if (typeof nameOrOpts === 'object' && nameOrOpts !== null) {
    if ('script_path' in nameOrOpts || 'scriptPath' in nameOrOpts) {
      throw new Error(
        'workflow(): script_path / scriptPath is not allowed. ' +
        "Use workflow('<template_name>') or workflow({ name: '<template_name>' })."
      );
    }
    name = nameOrOpts.name;
    args = nameOrOpts.args || args;
  } else {
    name = nameOrOpts;
  }

  let result;
  try {
    result = await sendRequest('workflow_call', { name, args });
  } catch (err) {
    // Propagate JsonRpcError (with code/data) untouched so callers can
    // inspect err.code / err.data.kind (e.g. "missing_tools") and decide
    // whether to fail-fast, skip, or surface to the confirmation card.
    if (err instanceof JsonRpcError) throw err;
    // Otherwise, re-wrap as a plain JsonRpcError so callers can distinguish
    // bridge-produced errors from programming errors in the script.
    throw new JsonRpcError({ code: -32000, message: err.message });
  }

  if (typeof result === 'object' && result !== null && 'data' in result) {
    return result.data;
  }
  return result;
}

// ---------------------------------------------------------------------------
// Expose globals
// ---------------------------------------------------------------------------

function installGlobals() {
  globalThis.agent = agent;
  globalThis.parallel = parallel;
  globalThis.pipeline = pipeline;
  globalThis.phase = phase;
  globalThis.log = log;
  globalThis.workflow = workflow;
}

// Wrap every function we hand into the vm sandbox so that attacker code
// cannot walk through ``fn.constructor.constructor(...)`` to reach the host
// Function constructor. Each wrapper is a plain closure with ``null``
// prototype; we additionally freeze the wrapper so its properties are
// immutable from within the sandbox.
function sandboxWrapHostFn(fn) {
  // eslint-disable-next-line func-names
  const wrapper = function (...args) { return fn(...args); };
  Object.setPrototypeOf(wrapper, null);
  Object.freeze(wrapper);
  return wrapper;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const scriptPath = process.argv[2];
  if (!scriptPath) {
    process.stderr.write('[runtime] Usage: node runtime.js <script-path>\n');
    process.exit(1);
  }

  // Setup readline for incoming NDJSON
  const rl = createInterface({ input: process.stdin, terminal: false });
  rl.on('line', handleMessage);

  // Signal ready
  sendNotification('ready', {});

  // Wait for init from Python host
  const initParams = await waitForInit();
  maxConcurrent = Number(initParams.max_concurrent) || 0;
  if (maxConcurrent < 0) maxConcurrent = 0;
  const workflowArgs = initParams.args || {};

  // Read user script source
  const absolutePath = resolve(scriptPath);
  let source;
  try {
    source = readFileSync(absolutePath, 'utf-8');
  } catch (err) {
    sendNotification('error', {
      message: sanitizePath(`Failed to read script: ${err.message}`),
      stack: '',
    });
    process.exit(1);
  }

  // Create sandboxed context with only orchestration primitives.
  // Every host-side function is wrapped in a sandboxed closure before being
  // handed to the vm context so script code cannot reach
  // ``agent.constructor.constructor("return process")()`` or similar
  // prototype-chain escapes.
  const sandboxGlobals = {
    agent: sandboxWrapHostFn(agent),
    parallel: sandboxWrapHostFn(parallel),
    pipeline: sandboxWrapHostFn(pipeline),
    phase: sandboxWrapHostFn(phase),
    log: sandboxWrapHostFn(log),
    workflow: sandboxWrapHostFn(workflow),
    CancelledError,
    workflowArgs,
    // Safe standard built-ins
    console: { log: sandboxWrapHostFn((...a) => debugLog(a.join(' '))),
                error: sandboxWrapHostFn((...a) => debugLog(a.join(' '))) },
    setTimeout,
    clearTimeout,
    Promise,
    JSON,
    Array,
    Object,
    String,
    Number,
    Boolean,
    Map,
    Set,
    Date,
    Math,
    Error,
    TypeError,
    RangeError,
    RegExp,
    Symbol,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
    encodeURIComponent,
    decodeURIComponent,
    encodeURI,
    decodeURI,
    undefined,
    NaN,
    Infinity,
  };

  const context = vm.createContext(sandboxGlobals, {
    name: 'workflow-sandbox',
    codeGeneration: { strings: false, wasm: false },
  });

  // Harden the prototypes inside the sandbox so attacker code cannot patch
  // ``Object.prototype`` / ``Function.prototype`` with getters/setters that
  // leak back into the host. These calls only affect objects inside the
  // vm context; the host side is untouched.
  try {
    vm.runInContext('Object.freeze(Object.prototype);' +
                    'Object.freeze(Function.prototype);' +
                    'Object.freeze(Array.prototype);' +
                    'Object.freeze(String.prototype);' +
                    'Object.freeze(Number.prototype);' +
                    'Object.freeze(Boolean.prototype);' +
                    'Object.freeze(Promise.prototype);', context, {
      filename: 'sandbox-harden.js',
    });
  } catch (err) {
    debugLog(`[runtime] sandbox prototype hardening skipped: ${err.message}`);
  }

  // Load and execute using vm.SourceTextModule (sandboxed ESM)
  let mod;
  try {
    mod = new vm.SourceTextModule(source, {
      context,
      identifier: `file://${absolutePath}`,
    });

    // Link: deny all imports (sandbox enforcement)
    await mod.link((specifier) => {
      throw new Error(`Imports are not allowed in workflow scripts: "${specifier}"`);
    });

    await mod.evaluate();
  } catch (err) {
    sendNotification('error', {
      message: sanitizePath(`Failed to load script: ${err.message}`),
      stack: '',
    });
    process.exit(1);
  }

  // Extract exports from the module namespace
  const ns = mod.namespace;

  // Validate meta
  const meta = ns.meta;
  try {
    validateMeta(meta);
  } catch (err) {
    sendNotification('error', {
      message: sanitizePath(`Meta validation failed: ${err.message}`),
      stack: '',
    });
    process.exit(1);
  }

  // Execute workflow
  try {
    let result;
    if (typeof ns.default === 'function') {
      result = await ns.default(workflowArgs);
    } else if (typeof ns.run === 'function') {
      result = await ns.run(workflowArgs);
    } else {
      // Script has no explicit entry — meta is the payload
      result = { meta };
    }

    sendNotification('done', { result: result ?? null });
  } catch (err) {
    if (err instanceof CancelledError) {
      sendNotification('error', {
        message: sanitizePath(err.message),
        stack: '',
      });
    } else {
      sendNotification('error', {
        message: sanitizePath(err.message || String(err)),
        stack: '',
      });
    }
    process.exit(1);
  }

  // Clean shutdown
  rl.close();
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Unhandled rejection safety net
// ---------------------------------------------------------------------------

process.on('unhandledRejection', (reason) => {
  const message = reason instanceof Error ? reason.message : String(reason);
  sendNotification('error', { message: sanitizePath(message), stack: '' });
  process.exit(1);
});

process.on('uncaughtException', (err) => {
  sendNotification('error', { message: sanitizePath(err.message), stack: '' });
  process.exit(1);
});

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

main();
