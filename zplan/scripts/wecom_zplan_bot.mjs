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

function runZplanReply(userText) {
  const py = join(ROOT, ".venv/bin/python");
  return new Promise((resolve, reject) => {
    const proc = spawn(
      py,
      ["openclaw_bridge.py", "wechat-reply", "--text", userText],
      { cwd: ROOT, env: { ...process.env } },
    );
    let out = "";
    let err = "";
    proc.stdout.on("data", (d) => {
      out += d;
    });
    proc.stderr.on("data", (d) => {
      err += d;
    });
    proc.on("close", (code) => {
      const blob = (out || err).trim();
      if (!blob) {
        reject(new Error(`zplan exit ${code}, no output`));
        return;
      }
      try {
        const lastLine = blob.split("\n").filter(Boolean).pop();
        const data = JSON.parse(lastLine);
        if (!data.ok) {
          reject(new Error(data.error?.message || JSON.stringify(data.error) || "zplan failed"));
          return;
        }
        resolve(String(data.reply_text || data.reply_markdown || "（无回复内容）").slice(0, 1800));
      } catch (e) {
        reject(new Error(`parse zplan json failed: ${blob.slice(0, 300)}`));
      }
    });
  });
}

async function main() {
  loadEnv(join(ROOT, ".env"));
  const botId = process.env.WECOM_BOT_ID;
  const secret = process.env.WECOM_BOT_SECRET;
  if (!botId || !secret) {
    console.error("请在 zplan/.env 配置 WECOM_BOT_ID 与 WECOM_BOT_SECRET");
    process.exit(1);
  }

  let AiBot;
  try {
    AiBot = await import(SDK);
  } catch (e) {
    console.error("无法加载 @wecom/aibot-node-sdk，请先安装企微 OpenClaw 插件。");
    console.error(e?.message || e);
    process.exit(1);
  }

  const { WSClient, generateReqId } = AiBot.default || AiBot;
  const ws = new WSClient({ botId, secret });

  ws.on("authenticated", () => {
    console.log("[wecom-zplan] 已连接企微，等待消息…（@机器人 发「帮助」测试）");
  });

  ws.on("message.text", async (frame) => {
    const raw = frame?.body?.text?.content || "";
    const query = stripMention(raw);
    if (!query) return;
    console.log(`[wecom-zplan] 收到: ${query.slice(0, 80)}`);
    const streamId = generateReqId("stream");
    try {
      await ws.replyStream(frame, streamId, "正在检索资讯，请稍候…", false);
      const reply = await runZplanReply(query);
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
    }
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
