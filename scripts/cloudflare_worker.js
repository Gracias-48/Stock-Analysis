/**
 * Cloudflare Worker: Telegram Webhook to GitHub Actions Dispatcher
 * Project: IDX Stock Intelligence - Triple-Engine Multi-Agent System
 *
 * Responsibilities:
 * 1. Listens for incoming webhook updates from Telegram Bot.
 * 2. Catches commands: `/analyze <TICKER>`, `/analisis <TICKER>`, `/stock <TICKER>`.
 * 3. Sends an immediate acknowledgement message back to the Telegram chat.
 * 4. Triggers the GitHub Actions workflow `deep_agent.yml` via GitHub REST API.
 *
 * Required Cloudflare Worker Environment Variables / Secrets:
 * - BOT_TOKEN: Telegram Bot Token (e.g. "123456789:ABCdefGhIJKlmNoPQRstuVWXyz")
 * - GITHUB_PAT: GitHub Personal Access Token (classic with `repo` or fine-grained with `actions:write`)
 * - GITHUB_REPO: "Owner/Repo" format (e.g. "Gracias-48/Stock-Analysis")
 * - GITHUB_REF: Branch name (Default: "main")
 * - ALLOWED_CHAT_ID: (Optional) Limit execution to your personal Telegram Chat ID for security
 * - SECRET_TOKEN: (Optional) Secret token configured in Telegram webhook for request verification
 */

export default {
  async fetch(request, env, ctx) {
    // Only accept POST requests from Telegram
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ status: "ok", message: "IDX Stock Agent Webhook Active" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Optional secret token verification
    if (env.SECRET_TOKEN) {
      const headerToken = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (headerToken !== env.SECRET_TOKEN) {
        return new Response("Unauthorized", { status: 403 });
      }
    }

    try {
      const update = await request.json();
      const message = update?.message || update?.edited_message;

      if (!message || !message.text) {
        return new Response("OK", { status: 200 });
      }

      const chatId = message.chat.id;
      const text = message.text.trim();

      // Security check: Restrict access if ALLOWED_CHAT_ID is set
      if (env.ALLOWED_CHAT_ID && String(chatId) !== String(env.ALLOWED_CHAT_ID)) {
        await sendTelegramMessage(
          env.BOT_TOKEN,
          chatId,
          "⛔ *Akses Ditolak*\nMaaf, bot ini diset khusus untuk private tracking pemilik repositori."
        );
        return new Response("OK", { status: 200 });
      }

      // Command regex match: /analyze <TICKER>, /analisis <TICKER>, /stock <TICKER>
      const analyzeMatch = text.match(/^\/(?:analyze|analisis|stock)(?:@\w+)?(?:\s+([A-Za-z0-9]+))?$/i);
      const helpMatch = text.match(/^\/(?:start|help)(?:@\w+)?$/i);

      if (helpMatch) {
        const welcomeText =
          `🤖 *IDX Stock Intelligence — Triple-Engine Multi-Agent*\n\n` +
          `Sistem analisis saham berbasis AI Gemini & LangGraph Multi-Agent.\n\n` +
          `📌 *Perintah Tersedia:*\n` +
          `• \`/analyze <TICKER>\` — Menjalankan deep-dive 7-node LangGraph untuk 1 saham BEI.\n` +
          `  _Contoh: \`/analyze BBRI\` atau \`/analyze ANTM\`_\n` +
          `• \`/help\` — Menampilkan panduan ini.\n\n` +
          `⚡ *3 Mesin Utama:*\n` +
          `1. *Morning Brief* (08:25 WIB): Radar Makro, Likuiditas Sektor & Berita.\n` +
          `2. *Price Sentry* (Tiap 15 mnt jam bursa): Pemantau Level Kritis Watchlist.\n` +
          `3. *Deep Agent* (On-Demand): 4 Analis + Debat Bull vs Bear + Trader Plan + Keputusan Portofolio.`;

        await sendTelegramMessage(env.BOT_TOKEN, chatId, welcomeText);
        return new Response("OK", { status: 200 });
      }

      if (analyzeMatch) {
        const tickerRaw = analyzeMatch[1];

        if (!tickerRaw) {
          await sendTelegramMessage(
            env.BOT_TOKEN,
            chatId,
            `⚠️ *Format Perintah Kurang Lengkap*\n\n` +
            `Gunakan: \`/analyze <KODE_SAHAM>\`\n` +
            `Contoh: \`/analyze BBRI\` atau \`/analyze ANTM\``
          );
          return new Response("OK", { status: 200 });
        }

        const ticker = tickerRaw.toUpperCase();

        // 1. Send immediate confirmation to user
        const ackText =
          `⏳ *Tim Agen Multi-Agent sedang menganalisis ${ticker}...*\n\n` +
          `Alur kerja LangGraph telah dipicu di GitHub Actions:\n` +
          `• 🔬 Technical, Fundamental, Flow & News Analyst\n` +
          `• ⚔️ Bull vs Bear Researcher Debate\n` +
          `• 📋 Research Manager & Trader Entry/TP/SL Plan\n` +
          `• 🛡️ Portfolio Manager Final 5-Tier Decision\n\n` +
          `_Laporan eksekutif akan dikirimkan otomatis ke sini dalam 1-2 menit._`;

        await sendTelegramMessage(env.BOT_TOKEN, chatId, ackText);

        // 2. Dispatch GitHub Actions workflow
        ctx.waitUntil(triggerGitHubDispatch(env, ticker, chatId));

        return new Response("OK", { status: 200 });
      }

      return new Response("OK", { status: 200 });
    } catch (err) {
      console.error("Worker error:", err);
      return new Response("OK", { status: 200 });
    }
  },
};

/**
 * Sends a Markdown formatted message to Telegram
 */
async function sendTelegramMessage(botToken, chatId, text) {
  if (!botToken || !chatId) return;

  const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: text,
        parse_mode: "Markdown",
        disable_web_page_preview: true,
      }),
    });
  } catch (e) {
    console.error("Failed to send telegram message:", e);
  }
}

/**
 * Triggers the GitHub Actions workflow_dispatch event
 */
async function triggerGitHubDispatch(env, ticker, chatId) {
  const repo = env.GITHUB_REPO || "Gracias-48/Stock-Analysis";
  const ref = env.GITHUB_REF || "main";
  const pat = env.GITHUB_PAT;

  if (!pat) {
    console.error("GITHUB_PAT is missing in environment variables!");
    await sendTelegramMessage(
      env.BOT_TOKEN,
      chatId,
      `❌ *Gagal Memicu Analisis ${ticker}*\n` +
      `Variabel \`GITHUB_PAT\` belum disetel pada Cloudflare Worker Secrets.`
    );
    return;
  }

  const url = `https://api.github.com/repos/${repo}/actions/workflows/deep_agent.yml/dispatches`;

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${pat}`,
        "User-Agent": "Cloudflare-Worker-Telegram-Bot",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: ref,
        inputs: {
          ticker: ticker,
        },
      }),
    });

    if (response.status === 204) {
      console.log(`Successfully dispatched deep_agent.yml for ${ticker}`);
    } else {
      const errorText = await response.text();
      console.error(`GitHub API error (${response.status}):`, errorText);
      await sendTelegramMessage(
        env.BOT_TOKEN,
        chatId,
        `⚠️ *Gagal Memulai Workflow GitHub Actions*\n` +
        `Status: ${response.status}\nRespon: \`${errorText.slice(0, 150)}\``
      );
    }
  } catch (err) {
    console.error("Failed to trigger GitHub dispatch:", err);
    await sendTelegramMessage(
      env.BOT_TOKEN,
      chatId,
      `❌ *Terjadi Kesalahan Koneksi*\nTidak dapat menghubungi GitHub Actions API: ${err.message}`
    );
  }
}
