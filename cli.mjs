#!/usr/bin/env node

import * as p from "@clack/prompts";
import pc from "picocolors";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { execSync, spawn } from "node:child_process";

const GITHUB_REPO = "https://github.com/notdp/codex-ultimate.git";
const CONFIG_DIR = path.join(os.homedir(), ".codex-ultimate");
const CONFIG_PATH = path.join(CONFIG_DIR, "config.json");
const PYTHON_CMD = "codex-pool-maintainer";

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) return null;
  return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
}

function saveConfig(data) {
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(data, null, 2) + "\n", "utf-8");
}

function cancelled() {
  p.cancel("已取消。");
  process.exit(0);
}

function check(v) {
  if (p.isCancel(v)) cancelled();
  return v;
}

function hasPipx() {
  try {
    execSync("pipx --version", { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function hasPythonCmd() {
  try {
    execSync(`${PYTHON_CMD} --help`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

// ── install ───────────────────────────────────────────────────

async function install() {
  p.intro(pc.bgCyan(pc.black(" codex-ultimate install ")));

  // 1. Check pipx
  const s = p.spinner();
  s.start("检查 pipx...");
  if (!hasPipx()) {
    s.stop(pc.red("未找到 pipx"));
    p.log.error("请先安装 pipx：brew install pipx 或 pip install pipx");
    process.exit(1);
  }
  s.stop(pc.green("pipx 已就绪"));

  // 2. Install Python package via pipx
  s.start("通过 pipx 安装 Python 包...");
  try {
    execSync(`pipx install "git+${GITHUB_REPO}" --force`, {
      stdio: "pipe",
      timeout: 120_000,
    });
    s.stop(pc.green("Python 包已安装"));
  } catch (e) {
    s.stop(pc.red("安装失败"));
    p.log.error(e.stderr?.toString() || e.message);
    process.exit(1);
  }

  // 3. Config wizard
  const existing = loadConfig();
  if (existing) {
    const overwrite = check(
      await p.confirm({
        message: "检测到已有配置，是否重新配置？",
        initialValue: false,
      })
    );
    if (!overwrite) {
      p.outro(
        pc.green("安装完成。") +
          " 运行 " +
          pc.bold("npx codex-ultimate run")
      );
      return;
    }
  }

  // ── CPA ──
  const baseUrl = check(
    await p.text({
      message: "CPA 后端地址",
      placeholder: "http://localhost:8317",
      defaultValue: "http://localhost:8317",
    })
  );

  const cpaToken = check(
    await p.text({
      message: "CPA Token",
      validate: (v) => (!v.trim() ? "不能为空" : undefined),
    })
  );

  // ── Email ──
  const emailProvider = check(
    await p.select({
      message: "邮箱提供者",
      options: [
        { value: "mail_gateway", label: "Mail Gateway（推荐）" },
        { value: "icloud", label: "iCloud IMAP 直连" },
      ],
    })
  );

  const emailDomain = check(
    await p.text({
      message: "你的 Catch-All 域名（注册邮箱会随机生成 xxx@此域名）",
      placeholder: "example.com",
      validate: (v) =>
        !v.trim() || !v.includes(".") ? "请输入有效域名" : undefined,
    })
  );

  let mailGateway, icloud;

  if (emailProvider === "mail_gateway") {
    const gwUrl = check(
      await p.text({
        message: "Mail Gateway 地址",
        validate: (v) => (!v.trim() ? "不能为空" : undefined),
      })
    );
    const gwToken = check(
      await p.text({
        message: "Mail Gateway Token",
        validate: (v) => (!v.trim() ? "不能为空" : undefined),
      })
    );
    mailGateway = { base_url: gwUrl, token: gwToken };
  } else {
    const imapUser = check(
      await p.text({
        message: "iCloud 账号",
        placeholder: "you@icloud.com",
        validate: (v) => (!v.trim() ? "不能为空" : undefined),
      })
    );
    const imapPass = check(
      await p.password({
        message: "App 专用密码",
        validate: (v) => (!v.trim() ? "不能为空" : undefined),
      })
    );
    icloud = {
      imap_host: "imap.mail.me.com",
      imap_port: 993,
      username: imapUser,
      app_password: imapPass,
    };
  }

  // ── Run ──
  const proxy = check(
    await p.text({
      message: "代理地址（留空跳过）",
      placeholder: "http://localhost:7890",
      defaultValue: "",
    })
  );

  const minCandidates = check(
    await p.text({
      message: "最小候选账号数（低于此值自动补号）",
      placeholder: "5",
      defaultValue: "5",
      validate: (v) =>
        isNaN(Number(v)) || Number(v) < 0 ? "请输入非负整数" : undefined,
    })
  );

  // ── Save ──
  const config = {
    clean: {
      base_url: baseUrl,
      token: cpaToken,
      target_type: "codex",
      workers: 20,
      delete_workers: 20,
      timeout: 10,
      retries: 1,
    },
    email: {
      provider: emailProvider,
      email_domains: [emailDomain],
    },
    ...(mailGateway ? { mail_gateway: mailGateway } : {}),
    ...(icloud ? { icloud } : {}),
    run: {
      workers: 1,
      proxy: proxy || "",
      ssl_verify: false,
    },
    maintainer: {
      min_candidates: Number(minCandidates),
    },
    oauth: {
      issuer: "https://auth.openai.com",
      client_id: "app_EMoamEEZ73f0CkXaXp7hrann",
      redirect_uri: "http://localhost:1455/auth/callback",
      retry_attempts: 3,
      retry_backoff_base: 2.0,
      retry_backoff_max: 15.0,
    },
    output: { save_local: false },
  };

  saveConfig(config);
  p.outro(
    pc.green("安装完成！") +
      `\n  配置文件: ${pc.dim(CONFIG_PATH)}` +
      `\n  运行 ${pc.bold("npx codex-ultimate run")} 启动维护`
  );
}

// ── run ───────────────────────────────────────────────────────

async function run() {
  if (!fs.existsSync(CONFIG_PATH)) {
    console.error(
      pc.red("未找到配置文件: ") +
        CONFIG_PATH +
        "\n请先运行 " +
        pc.bold("npx codex-ultimate install")
    );
    process.exit(1);
  }

  if (!hasPythonCmd()) {
    console.error(
      pc.red(`未找到 ${PYTHON_CMD} 命令。`) +
        "\n请先运行 " +
        pc.bold("npx codex-ultimate install") +
        " 安装 Python 包"
    );
    process.exit(1);
  }

  const args = ["--config", CONFIG_PATH, ...process.argv.slice(3)];
  const child = spawn(PYTHON_CMD, args, { stdio: "inherit" });
  child.on("close", (code) => process.exit(code ?? 1));
}

// ── entry ─────────────────────────────────────────────────────

const cmd = process.argv[2];

if (cmd === "install") {
  install();
} else if (cmd === "run") {
  run();
} else {
  console.log(`
  ${pc.bold("codex-ultimate")}

  ${pc.cyan("npx codex-ultimate install")}   交互式配置 + 安装 Python 依赖
  ${pc.cyan("npx codex-ultimate run")}       运行账号池维护

  run 后可追加参数，例如:
    npx codex-ultimate run --min-candidates 10
`);
}
