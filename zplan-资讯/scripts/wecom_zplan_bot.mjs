#!/usr/bin/env node
/**
 * 企微智能机器人 ↔ Z-Plan 直连（不经过 OpenClaw Agent）。
 * 与 OpenClaw 的 wecom 渠道不能同时占用同一 Bot ID，运行前请 stop gateway。
 */
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SDK = join(
  process.env.HOME || "",
  ".openclaw/extensions/wecom-openclaw-plugin/node_modules/@wecom/aibot-node-sdk/dist/index.esm.js",
);

function loadEnv(envPath) {
  const text = readFileSync(envPath, "utf8");
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    let val = trimmed.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    process.env[key] = val;
  }
}

function stripMention(text) {
  return (text || "").replace(/^@\S+\s*/u, "").trim() || (text || "").trim();
}

function parseZplanJson(blob) {
  const trimmed = blob.trim();
  if (!trimmed) throw new Error("empty output");
  try {
    return JSON.parse(trimmed);
  } catch {
    /* openclaw_bridge 使用 indent=2，整段 stdout 才是合法 JSON */
  }
  const start = trimmed.indexOf("{");
  const end = trimmed.lastIndexOf("}");
  if (start >= 0 && end > start) {
    return JSON.parse(trimmed.slice(start, end + 1));
  }
  throw new Error("no json object in output");
}

function runZplanReply(userText, childEnv) {
  const py = join(ROOT, ".venv/bin/python");
  const timeoutMs = Number(process.env.WECOM_REPLY_TIMEOUT_MS || 120_000);
  return new Promise((resolve, reject) => {
    const proc = spawn(
      py,
      ["openclaw_bridge.py", "wechat-reply", "--text", userText],
      { cwd: ROOT, env: childEnv },
    );
    let out = "";
    let err = "";
    let settled = false;
    const finish = (fn, val) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(val);
    };
    const timer = setTimeout(() => {
      proc.kill("SIGTERM");
      setTimeout(() => proc.kill("SIGKILL"), 3000);
      finish(
        reject,
        new Error(`处理超时（>${Math.round(timeoutMs / 1000)}s），请稍后重试「帮助」确认在线`),
      );
    }, timeoutMs);
    proc.stdout.on("data", (d) => {
      out += d;
    });
    proc.stderr.on("data", (d) => {
      err += d;
    });
    proc.on("close", (code) => {
      const blob = (out || err).trim();
      if (!blob) {
        finish(reject, new Error(`zplan exit ${code}, no output`));
        return;
      }
      try {
        const data = parseZplanJson(out || err);
        if (!data.ok) {
          finish(
            reject,
            new Error(data.error?.message || JSON.stringify(data.error) || "zplan failed"),
          );
          return;
        }
        finish(
          resolve,
          String(data.reply_text || data.reply_markdown || "（无回复内容）").slice(0, 1800),
        );
      } catch (e) {
        finish(reject, new Error(`parse zplan json failed: ${(out || err).slice(0, 300)}`));
      }
    });
  });
}

function resolveOutboundProxy() {
  const py = join(ROOT, ".venv/bin/python");
  return new Promise((resolve) => {
    const proc = spawn(
      py,
      [
        "-c",
        "from outbound_http import resolve_effective_proxy_url; u,_=resolve_effective_proxy_url(); print(u or '')",
      ],
      { cwd: ROOT, env: { ...process.env } },
    );
    let out = "";
    proc.stdout.on("data", (d) => {
      out += d;
    });
    proc.on("close", () => resolve(out.trim()));
    proc.on("error", () => resolve(""));
  });
}

function buildChildEnv() {
  const env = { ...process.env };
  if (!env.USE_SYSTEM_PROXY) env.USE_SYSTEM_PROXY = "true";
  if (env.HTTP_PROXY) env.AKSHARE_USE_SYSTEM_PROXY = "true";
  return env;
}

function makeStreamId() {
  return `stream_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

async function main() {
  loadEnv(join(ROOT, ".env"));
  const botId = process.env.WECOM_BOT_ID;
  const secret = process.env.WECOM_BOT_SECRET;
  if (!botId || !secret) {
    console.error("请在 zplan-资讯/.env 配置 WECOM_BOT_ID 与 WECOM_BOT_SECRET");
    process.exit(1);
  }

  let sdk;
  try {
    sdk = await import(SDK);
  } catch (e) {
    console.error("无法加载 @wecom/aibot-node-sdk，请先安装企微 OpenClaw 插件。");
    console.error(e?.message || e);
    process.exit(1);
  }

  const WSClient = sdk.WSClient;
  const generateReqId = typeof sdk.generateReqId === "function" ? sdk.generateReqId : makeStreamId;
  if (!WSClient) {
    console.error("SDK 缺少 WSClient 导出");
    process.exit(1);
  }
  const ws = new WSClient({ botId, secret });
  const inFlight = new Set();
  const childEnv = buildChildEnv();

  const proxy = await resolveOutboundProxy();
  if (proxy) {
    childEnv.HTTP_PROXY = proxy;
    childEnv.HTTPS_PROXY = proxy;
    childEnv.AKSHARE_USE_SYSTEM_PROXY = "true";
    console.log(`[wecom-zplan] 出站代理 ${proxy}`);
  } else {
    console.warn("[wecom-zplan] 未检测到系统代理，Gemini/外网可能较慢");
  }

  ws.on("authenticated", () => {
    console.log("[wecom-zplan] 已连接企微，等待消息…（@机器人 发「帮助」测试）");
  });

  ws.on("message.text", (frame) => {
    const raw = frame?.body?.text?.content || "";
    const query = stripMention(raw);
    if (!query) return;

    const msgId = frame?.body?.msgid || frame?.header?.req_id || makeStreamId();
    if (inFlight.has(msgId)) return;
    inFlight.add(msgId);

    console.log(`[wecom-zplan] 收到: ${query.slice(0, 80)} (并行 ${inFlight.size})`);

    void (async () => {
      const streamId = generateReqId("stream");
      const isFast = /^(帮助|help|\?|？|最新|latest|列表|topics?|7天|7d|一周)$/iu.test(query);
      try {
        if (!isFast) {
          await ws.replyStream(frame, streamId, "正在检索资讯，请稍候…", false);
        }
        const reply = await runZplanReply(query, childEnv);
        await ws.replyStream(frame, streamId, reply, true);
        console.log(`[wecom-zplan] 已回复 ${reply.length} 字`);
      } catch (e) {
        const msg = `处理失败：${e?.message || e}`;
        console.error(`[wecom-zplan] ${msg}`);
        try {
          await ws.replyStream(frame, streamId, msg.slice(0, 500), true);
        } catch {
          /* ignore */
        }
      } finally {
        inFlight.delete(msgId);
      }
    })();
  });

  ws.connect();
  process.on("SIGINT", () => {
    ws.disconnect();
    process.exit(0);
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
