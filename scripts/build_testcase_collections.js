#!/usr/bin/env node
/*
 * Builds, for every docs/testcases/_cases/<slug>.cases.json:
 *   docs/testcases/<slug>_test_cases.md      human-readable test-case sheet
 *   <slug>.postman_collection.json           Postman v2.1 collection
 *
 * The collection mirrors the procurement/BOM layout: four category folders
 * (Positive / Negative / Boundary / Business Logic), one sub-folder per
 * endpoint group, one request per case (a folder of ordered steps when the
 * case makes more than one call).
 *
 * Run:  node scripts/build_testcase_collections.js
 */
const fs = require("fs");
const path = require("path");

const BACKEND = path.resolve(__dirname, "..");
const CASES_DIR = path.join(BACKEND, "docs", "testcases", "_cases");
const DOCS_DIR = path.join(BACKEND, "docs", "testcases");

const CAT_FOLDER = { POS: "Positive", NEG: "Negative", BND: "Boundary", LGC: "Business Logic" };
const CAT_ORDER = ["POS", "NEG", "BND", "LGC"];
const CAT_LABEL = { POS: "POS", NEG: "NEG", BND: "BND", LGC: "LGC" };

const NIL_UUID = "00000000-0000-0000-0000-000000000000";

const ACCOUNTS = [
  ["Managing Director", "9000000000", "md_token"],
  ["Direct Manager", "9000000001", "dm_token"],
  ["Cutting Manager", "9000000002", "cm_token"],
  ["Stitching Manager", "9000000003", "other_token"],
  ["Office Viewer", "9000000004", "viewer_token"],
  ["HR / Accounts", "9000000005", "hr_token"],
];

/* ----------------------------------------------------------------- paths */

const PATH_VARS = [
  [/\{supplier_id\}/g, "{{supplier_id}}"],
  [/\{po_id\}/g, "{{po_id}}"],
  [/\{po_item_id\}/g, "{{po_item_id}}"],
  [/\{bom_id\}/g, "{{bom_id}}"],
  [/\{check_id\}/g, "{{check_id}}"],
  [/\{tracking_id\}/g, "{{tracking_id}}"],
  [/\{order_id\}/g, "{{order_id}}"],
  [/\{client_id\}/g, "{{client_id}}"],
  [/\{tracking_token\}/g, "{{tracking_token}}"],
  [/\{token\}/g, "{{tracking_token}}"],
];

// <angle placeholders> that appear inside a URL rather than a body
const URL_PLACEHOLDERS = [
  [/<wrapped target>|<the wrapped target>|<target>/g, "{{tracking_target}}"],
  [/<valid signature>/g, "{{tracking_sig}}"],
  [/<that item>|<that stock item>/g, "{{search_term}}"],
  [/<[^>]*>/g, "PLACEHOLDER"],
];

// Idempotent: {{vars}} are parked before the {placeholder} rewrites run.
function normalisePath(p) {
  const parked = [];
  let out = p.trim().replace(/\{\{[^{}]*\}\}/g, (m) => {
    parked.push(m);
    return `\u0000${parked.length - 1}\u0000`;
  });
  out = out
    .replace(/\[[^\]]*\]/g, "")
    .replace(/[.,;:)]+$/, "") // prose often wraps a call in (parentheses)
    .replace(/^\(+/, "")
    .replace(/\?$/, "")
    .replace(/\{$/, "");
  for (const [re, rep] of PATH_VARS) out = out.replace(re, rep);
  for (const [re, rep] of URL_PLACEHOLDERS) out = out.replace(re, rep);
  out = out.replace(/\{[^}]*\}/g, (m) => (m.startsWith("{{") ? m : NIL_UUID));
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => parked[Number(i)]);
}

function fullUrl(p, prefix) {
  const clean = normalisePath(p);
  if (clean.startsWith("/api/")) return "{{base_url}}" + clean;
  return "{{base_url}}" + prefix + (clean.startsWith("/") ? clean : "/" + clean);
}

function urlObject(raw) {
  const [beforeQuery, queryStr] = raw.split("?");
  const url = {
    raw,
    host: ["{{base_url}}"],
    path: beforeQuery.replace("{{base_url}}", "").split("/").filter(Boolean),
  };
  if (queryStr) {
    url.query = queryStr.split("&").map((pair) => {
      const [key, value = ""] = pair.split("=");
      return { key, value };
    });
  }
  return url;
}

/* ------------------------------------------------------ step extraction */

// path token: "/" or "..." then non-space chars, with {…} and <…> groups
// allowed to contain spaces ("/t/c/{token}?u=<wrapped target>&s=<valid signature>")
const METHOD_RE = /\b(GET|POST|PATCH|PUT|DELETE)\b\s+((?:\.\.\.|\/)(?:\{[^}]*\}|<[^>]*>|[^\s])*)/g;

const KNOWN_TAILS =
  /\/(items|submit|approve|reject|cancel|send|acknowledge|reactivate|transition|preview|commit|inventory-check|generate-pos|stream)$/;

// Prose shorthand: "…" / "..." stands for the endpoint under test.
function prep(text) {
  return String(text || "")
    .replace(/…/g, "...")
    .replace(/\{\{base_url\}\}/g, "");
}

function resolveDots(tail, basePath) {
  if (!tail || tail === "/") return basePath;
  const segs = tail.split("/").filter(Boolean);
  if (segs.length >= 2) return "/" + segs.join("/");
  return basePath.replace(KNOWN_TAILS, "") + tail;
}

function extractCalls(text, basePath) {
  const src = prep(text);
  const calls = [];
  let m;
  METHOD_RE.lastIndex = 0;
  while ((m = METHOD_RE.exec(src)) !== null) {
    let p = m[2];
    if (p.startsWith("...")) p = resolveDots(p.slice(3), basePath);
    calls.push({ method: m[1], path: p });
  }
  return calls;
}

// The first "METHOD /path" in a section's `sub` is that section's own endpoint.
function sectionBasePath(sec) {
  const calls = extractCalls(sec.sub, "/");
  return calls.length ? calls[0].path : "/";
}

/* --------------------------------------------------------------- bodies */

function balanced(s) {
  let depth = 0;
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "{") depth++;
    else if (s[i] === "}") {
      depth--;
      if (depth === 0) return s.slice(0, i + 1);
    }
  }
  return null;
}

function extractBody(text) {
  const t = String(text || "");
  const curl = t.match(/-d\s+'(\{[\s\S]*?\})'/);
  if (curl) return curl[1];
  const keyed = t.match(/Body:\s*(\{[\s\S]*)$/);
  if (keyed) return balanced(keyed[1]);
  const dash = t.match(/[—-]\s*(\{[\s\S]*)$/);
  if (dash) return balanced(dash[1]);
  return null;
}

function placeholders(s) {
  return String(s)
    .replace(/<the exact name of an existing supplier>/g, "{{existing_supplier_name}}")
    .replace(/<a deactivated supplier's uuid>/g, "{{inactive_supplier_id}}")
    .replace(/<an active supplier uuid>/g, "{{supplier_id}}")
    .replace(/<active supplier uuid>/g, "{{supplier_id}}")
    .replace(/<a different supplier>/g, "{{supplier_id_2}}")
    .replace(/<a uuid not on this PO>/g, NIL_UUID)
    .replace(/<po_item uuid>/g, "{{po_item_id}}")
    .replace(/<several paragraphs>/g,
      "Rejected on cross-check: the unit rate is well above the last purchase price for this article, the quantity does not match the shortfall on the inventory check, and the delivery window misses the cutting start date. Please correct all three and resubmit.")
    .replace(/<10,000 characters of text>/g, "lorem ipsum ".repeat(40).trim())
    .replace(/<the wrapped target>|<wrapped target>/g, "{{tracking_target}}")
    .replace(/<valid signature>/g, "{{tracking_sig}}")
    .replace(/<token>/g, "{{tracking_token}}")
    .replace(/<token2>/g, "{{tracking_token_2}}")
    .replace(/<uuid>/g, "{{po_item_id}}")
    .replace(/<[^>]*>/g, "1");
}

function prettyJson(raw) {
  if (!raw) return null;
  const withVars = placeholders(raw);
  try {
    return JSON.stringify(JSON.parse(withVars), null, 2);
  } catch (e) {
    return withVars;
  }
}

function usableBody(raw) {
  if (!raw) return null;
  return /\.\.\./.test(raw) ? null : raw;
}

// When a step describes the call in prose only ("reject with a reason"), fall
// back to the endpoint's own representative payload so the request is sendable.
const PATH_BODIES = [
  [/\/pos\/[^/]+\/reject$/, { reason: "Rate is above the last purchase price" }],
  [/\/pos\/[^/]+\/acknowledge$/, { channel: "manual", confirmed_qty: 100, notes: "Supplier confirmed" }],
  [/\/pos\/[^/]+\/items$/, { base_revision: "{{base_revision}}", item_edits: [{ po_item_id: "{{po_item_id}}", field: "unit_price", value: 180.5 }] }],
  [/\/production-tracking\/[^/]+\/transition$/, { status: "released_to_production" }],
  [/\/suppliers$/, { name: "New Vendor Pvt Ltd" }],
  [/\/suppliers\/[^/]+$/, { phone: "9876543210" }],
  [/\/chat(\/stream)?$/, { question: "Where is the bottleneck?", use_llm: false }],
  [/\/webhooks\/twilio\/whatsapp$/, { tracking_token: "{{tracking_token}}", Body: "Confirmed, dispatching Friday" }],
  [/\/webhooks\/twilio\/voice$/, { tracking_token: "{{tracking_token}}", Digits: "1" }],
  [/\/webhooks\/ses$/, { tracking_token: "{{tracking_token}}", eventType: "Delivery" }],
];

function pathBody(method, p) {
  if (!["POST", "PUT", "PATCH"].includes(method)) return null;
  if (method === "POST" && /\/suppliers\/[^/]+$/.test(p)) return null; // reactivate takes none
  for (const [re, body] of PATH_BODIES) if (re.test(p)) return JSON.stringify(body, null, 2);
  return null;
}

/* -------------------------------------------------------------- uploads */

function fixtureFor(p, text) {
  const at = String(text).match(/file\s*=\s*@?\s*([A-Za-z0-9_.\-]+\.[A-Za-z0-9]+)/);
  const named = at ? at[1].trim() : null; // only a real filename, never prose
  if (/\/suppliers\/import\//.test(p)) return named || "supplier_master.xlsx";
  if (/\/inventory\/(preview|commit)$/.test(p)) return named || "inventory_master.xlsx";
  if (/multipart|file\s*=/.test(text)) return named || "upload_fixture";
  return null;
}

// A case's steps are prose lines: a call on one line, its "Body: {...}" often on
// the next. Group each call with the continuation lines that belong to it.
function groupSteps(steps, base) {
  const groups = [];
  for (const step of steps) {
    const calls = extractCalls(step, base);
    if (calls.length) {
      calls.forEach((call, i) => groups.push({ call, text: i === 0 ? step : "" }));
    } else if (groups.length) {
      groups[groups.length - 1].text += "\n" + step;
    }
  }
  return groups;
}

/* ---------------------------------------------------------------- auth  */

function tokenFor(roleText) {
  const r = String(roleText || "").toLowerCase();
  if (/no authentication|unauthenticated|no token/.test(r)) return "noauth";
  if (/bad token|garbage|expired/.test(r)) return "expired_token";
  if (/managing director/.test(r) && !/then/.test(r)) return "md_token";
  if (/^managing director/.test(r)) return "md_token";
  if (/hr/.test(r)) return "hr_token";
  if (/office viewer|viewer/.test(r)) return "viewer_token";
  if (/cutting manager/.test(r)) return "cm_token";
  if (/stitching manager|lining manager|security/.test(r)) return "other_token";
  if (/direct manager|\bdm\b/.test(r)) return "dm_token";
  if (/managing director|\bmd\b/.test(r)) return "md_token";
  return "dm_token";
}

function authFor(tokenVar) {
  if (tokenVar === "noauth") return { type: "noauth" };
  return { type: "bearer", bearer: [{ key: "token", value: `{{${tokenVar}}}`, type: "string" }] };
}

/* ------------------------------------------------------------- requests */

function testScript(id, title, statusHint, expected, capture) {
  const lines = [`// ${id} — ${title}`];
  if (expected) lines.push(`// Expected: ${String(expected).replace(/\s+/g, " ").slice(0, 220)}`);
  const code = String(statusHint || "").match(/^\d{3}$/);
  if (code) {
    lines.push(`pm.test("Status is ${code[0]}", function () {`);
    lines.push(`    pm.response.to.have.status(${code[0]});`);
    lines.push(`});`);
  } else {
    lines.push(`// No single deterministic status for this case — verify by hand.`);
    lines.push(`pm.test("Response received (inspect manually)", function () {`);
    lines.push(`    pm.expect(pm.response.code).to.be.a("number");`);
    lines.push(`});`);
  }
  if (capture) {
    lines.push("");
    lines.push("// carry ids forward for the chained requests");
    lines.push("try {");
    lines.push("    const body = pm.response.json();");
    lines.push('    ["supplier_id", "po_id", "bom_id", "check_id", "tracking_id", "tracking_token"]');
    lines.push("        .forEach(function (k) { if (body && body[k]) pm.collectionVariables.set(k, body[k]); });");
    lines.push("    if (body && typeof body.revision === \"number\") pm.collectionVariables.set(\"base_revision\", body.revision);");
    lines.push("} catch (e) { /* non-JSON response */ }");
  }
  return lines;
}

function describe(c, sectionLabel, gate) {
  const out = [`**${c.id} — ${c.title}**`, ""];
  out.push("| | |");
  out.push("|---|---|");
  out.push(`| Category | ${CAT_LABEL[c.cat]} |`);
  out.push(`| Priority | ${c.prio} |`);
  if (c.role) out.push(`| Role | ${c.role} |`);
  out.push(`| Endpoint group | ${sectionLabel} |`);
  if (gate) out.push(`| Role gate | ${gate} |`);
  out.push("");
  out.push("**Request / steps**");
  out.push("");
  c.steps.forEach((s) => out.push(`- ${s}`));
  out.push("");
  out.push("**Expected result**");
  out.push("");
  out.push(c.expected);
  return out.join("\n");
}

function makeRequest({ name, method, rawUrl, tokenVar, body, fixture, description, script }) {
  const request = { method, header: [], url: urlObject(rawUrl), description };
  if (tokenVar) request.auth = authFor(tokenVar);
  if (fixture) {
    request.body = {
      mode: "formdata",
      formdata: [{ key: "file", type: "file", src: "", description: `Select the fixture: ${fixture}` }],
    };
  } else if (body) {
    request.header.push({ key: "Content-Type", value: "application/json" });
    request.body = { mode: "raw", raw: body, options: { raw: { language: "json" } } };
  }
  return {
    name,
    event: [{ listen: "test", script: { type: "text/javascript", exec: script } }],
    request,
    response: [],
  };
}

function buildCase(sec, c, prefix) {
  const base = sectionBasePath(sec);
  const label = `${sec.code} · ${sec.title}`;
  const stepsText = c.steps.join("\n");
  const calls = extractCalls(stepsText, base);
  const tokenVar = tokenFor(c.role);
  const success = /^2\d\d$/.test(String(c.status));

  const bodyFor = (call, text) => {
    if (!["POST", "PUT", "PATCH"].includes(call.method)) return null;
    return usableBody(prettyJson(extractBody(text))) || pathBody(call.method, normalisePath(call.path));
  };

  if (calls.length <= 1) {
    const call = calls[0] || { method: guessMethod(sec), path: base };
    const p = normalisePath(call.path);
    const fixture = call.method === "GET" ? null : fixtureFor(p, stepsText);
    return makeRequest({
      name: `${c.id} · ${c.title}`,
      method: call.method,
      rawUrl: fullUrl(call.path, prefix),
      tokenVar,
      body: fixture ? null : bodyFor(call, stepsText),
      fixture,
      description: describe(c, label, sec.gate),
      script: testScript(c.id, c.title, c.status, c.expected, success),
    });
  }

  // multi-step case -> an ordered folder; only the last step carries the assertion
  const items = [];
  {
    for (const { call, text: step } of groupSteps(c.steps, base)) {
      const n = items.length + 1;
      const isLast = n === calls.length;
      const p = normalisePath(call.path);
      const fixture = call.method === "GET" ? null : fixtureFor(p, step);
      // an unauthenticated case (tracking/webhook) still verifies through a
      // normal authenticated read — only the public routes stay anonymous
      const stepToken =
        tokenVar === "noauth" && !/\/(t\/[oc]|webhooks)\//.test(p) ? "dm_token" : tokenVar;
      items.push(
        makeRequest({
          name: `${c.id}.${n} · step ${n} · ${call.method} ${p}`,
          method: call.method,
          rawUrl: fullUrl(call.path, prefix),
          tokenVar: stepToken,
          body: fixture ? null : bodyFor(call, step),
          fixture,
          description: describe({ ...c, id: `${c.id} · step ${n}` }, label, sec.gate),
          script: testScript(`${c.id}.${n}`, c.title, isLast ? c.status : null, isLast ? c.expected : "", true),
        })
      );
    }
  }
  return { name: `${c.id} · ${c.title}`, description: describe(c, label, sec.gate), item: items };
}

function guessMethod(sec) {
  const calls = extractCalls(sec.sub, "/");
  return calls.length ? calls[0].method : "GET";
}

/* ----------------------------------------------------------- collection */

function loginRequest(label, phone, varName) {
  return {
    name: `Login · ${label} → ${varName}`,
    event: [
      {
        listen: "test",
        script: {
          type: "text/javascript",
          exec: [
            'pm.test("Login succeeded", function () { pm.response.to.have.status(200); });',
            "try {",
            "    const body = pm.response.json();",
            "    const token = body.access_token || body.token;",
            `    if (token) pm.collectionVariables.set("${varName}", token);`,
            "} catch (e) { /* non-JSON response */ }",
          ],
        },
      },
    ],
    request: {
      auth: { type: "noauth" },
      method: "POST",
      header: [{ key: "Content-Type", value: "application/json" }],
      url: urlObject("{{base_url}}/api/v1/auth/login"),
      description: `${label} — demo account, phone = password.`,
      body: {
        mode: "raw",
        raw: JSON.stringify({ username: phone, password: phone }, null, 2),
        options: { raw: { language: "json" } },
      },
    },
    response: [],
  };
}

const EXTRA_VARS = {
  supplier_po: ["supplier_id", "supplier_id_2", "inactive_supplier_id", "existing_supplier_name",
    "po_id", "po_item_id", "base_revision", "bom_id", "tracking_token", "tracking_token_2",
    "tracking_target", "tracking_sig", "tracking_id", "order_id", "client_id"],
  inventory: ["bom_id", "check_id", "order_id", "client_id"],
  intelligence: ["style_name"],
};

function buildCollection(spec) {
  const folders = CAT_ORDER.map((cat) => ({ name: CAT_FOLDER[cat], item: [] }));

  for (const sec of spec.sections) {
    const byCat = {};
    for (const c of sec.cases) (byCat[c.cat] = byCat[c.cat] || []).push(c);
    CAT_ORDER.forEach((cat, idx) => {
      const cases = byCat[cat];
      if (!cases || !cases.length) return;
      folders[idx].item.push({
        name: `${sec.code} · ${sec.title}`,
        description: [sec.sub, sec.gate ? `Role gate: ${sec.gate}` : ""].filter(Boolean).join("\n\n"),
        item: cases.map((c) => buildCase(sec, c, spec.prefix)),
      });
    });
  }

  const vars = [
    { key: "base_url", value: "http://127.0.0.1:8000", type: "string" },
    ...ACCOUNTS.map(([, , v]) => ({ key: v, value: "", type: "string" })),
    { key: "expired_token", value: "expired.or.garbage.token", type: "string" },
    ...(EXTRA_VARS[spec.slug] || []).map((k) => ({ key: k, value: "", type: "string" })),
  ];

  return {
    info: {
      name: spec.service,
      description: [
        `${spec.service} — ${spec.stage}.`,
        "",
        spec.intro,
        "",
        "Folders: **Positive / Negative / Boundary / Business Logic**; inside each, one sub-folder per endpoint group.",
        "Request names start with the case ID so they map 1:1 to `docs/testcases/" + spec.slug + "_test_cases.md`.",
        "",
        "SETUP",
        "1. Set `base_url`.",
        "2. Run **Setup — Tokens**; each login stores its own token variable.",
        "3. Fill the id variables from seed data or from an earlier response.",
        "",
        "NOTES",
        ...spec.notes.map((n) => "- " + n.replace(/\*\*/g, "")),
      ].join("\n"),
      schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    auth: authFor("dm_token"),
    variable: vars,
    item: [
      {
        name: "Setup — Tokens",
        description: "Run these first. Demo accounts use phone = password; each login stores its bearer token in the matching collection variable.",
        item: ACCOUNTS.map(([label, phone, v]) => loginRequest(label, phone, v)),
      },
      ...folders,
    ],
  };
}

/* ------------------------------------------------------------- markdown */

function mdTable(sec) {
  const rows = [
    "| ID | Type | Prio | Case | Request | Expected result |",
    "|---|---|---|---|---|---|",
  ];
  for (const c of sec.cases) {
    const req = c.steps.map((s) => "`" + s.replace(/\|/g, "\\|").replace(/`/g, "'") + "`").join("<br>");
    const exp = c.expected.replace(/\|/g, "\\|").replace(/\n/g, " ");
    const role = c.role ? `<br>_${c.role}_` : "";
    rows.push(
      `| **${c.id}** | ${CAT_LABEL[c.cat]} | ${c.prio} | ${c.title.replace(/\|/g, "\\|")}${role} | ${req} | ${exp} |`
    );
  }
  return rows.join("\n");
}

function buildMarkdown(spec) {
  const total = spec.sections.reduce((a, s) => a + s.cases.length, 0);
  const counts = {};
  spec.sections.forEach((s) => s.cases.forEach((c) => (counts[c.cat] = (counts[c.cat] || 0) + 1)));

  const out = [];
  out.push(`# ${spec.service} — API Test Cases`);
  out.push("");
  out.push(`> ${spec.stage} · module \`${spec.module}\` · ${total} cases`);
  out.push("");
  out.push(spec.intro);
  out.push("");
  out.push("## Connection");
  out.push("");
  out.push("| | |");
  out.push("|---|---|");
  out.push("| Base URL | `http://127.0.0.1:8000` |");
  out.push(`| Module prefix | \`${spec.prefix}\` |`);
  out.push("| Login | `POST /api/v1/auth/login` with `{\"username\": phone, \"password\": phone}` |");
  out.push("| Auth header | `Authorization: Bearer <token>` |");
  out.push("| Docs | `/docs` (Swagger) |");
  out.push("");
  out.push("### Demo accounts (phone = password)");
  out.push("");
  out.push("| Role | Phone | Collection variable |");
  out.push("|---|---|---|");
  ACCOUNTS.forEach(([label, phone, v]) => out.push(`| ${label} | \`${phone}\` | \`{{${v}}}\` |`));
  out.push("");
  out.push("## Case mix");
  out.push("");
  out.push("| Category | Count |");
  out.push("|---|---|");
  CAT_ORDER.forEach((cat) => out.push(`| ${CAT_FOLDER[cat]} (${cat}) | ${counts[cat] || 0} |`));
  out.push(`| **Total** | **${total}** |`);
  out.push("");
  out.push("## Endpoints covered");
  out.push("");
  out.push("| Group | Endpoints | Role gate | Cases |");
  out.push("|---|---|---|---|");
  spec.sections.forEach((s) =>
    out.push(`| ${s.code} · ${s.title} | \`${s.sub}\` | ${s.gate || "—"} | ${s.cases.length} |`)
  );
  out.push("");
  out.push("## Verify before relying on");
  out.push("");
  spec.notes.forEach((n) => out.push(`- ${n}`));
  out.push("");
  for (const sec of spec.sections) {
    out.push(`## ${sec.code} · ${sec.title}`);
    out.push("");
    out.push(`\`${sec.sub}\`${sec.gate ? ` · **Role gate:** ${sec.gate}` : ""}`);
    out.push("");
    out.push(mdTable(sec));
    out.push("");
  }
  out.push("---");
  out.push("");
  out.push(
    `Generated from \`docs/testcases/_cases/${spec.slug}.cases.json\` by \`scripts/build_testcase_collections.js\`. ` +
      `Edit the case file and re-run, so this sheet and \`${spec.slug}.postman_collection.json\` stay in step.`
  );
  out.push("");
  return out.join("\n");
}

/* ------------------------------------------------------------------ run */

function countRequests(node) {
  return node.item ? node.item.reduce((a, n) => a + countRequests(n), 0) : 1;
}

const files = fs.readdirSync(CASES_DIR).filter((f) => f.endsWith(".cases.json"));
for (const f of files) {
  const spec = JSON.parse(fs.readFileSync(path.join(CASES_DIR, f), "utf8"));
  const cases = spec.sections.reduce((a, s) => a + s.cases.length, 0);
  const mdPath = path.join(DOCS_DIR, `${spec.slug}_test_cases.md`);
  fs.writeFileSync(mdPath, buildMarkdown(spec));

  // doc_only: the collection for this service is maintained elsewhere
  if (spec.doc_only) {
    console.log(`${spec.service.padEnd(24)} ${String(cases).padStart(3)} cases -> ${path.relative(BACKEND, mdPath)} (doc only)`);
    continue;
  }

  const collection = buildCollection(spec);
  const jsonPath = path.join(BACKEND, `${spec.slug}.postman_collection.json`);
  fs.writeFileSync(jsonPath, JSON.stringify(collection, null, 2));

  console.log(
    `${spec.service.padEnd(24)} ${String(cases).padStart(3)} cases -> ${String(countRequests(collection)).padStart(3)} requests | ` +
      `${path.relative(BACKEND, mdPath)} + ${path.relative(BACKEND, jsonPath)}`
  );
}
