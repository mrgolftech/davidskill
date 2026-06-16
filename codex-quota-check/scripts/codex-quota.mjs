#!/usr/bin/env node
/**
 * codex-quota.mjs — Codex 剩余额度查询
 *
 * 通过 codex app-server JSON-RPC 获取额度，输出 JSON 供 Hermes agent 解析展示。
 *
 * Usage: node /path/to/codex-quota.mjs
 * Output: single JSON object to stdout
 */

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const CODEX = "/root/.hermes/node/bin/codex";

function main() {
  return new Promise((resolve, reject) => {
    const child = spawn(CODEX, ["app-server", "--listen", "stdio://"], {
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stderrBuf = "";
    child.stderr.on("data", (d) => { stderrBuf += d.toString(); });

    const rl = createInterface({ input: child.stdout });
    let nextId = 1;
    const pending = new Map();
    let timedOut = false;

    const killTimer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      reject(new Error("TIMEOUT: codex app-server did not respond within 12s"));
    }, 12000);

    rl.on("line", (line) => {
      if (timedOut) return;
      let msg;
      try { msg = JSON.parse(line); } catch { return; }
      if (msg.id !== undefined && pending.has(msg.id)) {
        const { resolve: res, reject: rej, timer } = pending.get(msg.id);
        clearTimeout(timer);
        pending.delete(msg.id);
        if (msg.error) rej(new Error(JSON.stringify(msg.error)));
        else res(msg.result);
      }
    });

    child.on("error", (err) => {
      clearTimeout(killTimer);
      reject(new Error(`spawn error: ${err.message}`));
    });

    child.on("exit", (code) => {
      clearTimeout(killTimer);
      if (!timedOut && pending.size > 0) {
        reject(new Error(`app-server exited (code ${code}) with pending requests`));
      }
    });

    function request(method, params) {
      const id = nextId++;
      const payload = { id, method };
      if (params !== undefined) payload.params = params;
      child.stdin.write(JSON.stringify(payload) + "\n");
      return new Promise((resolveReq, rejectReq) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          rejectReq(new Error(`${method} timeout after 8s`));
        }, 8000);
        pending.set(id, { resolve: resolveReq, reject: rejectReq, timer });
      });
    }

    function notify(method, params) {
      const payload = { method };
      if (params !== undefined) payload.params = params;
      child.stdin.write(JSON.stringify(payload) + "\n");
    }

    function clampPercent(v) {
      if (typeof v !== "number") return null;
      return Math.max(0, Math.min(100, Number(v.toFixed(1))));
    }

    function remainingPercent(bucket) {
      if (!bucket || typeof bucket.usedPercent !== "number") return null;
      return clampPercent(100 - bucket.usedPercent);
    }

    function bucketName(mins) {
      if (mins === 300) return "5h";
      if (mins === 10080) return "weekly";
      if (typeof mins === "number") return `${mins}min`;
      return "unknown";
    }

    (async () => {
      try {
        await request("initialize", {
          clientInfo: { name: "codex_quota_check", title: "Codex Quota Check", version: "0.1.0" },
        });
        notify("initialized");

        const result = await request("account/rateLimits/read");
        const snapshot = result?.rateLimitsByLimitId?.codex ?? result?.rateLimits;

        const output = {
          planType: null,
          rateLimitReachedType: null,
          credits: null,
          buckets: [],
        };

        if (snapshot) {
          const buckets = [
            { key: "primary", value: snapshot.primary },
            { key: "secondary", value: snapshot.secondary },
          ].filter((x) => x.value);

          for (const { key, value } of buckets) {
            output.buckets.push({
              name: bucketName(value.windowDurationMins),
              key,
              usedPercent: value.usedPercent,
              remainingPercent: remainingPercent(value),
              windowDurationMins: value.windowDurationMins,
              resetsAt: value.resetsAt,
              resetsAtISO: value.resetsAt ? new Date(value.resetsAt * 1000).toISOString() : null,
            });
          }

          output.rateLimitReachedType = snapshot.rateLimitReachedType ?? null;
        }

        output.planType = snapshot?.planType ?? result?.planType ?? null;
        output.credits = snapshot?.credits ?? result?.credits ?? null;

        resolve(output);
      } catch (err) {
        reject(err);
      } finally {
        child.kill("SIGTERM");
      }
    })();
  });
}

main()
  .then((data) => {
    console.log(JSON.stringify(data));
    process.exit(0);
  })
  .catch((err) => {
    console.error(JSON.stringify({ error: err.message }));
    process.exit(1);
  });
