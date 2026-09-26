// Browser test: per-part conversations, attachments, rebuild indicator, editing a fillet's edges.
// Usage: node workflow.js <base_url (examples)> <agent_base_url (scripted agent)> <screenshot_dir> [three_pkg_dir]
const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");

const [BASE, AGENT, OUT, THREE] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };
// a 2x2 red PNG
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg==", "base64");

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `workflow_${n}.png`) });
  const exactVol = () => page.evaluate(() => fetch("/api/state").then((r) => r.json()).then((s) => s.state.volume));
  const open = async (p) => {
    await page.selectOption("#partSelect", p);
    await page.waitForFunction((p) => document.querySelector("#partName").textContent.includes(p.split("/").pop()), p, { timeout: 15000 });
    await page.waitForFunction(() => !document.querySelector("#rebuild") || document.querySelector("#rebuild").hidden, null, { timeout: 15000 });
    await page.waitForTimeout(1200);
  };
  const ask = async (text) => {
    await page.fill("#prompt", text);
    await page.press("#prompt", "Enter");
    await page.waitForFunction(() => document.querySelector("#metrics").textContent.includes("last run"), null, { timeout: 30000 })
      .catch(async (e) => { await shot("ask_timeout"); console.log("log:", (await page.textContent("#log")).slice(-600)); throw e; });
    await page.waitForTimeout(800);
  };

  // ── rebuild indicator (examples server): hold the edit's response so the edit is slow ──
  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  await open("l_bracket.vcad.json");
  check("3D Extrude is off when every sketch is already used", await page.isDisabled("#extrude3dBtn"));
  await page.evaluate(() => fetch("/api/ops", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ ops: [{ op: "add_feature", feature: { id: "loose_sk", type: "sketch", plane: { datum: "XY", offset: 60 } } },
      { op: "add_rectangle", sketch: "loose_sk", id: "r", width: 10, height: 10, center: [0, 0] }], message: "loose sketch" }) }));
  await page.waitForFunction(() => document.querySelector('#tree li.feat[data-id="loose_sk"]'), null, { timeout: 10000 });
  await page.waitForTimeout(500);
  check("3D Extrude offers the unused sketch", !(await page.isDisabled("#extrude3dBtn")) && (await page.getAttribute("#extrude3dBtn", "title")).includes("loose_sk"));
  await page.click("#extrude3dBtn");
  await page.waitForSelector("#featMenu:not([hidden])", { timeout: 10000 });
  check("it opens the sketch and the extrude form", (await page.textContent("#featMenu .ttl")).includes("loose_sk") && await page.isVisible("#sketchBar"));
  await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
  await page.evaluate(() => fetch("/api/undo", { method: "POST" }));
  await page.waitForTimeout(800);
  await page.route("**/api/ops", async (route) => { await new Promise((r) => setTimeout(r, 1200)); await route.continue(); });
  const inp = page.locator('#params tr[data-param="base_t"] input');
  await inp.fill("6 mm");
  await inp.press("Enter");
  await page.waitForTimeout(700);
  const shown = await page.isVisible("#rebuild");
  const txt = shown ? await page.textContent("#rebuildText") : "";
  check("a slow edit shows the rebuild indicator", shown && /…\s[\d.]+ s/.test(txt), txt);
  await shot("1_rebuilding");
  await page.waitForFunction(() => document.querySelector("#rebuild").hidden, null, { timeout: 15000 });
  check("the indicator goes away when the part is shown", await page.isHidden("#rebuild"));
  check("the edit landed", (await page.inputValue('#params tr[data-param="base_t"] input')) === "6 mm");
  await page.unroute("**/api/ops");

  // ── edit a fillet's edges ──
  await page.click('#tree li.feat[data-id="corner_fillet"]');
  await page.waitForSelector('#details [data-a="edges"]', { timeout: 5000 });
  check("a fillet's details list its edges", (await page.$$eval("#details .edgelist .e", (l) => l.length)) === 1);
  const v0 = await exactVol();
  await page.click('#details [data-a="edges"]');
  await page.waitForSelector("#edgeEdit:not([hidden])", { timeout: 10000 });
  await page.waitForTimeout(600);
  let ee = await page.evaluate(() => window.vibecadView.edgeEdit());
  check("edit edges rolls back to before the fillet and shows its edge", ee && ee.edges.length === 1, JSON.stringify(ee));
  check("rolled back while picking", (await page.textContent("#partStatus")).includes("rolled back"));
  // click another visible edge: try edges until one toggles on
  let added = false;
  for (const i of await page.evaluate(() => window.vibecadView.edgeIds())) {
    if (ee.edges.includes(i)) continue;
    const at = await page.evaluate((i) => window.vibecadView.edgeScreen(i), i);
    if (!at || at[0] < 320 || at[0] > 1050 || at[1] < 120 || at[1] > 850) continue;
    await page.mouse.move(at[0] + 20, at[1] + 20);
    await page.mouse.move(at[0], at[1], { steps: 2 });
    await page.mouse.click(at[0], at[1]);
    await page.waitForTimeout(500);
    const now = await page.evaluate(() => window.vibecadView.edgeEdit());
    if (now.edges.length === 2) { added = true; ee = now; break; }
  }
  check("clicking an edge adds it", added, JSON.stringify(ee));
  await shot("2_edge_edit");
  await page.click("#edgeEditDone");
  await page.waitForSelector("#edgeEdit", { state: "hidden" });
  await page.waitForTimeout(1500);
  const f = await page.evaluate(() => fetch("/api/feature/corner_fillet").then((r) => r.json()));
  check("the fillet now has two edge refs", f.edges?.length === 2, JSON.stringify(f.edges).slice(0, 200));
  check("the part is shown whole again", !(await page.textContent("#partStatus")).includes("rolled back"));
  check("the second fillet removed material", (await exactVol()) < v0 - 1e-6, `${v0} -> ${await exactVol()}`);
  // cancel leaves it alone (double-clicking the fillet in the tree starts the same edge picking)
  await page.dblclick('#tree li.feat[data-id="corner_fillet"] .fid');
  await page.waitForSelector("#edgeEdit:not([hidden])", { timeout: 10000 });
  await page.keyboard.press("Escape");
  await page.waitForSelector("#edgeEdit", { state: "hidden" });
  await page.waitForTimeout(800);
  check("Esc cancels without changing the edges", (await page.evaluate(() => fetch("/api/feature/corner_fillet").then((r) => r.json()))).edges.length === 2);

  // ── conversations and attachments (scripted agent server) ──
  if (process.env.WS_DEBUG) page.on("websocket", (ws) => { ws.on("framereceived", (f) => console.log("ws<", String(f.payload).slice(0, 160))); ws.on("framesent", (f) => console.log("ws>", String(f.payload).slice(0, 160))); });
  await page.goto(AGENT);
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  const newPart = async (name) => {
    page.once("dialog", (d) => d.accept(name));
    await page.click("#newBtn");
    await page.waitForFunction((n) => document.querySelector("#partName").textContent.includes(n), name, { timeout: 10000 });
    await page.waitForTimeout(500);
  };
  const stamp = Date.now().toString(36);
  await newPart(`conv_a_${stamp}`);
  const partA = await page.inputValue("#partSelect");
  await ask("(echo) hello from part A");
  check("the conversation shows on part A", (await page.textContent("#log")).includes("hello from part A"));
  await newPart(`conv_b_${stamp}`);
  check("a new part starts with an empty conversation", !(await page.textContent("#log")).includes("hello from part A"), (await page.textContent("#log")).slice(0, 200));
  await open(partA);
  check("reopening a part brings back its conversation", (await page.textContent("#log")).includes("hello from part A"));
  await page.reload();
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  await page.waitForTimeout(800);
  check("the conversation survives a reload", (await page.textContent("#log")).includes("hello from part A"));

  // attachments: an image and a text file
  const tmp = fs.mkdtempSync("/tmp/vcatt-");
  fs.writeFileSync(path.join(tmp, "sketch.png"), PNG);
  fs.writeFileSync(path.join(tmp, "notes.txt"), "hole 5 mm from the edge\n");
  await page.setInputFiles("#attachInput", [path.join(tmp, "sketch.png"), path.join(tmp, "notes.txt")]);
  await page.waitForFunction(() => document.querySelectorAll("#attachList .att:not(.up)").length === 2, null, { timeout: 10000 });
  check("attached files show as chips", (await page.$$eval("#attachList .att", (l) => l.length)) === 2);
  await ask("(echo) use these");
  const last = await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent);
  check("the agent receives the image and the text", /Attachment blocks: image, text/.test(last), last.slice(0, 300));
  check("the prompt names the attachments", /attached 2 file\(s\): sketch\.png \(image, below\); notes\.txt \(text, below\)/.test(last), last.slice(0, 300));
  check("the sent message shows the image", (await page.$$eval("#log .msg.user .atts img", (l) => l.length)) >= 1);
  check("the attachment list clears after sending", await page.isHidden("#attachList"));
  const imgOk = await page.$eval("#log .msg.user .atts img", (img) => img.complete && img.naturalWidth === 2);
  check("the attached image is served back", imgOk);
  await shot("3_attachments");

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
