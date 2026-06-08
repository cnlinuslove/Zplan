#!/usr/bin/env node
/**
 * 企微智能机器人 ↔ Z-Plan 直连（不经过 OpenClaw Agent）。
 * 与 OpenClaw 的 wecom 渠道不能同时占用同一 Bot ID，运行前请 stop gateway。
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
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

/**
 * 调用 Python openclaw_bridge.py wechat-reply
 * @returns {{text: string, templateCard: object|null}}
 */
function runZplanReply(userText, childEnv, { userId, chatId, mentioned } = {}) {
  const py = join(ROOT, ".venv/bin/python");
  const timeoutMs = Number(process.env.WECOM_REPLY_TIMEOUT_MS || 120_000);
  const args = ["openclaw_bridge.py", "wechat-reply", "--text", userText, "--channel", "wecom_bot"];
  if (userId) args.push("--user-id", userId);
  if (chatId) args.push("--chat-id", chatId);
  if (mentioned) args.push("--mentioned");
  return new Promise((resolve, reject) => {
    const proc = spawn(
      py,
      args,
      { cwd: ROOT, env: childEnv },
    );
    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");
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
      if (err.trim()) {
        console.error(`[wecom-zplan] stderr (exit ${code}):`, err.slice(0, 500));
      }
      const blob = out.trim();
      if (!blob) {
        finish(reject, new Error(`zplan exit ${code}, no output${err ? `; stderr: ${err.slice(0, 200)}` : ""}`));
        return;
      }
      try {
        const data = parseZplanJson(out);
        if (!data.ok) {
          finish(
            reject,
            new Error(data.error?.message || JSON.stringify(data.error) || "zplan failed"),
          );
          return;
        }
        finish(resolve, {
          // 优先使用更长的 reply（markdown 支持 4096 字节，可嵌入可点击链接）
          text: (() => {
            const rt = String(data.reply_text || "");
            const rm = String(data.reply_markdown || "");
            const preferred = rm.length > rt.length ? rm : rt;
            const maxLen = rm.length > rt.length ? 3600 : 1800;
            return (preferred || "（无回复内容）").slice(0, maxLen);
          })(),
          templateCard: data.reply_template_card || null,
          pdfPath: data.pdf_path || null,
        });
      } catch (e) {
        const outLen = out.length;
        const head = out.slice(0, 200);
        const tail = out.slice(-200);
        console.error(
          `[wecom-zplan] JSON parse error: ${e.message}`,
          `| out.length=${outLen} | head=[${head}] | tail=[${tail}]`,
        );
        finish(
          reject,
          new Error(
            `parse zplan json failed (len=${outLen}, ${e.message.slice(0, 80)}): ${out.slice(0, 200)}`,
          ),
        );
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

// ── 模板卡片按钮事件处理 ──────────────────────────────

/**
 * 解析按钮 event_key → {action, tsCode, name}
 * key 格式: "analyze|300058|蓝色光标" / "news|300058|蓝色光标" / "picklist" / "picklist_analyze"
 */
function parseButtonKey(eventKey) {
  if (!eventKey) return null;
  const parts = eventKey.split("|");
  return { action: parts[0], tsCode: parts[1] || null, name: parts[2] || null };
}

/**
 * 处理按钮点击：调用 Python 后端并流式返回结果。
 */
async function handleButtonClick(ws, frame, eventKey, streamId, childEnv) {
  const parsed = parseButtonKey(eventKey);
  if (!parsed) return;

  const { action, tsCode, name } = parsed;
  const userId = frame?.body?.from?.userid || frame?.body?.from_userid || "";
  const chatId = frame?.body?.chatid || "";
  const ctx = { userId, chatId };

  if (action === "analyze" && tsCode) {
    await ws.replyStream(frame, streamId, `正在分析 ${name || tsCode}，请稍候…`, false);
    try {
      const { text } = await runZplanReply(`分析 ${name || tsCode}`, childEnv, { ...ctx, mentioned: true });
      await ws.replyStream(frame, streamId, text, true);
      console.log(`[wecom-zplan] 按钮-分析 ${tsCode} 完成 (${text.length} 字)`);
    } catch (e) {
      const msg = `分析失败：${e?.message || e}`;
      await ws.replyStream(frame, streamId, msg.slice(0, 500), true);
    }
  } else if (action === "news" && tsCode) {
    await ws.replyStream(frame, streamId, `正在查询 ${name || tsCode} 最新快讯…`, false);
    try {
      const { text } = await runZplanReply(`${tsCode} 新闻`, childEnv, { ...ctx, mentioned: true });
      await ws.replyStream(frame, streamId, text, true);
      console.log(`[wecom-zplan] 按钮-快讯 ${tsCode} 完成 (${text.length} 字)`);
    } catch (e) {
      const msg = `查询失败：${e?.message || e}`;
      await ws.replyStream(frame, streamId, msg.slice(0, 500), true);
    }
  } else if (action === "picklist") {
    await ws.replyStream(frame, streamId, "正在生成选股清单…", false);
    try {
      const { text } = await runZplanReply("选股清单", childEnv, { ...ctx, mentioned: true });
      await ws.replyStream(frame, streamId, text, true);
      console.log(`[wecom-zplan] 按钮-选股清单 完成 (${text.length} 字)`);
    } catch (e) {
      const msg = `查询失败：${e?.message || e}`;
      await ws.replyStream(frame, streamId, msg.slice(0, 500), true);
    }
  } else if (action === "picklist_analyze") {
    await ws.replyStream(
      frame, streamId,
      "请直接发送股票名称或代码，例如「爱普股份」或「603020」",
      true,
    );
  }
}

// ── 主入口 ─────────────────────────────────────────────

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
  // WebSocket 僵死连接看门狗：超过 2 小时无消息则强制重连
  let lastActivityTs = Date.now();
  const WATCHDOG_INTERVAL_MS = 5 * 60 * 1000; // 每 5 分钟检查一次
  const WATCHDOG_IDLE_MAX_MS = 2 * 60 * 60 * 1000; // 超过 2 小时无消息视为僵死
  function bumpActivity() { lastActivityTs = Date.now(); }

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
    bumpActivity();
    console.log("[wecom-zplan] 已连接企微，等待消息…（@机器人 发「帮助」测试）");
  });

  // ── 文本消息 ──
  ws.on("message.text", (frame) => {
    bumpActivity();
    const raw = frame?.body?.text?.content || "";
    const query = stripMention(raw);
    if (!query) return;

    const msgId = frame?.body?.msgid || frame?.header?.req_id || makeStreamId();
    if (inFlight.has(msgId)) return;
    inFlight.add(msgId);

    const userId = frame?.body?.from?.userid || frame?.body?.from_userid || "";
    const chatId = frame?.body?.chatid || "";

    console.log(`[wecom-zplan] 收到: ${query.slice(0, 80)} (并行 ${inFlight.size})`);

    void (async () => {
      const streamId = generateReqId("stream");
      const isFast = /^(帮助|help|\?|？|最新|latest|列表|topics?|7天|7d|一周)$/iu.test(query);
      try {
        if (!isFast) {
          await ws.replyStream(frame, streamId, "正在检索资讯，请稍候…", false);
        }
        // 智能机器人仅接收 @ 消息，mentioned 始终为 true
        const { text, templateCard } = await runZplanReply(query, childEnv, { userId, chatId, mentioned: true });

        if (templateCard && ws.replyStreamWithCard) {
          await ws.replyStreamWithCard(frame, streamId, text, true, { templateCard });
          console.log(`[wecom-zplan] 已回复 ${text.length} 字 + 卡片`);
        } else {
          await ws.replyStream(frame, streamId, text, true);
          console.log(`[wecom-zplan] 已回复 ${text.length} 字`);
        }
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

  // ── 模板卡片按钮点击 ──
  ws.on("template_card_event", (frame) => {
    bumpActivity();


    const eventKey = frame?.body?.event?.template_card_event?.event_key;
    if (!eventKey) return;

    console.log(`[wecom-zplan] 按钮点击: ${eventKey}`);

    const streamId = generateReqId("stream");
    void handleButtonClick(ws, frame, eventKey, streamId, childEnv);
  });

  ws.connect();

  // ── WebSocket 僵死看门狗：定期检查连接活性 ──
  const watchdog = setInterval(() => {
    const idle = Date.now() - lastActivityTs;
    if (idle > WATCHDOG_IDLE_MAX_MS) {
      const idleH = (idle / 3600000).toFixed(1);
      console.warn(`[wecom-zplan] ⚠️ ${idleH}h 无消息，可能僵死连接，强制重连…`);
      lastActivityTs = Date.now(); // 防重入
      try { ws.disconnect(); } catch { /* ignore */ }
      setTimeout(() => {
        try { ws.connect(); } catch { /* ignore */ }
      }, 2000);
    }
  }, WATCHDOG_INTERVAL_MS);
  watchdog.unref(); // 不阻止进程退出

  process.on("SIGINT", () => {
    clearInterval(watchdog);
    ws.disconnect();
    process.exit(0);
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
