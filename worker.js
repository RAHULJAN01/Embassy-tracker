/**
 * Madison & Main — Register Control Worker
 * =========================================
 * A free, always-online Cloudflare Worker that sits between the register page
 * and GitHub, so the page never has to hold a GitHub token.
 *
 * The page authenticates with the SAME site password you already type to unlock
 * the register. The Worker holds the GitHub token as a server-side secret.
 *
 * Secrets to set on the Worker (Settings -> Variables -> Add secret):
 *   SITE_PASSWORD  - the same password that unlocks the register page
 *   GH_TOKEN       - a GitHub fine-grained token, Actions: Read and write
 *   GH_REPO        - e.g. RAHULJAN01/embassy-tracker
 *
 * Endpoints (all POST except /status):
 *   GET  /status        -> what the bots are doing right now (live, no rebuild)
 *   POST /run           -> { mode: "roots" | "live" }  start a crawl
 *                          { mode: "deepone", sol: "PR123" } re-read ONE
 *                          solicitation on the strong model and nothing else
 *   POST /stop          -> halt the whole fleet
 *   POST /resume        -> let the fleet run again
 *   POST /verify        -> { sol: "..." } re-verify one solicitation
 *   GET  /operator      -> the operator's own actions (delete / hide / switch)
 *   POST /operator      -> { action, sol, tier? } record one, on the SERVER, so
 *                          a solicitation you delete on your phone is deleted on
 *                          your laptop too — and the bots honour it as well.
 *                          action: delete | restore | hide | unhide | switch | clear
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,X-Site-Key",
};

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });

function authorised(request, env) {
  const key =
    request.headers.get("X-Site-Key") ||
    new URL(request.url).searchParams.get("k") ||
    "";
  // constant-ish time compare, good enough for a single-user control surface
  const a = key || "";
  const b = env.SITE_PASSWORD || "";
  if (a.length !== b.length || !b) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function gh(env, path, init = {}) {
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `token ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "mm-register-control",
      ...(init.headers || {}),
    },
  });
  return r;
}

async function dispatch(env, workflow, inputs) {
  const r = await gh(env, `/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: inputs || {} }),
  });
  return r.status === 204
    ? { ok: true }
    : { ok: false, status: r.status, msg: (await r.text()).slice(0, 180) };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/" || path === "/health") {
      return json({ ok: true, service: "mm-register-control" });
    }

    if (!authorised(request, env)) {
      return json({ ok: false, error: "unauthorised" }, 401);
    }

    try {
      // ---- live status: what are the bots doing RIGHT NOW ----
      if (path === "/status") {
        const runs = await gh(
          env,
          "/actions/workflows/crawl.yml/runs?per_page=5"
        );
        const d = await runs.json();
        const list = (d.workflow_runs || []).map((r) => ({
          id: r.id,
          status: r.status,
          conclusion: r.conclusion,
          event: r.event,
          started: r.created_at,
          url: r.html_url,
        }));
        const live = list.find(
          (r) => r.status === "in_progress" || r.status === "queued"
        );
        // the committed status.json, so the page can show detail between deploys
        let snapshot = null;
        try {
          const f = await gh(env, "/contents/status.json");
          if (f.ok) {
            const j = await f.json();
            snapshot = JSON.parse(atob(j.content.replace(/\n/g, "")));
          }
        } catch (e) {}
        return json({ ok: true, running: !!live, current: live || null, recent: list, snapshot });
      }

      // ---- read the operator's decisions without waiting for a rebuild ----
      if (path === "/operator" && request.method === "GET") {
        const f = await gh(env, "/contents/operator.json");
        if (!f.ok) return json({ ok: true, state: { deleted: {}, hidden: {}, switched: {} } });
        const j = await f.json();
        let state = { deleted: {}, hidden: {}, switched: {} };
        try { state = JSON.parse(atob(j.content.replace(/\n/g, ""))); } catch (e) {}
        return json({ ok: true, state });
      }

      // ---- WHERE ARE THE BOTS RIGHT NOW ----
      // The crawler prints a [PROGRESS] line as it works; we read it out of the
      // running job's log. GitHub answers the logs endpoint with a 302 to a
      // SIGNED url that rejects an Authorization header — so the redirect is
      // taken manually and the second fetch is sent bare. Sending our token
      // along was why this came back empty.
      if (path === "/live" && request.method === "GET") {
        const runs = await gh(env, "/actions/workflows/crawl.yml/runs?per_page=5");
        const d = await runs.json();
        const all = d.workflow_runs || [];
        const run = all.find((r) => r.status === "in_progress" || r.status === "queued") || all[0];
        if (!run) return json({ ok: true, running: false, bots: [] });

        const jr = await gh(env, `/actions/runs/${run.id}/jobs?per_page=20`);
        const jd = await jr.json();
        const jobs = (jd.jobs || []).filter((j) => /^crawl/.test(j.name));

        async function logOf(jobId) {
          const r1 = await gh(env, `/actions/jobs/${jobId}/logs`, { redirect: "manual" });
          if (r1.status === 200) return await r1.text();
          const loc = r1.headers.get("location");
          if (!loc) return "";
          const r2 = await fetch(loc);           // bare: no Authorization
          return r2.ok ? await r2.text() : "";
        }

        const bots = await Promise.all(
          jobs.map(async (j) => {
            const shard = (j.name.match(/\((\d+)\)/) || [])[1] ?? String(jobs.indexOf(j));
            const step = (j.steps || []).find((s) => s.status === "in_progress");
            const base = {
              shard, status: j.status, conclusion: j.conclusion,
              step: step ? step.name : "",
              startedAt: j.started_at, finishedAt: j.completed_at,
            };
            try {
              const txt = await logOf(j.id);
              const lines = txt.split("\n").filter((l) => l.includes("[PROGRESS]"));
              if (!lines.length) return base;
              const last = lines[lines.length - 1];
              const f = {};
              for (const m of last.matchAll(/(\w+)="([^"]*)"/g)) f[m[1]] = m[2];
              // every post this bot has touched this run, newest last
              const posts = [...new Set(
                lines.map((l) => (l.match(/post="([^"]*)"/) || [])[1]).filter(Boolean)
              )];
              return { ...base, ...f, steps: lines.length,
                       visited: posts.length, recent: posts.slice(-4) };
            } catch (e) {
              return base;
            }
          })
        );
        return json({
          ok: true,
          running: run.status === "in_progress" || run.status === "queued",
          runStatus: run.status, conclusion: run.conclusion,
          startedAt: run.created_at, runUrl: run.html_url, bots,
        });
      }

      if (request.method !== "POST")
        return json({ ok: false, error: "use POST" }, 405);

      const body = await request.json().catch(() => ({}));

      if (path === "/run") {
        const ok = ["live", "probe", "probe-sites", "deepone", "roots"];
        const mode = ok.includes(body.mode) ? body.mode : "roots";
        // deepone works on ONE solicitation and needs to be told which. Without
        // a reference it would have nothing to do, so it is refused here rather
        // than burning a workflow run to find that out.
        if (mode === "deepone") {
          const sol = String(body.sol || "").trim();
          if (!sol) return json({ ok: false, error: "deepone needs a solicitation number" }, 400);
          return json(await dispatch(env, "crawl.yml", { mode, sol }));
        }
        return json(await dispatch(env, "crawl.yml", { mode }));
      }
      if (path === "/stop") {
        return json(await dispatch(env, "control.yml", { action: "stop" }));
      }
      if (path === "/resume") {
        return json(await dispatch(env, "control.yml", { action: "resume" }));
      }
      if (path === "/verify") {
        // a targeted re-check; the crawler re-reads and re-adjudicates
        return json(await dispatch(env, "crawl.yml", { mode: "live" }));
      }

      // ---- the operator's own decisions, kept on the server ----
      if (path === "/operator") {
        const sol = String(body.sol || "").trim();
        const action = String(body.action || "").trim();
        if (!sol || !action) return json({ ok: false, error: "need sol and action" }, 400);

        const f = await gh(env, "/contents/operator.json");
        let state = { deleted: {}, hidden: {}, switched: {}, updated: "" };
        let sha;
        if (f.ok) {
          const j = await f.json();
          sha = j.sha;
          try { state = { ...state, ...JSON.parse(atob(j.content.replace(/\n/g, ""))) }; } catch (e) {}
        }
        const stamp = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";
        if (action === "delete")       state.deleted[sol]  = { on: stamp };
        else if (action === "restore") delete state.deleted[sol];
        else if (action === "hide")    state.hidden[sol]   = { on: stamp };
        else if (action === "unhide")  delete state.hidden[sol];
        else if (action === "switch")  state.switched[sol] = { tier: String(body.tier || "").toUpperCase(), on: stamp };
        else if (action === "clear")   delete state.switched[sol];
        else return json({ ok: false, error: "unknown action" }, 400);
        state.updated = stamp;

        const put = await gh(env, "/contents/operator.json", {
          method: "PUT",
          body: JSON.stringify({
            message: `operator: ${action} ${sol}`,
            content: btoa(unescape(encodeURIComponent(JSON.stringify(state, null, 1)))),
            ...(sha ? { sha } : {}),
          }),
        });
        if (!put.ok)
          return json({ ok: false, status: put.status, msg: (await put.text()).slice(0, 180) }, 502);
        // rebuild the page so every device sees it
        await dispatch(env, "deploy.yml", {});
        return json({ ok: true, state, stamp });
      }

      return json({ ok: false, error: "unknown endpoint" }, 404);
    } catch (e) {
      return json({ ok: false, error: String(e).slice(0, 200) }, 500);
    }
  },
};
