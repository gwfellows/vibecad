// Animated demos for README.md (docs/img/*.gif), recorded from the running GUI. Not a test: run through
//   ONLY=readme_gifs scripts/gui_smoke.sh docs/img
// Usage: node readme_gifs.js <base_url> <agent_base_url> <out_dir> [three_pkg_dir]
// Each demo is recorded as a video (Playwright), then turned into a GIF with ffmpeg. A drawn cursor, click
// ripples and a caption strip are overlaid, since recorded videos show neither the pointer nor what is going on.
const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");
const { execFileSync } = require("child_process");

const [BASE, AGENT, OUT, THREE] = process.argv.slice(2);
const ONLY = (process.env.GIFS || "").split(",").filter(Boolean);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const W = 1280, H = 760;
// an image to attach in the agent demo
const PHOTO = path.join(__dirname, "..", "..", "docs", "img", "overview.png");

const OVERLAY = () => {
  const cur = document.createElement("div");
  cur.id = "demoCursor";
  cur.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2l7 19 2.5-7.5L20 11z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
  Object.assign(cur.style, { position: "fixed", left: "0", top: "0", zIndex: 99999, pointerEvents: "none", transform: "translate(-3px,-2px)" });
  const cap = document.createElement("div");
  cap.id = "demoCaption";
  Object.assign(cap.style, { position: "fixed", left: "50%", bottom: "18px", transform: "translateX(-50%)", zIndex: 99998,
    background: "rgba(17,24,39,.88)", color: "#fff", font: "600 15px -apple-system, Segoe UI, sans-serif", padding: "7px 16px",
    borderRadius: "18px", pointerEvents: "none", display: "none", whiteSpace: "nowrap" });
  const add = () => { document.body.appendChild(cur); document.body.appendChild(cap); };
  if (document.body) add(); else addEventListener("DOMContentLoaded", add);
  addEventListener("mousemove", (e) => { cur.style.left = e.clientX + "px"; cur.style.top = e.clientY + "px"; }, true);
  addEventListener("mousedown", (e) => {
    const r = document.createElement("div");
    Object.assign(r.style, { position: "fixed", left: e.clientX - 14 + "px", top: e.clientY - 14 + "px", width: "28px", height: "28px",
      borderRadius: "50%", border: "3px solid #f97316", zIndex: 99997, pointerEvents: "none", transition: "all .45s ease-out", opacity: "1" });
    document.body.appendChild(r);
    requestAnimationFrame(() => Object.assign(r.style, { transform: "scale(1.8)", opacity: "0" }));
    setTimeout(() => r.remove(), 600);
  }, true);
  window.demoCaption = (t) => { cap.textContent = t || ""; cap.style.display = t ? "block" : "none"; };
};

async function record(name, base, part, fn) {
  if (ONLY.length && !ONLY.includes(name)) return;
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const vdir = fs.mkdtempSync(path.join(OUT, ".vid-"));
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, recordVideo: { dir: vdir, size: { width: W, height: H } } });
  const page = await ctx.newPage();
  const t0 = Date.now();
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  await page.addInitScript(OVERLAY);
  page.on("console", (m) => { if (process.env.GIF_DEBUG) console.log("console:", m.text().slice(0, 300)); });
  page.on("pageerror", (e) => console.log("pageerror:", String(e).slice(0, 300)));
  await page.goto(base);
  await page.evaluate(() => { try { localStorage.clear(); } catch {} });
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  if (part) {
    await page.selectOption("#partSelect", part);
    await page.waitForFunction(() => document.querySelector("#rebuild")?.hidden !== false, null, { timeout: 20000 });
    await sleep(2500);
  }
  await page.mouse.move(W / 2, H / 2);
  const start = (Date.now() - t0) / 1000;  // trim the page load
  const h = helpers(page);
  try {
    await fn(page, h);
  } catch (e) {
    console.error(`demo ${name} failed:`, e);
  }
  await sleep(800);
  const video = await page.video().path();
  await ctx.close();
  await browser.close();
  const gif = path.join(OUT, `${name}.gif`);
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-ss", String(start), "-i", video, "-vf",
    "fps=8,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
    gif]);
  fs.rmSync(vdir, { recursive: true, force: true });
  console.log(`${gif}  ${(fs.statSync(gif).size / 1e6).toFixed(2)} MB`);
}

function helpers(page) {
  const h = {
    caption: (t) => page.evaluate((t) => window.demoCaption(t), t),
    world: (x, y, z) => page.evaluate(([x, y, z]) => window.vibecadView.toScreen(x, y, z), [x, y, z]),
    sk: (u, v) => page.evaluate(([u, v]) => window.vibecadSketch.toScreen(u, v), [u, v]),
    glide: async (x, y, steps = 14) => { await page.mouse.move(x, y, { steps }); await sleep(120); },
    click: async (x, y, opt = {}) => { await h.glide(x, y); if (opt.shift) await page.keyboard.down("Shift"); await page.mouse.click(x, y); if (opt.shift) await page.keyboard.up("Shift"); await sleep(opt.wait ?? 450); },
    clickSel: async (sel, wait = 450) => {
      await page.locator(sel).first().scrollIntoViewIfNeeded();
      const b = await page.locator(sel).first().boundingBox();
      await h.click(b.x + b.width / 2, b.y + b.height / 2, { wait });
    },
    type: async (sel, text) => { await h.clickSel(sel, 150); await page.keyboard.press("ControlOrMeta+a"); await page.keyboard.type(text, { delay: 60 }); },
    settle: () => page.waitForFunction(() => document.querySelector("#rebuild")?.hidden !== false, null, { timeout: 30000 }).then(() => sleep(500)),
  };
  return h;
}

(async () => {
  // 1. parameters: change a size in the tree, watch it rebuild; drag the rollback bar; perspective toggle
  await record("params", BASE, "pillow_block.vcad.json", async (page, h) => {
    await h.caption("Every size is a named parameter");
    await h.clickSel("#tree li.feat[data-id=housing_sketch] .caret", 600);
    await h.type('#tree .fparams[data-feature=housing_sketch] li[data-param=housing_r] input', "26 mm");
    await page.keyboard.press("Enter");
    await h.settle();
    await sleep(700);
    await h.caption("Change one and the part rebuilds");
    await h.type('#params tr[data-param="base_w"] input', "80 mm");
    await page.keyboard.press("Enter");
    await h.settle();
    await sleep(900);
    await h.caption("Drag the rollback bar to step through the history");
    const bar = await page.locator("#tree li.rollbar").boundingBox();
    await h.glide(bar.x + 60, bar.y + bar.height / 2);
    await page.mouse.down();
    for (const id of ["bolt_sketch", "shaft_sketch", "pocket_sketch", "housing"]) {
      const li = await page.locator(`#tree li.feat[data-id=${id}]`).boundingBox();
      await page.mouse.move(bar.x + 60, li.y + 2, { steps: 12 });
      await sleep(250);
    }
    await page.mouse.up();
    await h.settle();
    await sleep(900);
    const bar2 = await page.locator("#tree li.rollbar").boundingBox();
    const last = await page.locator("#tree li.feat").last().boundingBox();
    await h.glide(bar2.x + 60, bar2.y + bar2.height / 2);
    await page.mouse.down();
    await page.mouse.move(bar2.x + 60, last.y + last.height + 6, { steps: 20 });
    await page.mouse.up();
    await h.settle();
    await h.caption("Orthographic by default; perspective is one click");
    await h.clickSel("#projBtn", 900);
    await h.clickSel("#projBtn", 700);
    await h.caption("");
  });

  // 2. sketching by hand: a plate from an empty part, then a hole on its top face
  await record("sketch", BASE, null, async (page, h) => {
    page.once("dialog", (d) => d.accept(`demo_plate_${Date.now().toString(36)}`));
    await h.clickSel("#newBtn", 1200);
    await h.caption("+ Sketch on a plane");
    await h.clickSel("#newSketchBtn", 500);
    await h.clickSel("#newMenu [data-d=XY]", 1200);
    await h.caption("Draw: R for a rectangle");
    await page.keyboard.press("r");
    let [x, y] = await h.sk(-20, -12); await h.click(x, y);
    [x, y] = await h.sk(22, 14); await h.click(x, y, { wait: 700 });
    await page.keyboard.press("Escape");
    const mid = (id) => page.evaluate((id) => { const e = window.vibecadSketch.data().entities.find((x) => x.id === id); return window.vibecadSketch.toScreen((e.p1[0] + e.p2[0]) / 2, (e.p1[1] + e.p2[1]) / 2); }, id);
    const dim = async (v) => {
      await page.keyboard.press("d");
      await page.waitForSelector("#dimEdit:not([hidden])");
      await sleep(300);
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type(String(v), { delay: 90 });
      await page.keyboard.press("Enter");
      await h.settle();
    };
    await h.caption("Select an edge, D to dimension it");
    [x, y] = await mid("rect1_bottom"); await h.click(x, y); await dim(50);
    [x, y] = await mid("rect1_left"); await h.click(x, y); await dim(30);
    await h.caption("Pin a corner to the origin: 0 DOF, fully constrained");
    const p1 = await page.evaluate(() => window.vibecadSketch.data().points.find((p) => p.ref === "rect1_bottom.p1").at);
    [x, y] = await h.sk(...p1); await h.click(x, y);
    [x, y] = await h.sk(0, 0); await h.click(x, y, { shift: true });
    await h.clickSel("#sketchTools [data-con=coincident]", 1000);
    await h.caption("Extrude");
    await h.clickSel("#extrudeBtn", 500);
    await h.type("#ffDist", "6");
    await h.clickSel("#ffGo", 400);
    await h.settle();
    await page.click("#fitBtn"); await sleep(900);
    await h.caption("Click a face, sketch on it");
    [x, y] = await h.world(25, 15, 6); await h.click(x, y);
    await h.clickSel("#newSketchBtn", 500);
    await h.clickSel("#newMenu [data-face]", 1200);
    await page.keyboard.press("c");
    [x, y] = await h.sk(25, 15); await h.click(x, y);
    [x, y] = await h.sk(29, 15); await h.click(x, y, { wait: 600 });
    await page.keyboard.press("Escape");
    await h.caption("Cut through");
    await h.clickSel("#extrudeBtn", 500);
    await h.clickSel("#ffThru", 200);
    await page.selectOption("#ffDir", "reverse"); await sleep(250);
    await page.selectOption("#ffMode", "cut"); await sleep(250);
    await h.clickSel("#ffGo", 400);
    await h.settle();
    await page.click("#fitBtn"); await sleep(1200);
    await h.caption("");
  });

  // 3. fillets: pick edges, fillet; later change which edges an existing fillet rounds
  await record("edges", BASE, "l_bracket.vcad.json", async (page, h) => {
    await page.click("#fitBtn"); await sleep(700);
    await h.caption("Double-click a fillet to change which edges it rounds");
    const fl = await page.locator("#tree li.feat[data-id=corner_fillet] .fid").boundingBox();
    await h.glide(fl.x + 30, fl.y + fl.height / 2);
    await page.mouse.dblclick(fl.x + 30, fl.y + fl.height / 2);
    await page.waitForSelector("#edgeEdit:not([hidden])", { timeout: 15000 });
    await sleep(900);
    await h.caption("The part rolls back to before the fillet; click edges on and off");
    let added = 0;
    for (const i of await page.evaluate(() => window.vibecadView.edgeIds())) {  // two visible edges, not yet picked
      if (added === 2) break;
      const ee = await page.evaluate(() => window.vibecadView.edgeEdit());
      if (ee.edges.includes(i)) continue;
      const at = await page.evaluate((i) => window.vibecadView.edgeScreen(i), i);
      if (!at || at[0] < 330 || at[0] > 880 || at[1] < 430 || at[1] > 680) continue;
      await page.mouse.move(at[0], at[1]);
      await sleep(60);
      if (!(await page.textContent(".tip")).startsWith("edge")) continue;
      await h.click(at[0], at[1], { wait: 800 });
      if ((await page.evaluate(() => window.vibecadView.edgeEdit())).edges.length > ee.edges.length) added++;
    }
    await h.clickSel("#edgeEditDone", 400);
    await h.settle();
    await sleep(1400);
    await page.click("#fitBtn"); await sleep(600);
    await h.caption("Click an edge, shift-click more");
    let [x, y] = await h.world(30, 0, 50); await h.click(x, y, { wait: 600 });
    [x, y] = await h.world(30, 5, 50); await h.click(x, y, { shift: true, wait: 600 });
    await h.caption("Fillet the picked edges");
    await h.clickSel("#filletBtn", 500);
    await h.type("#ffSize", "2");
    await h.clickSel("#ffGo", 400);
    await h.settle();
    await sleep(900);
    await h.caption("");
  });

  // 4. the agent (scripted for the demo): point at geometry, attach a file, watch its tool calls
  await record("agent", AGENT, null, async (page, h) => {
    await h.caption("Ask for a part (this demo replays a scripted agent)");
    await h.clickSel("#prompt", 200);
    await page.keyboard.type("Make a 60 x 40 plate, 4 mm thick", { delay: 35 });
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector("#metrics").textContent.includes("last run"), null, { timeout: 60000 });
    await sleep(1500);
    await h.caption("Reference puts a face or edge into the message");
    await h.clickSel("#prompt", 200);
    await page.keyboard.type("Round ", { delay: 35 });
    await h.clickSel("#refBtn", 300);
    let [x, y] = await h.world(10, -20, 4); await h.click(x, y, { wait: 500 });
    await page.keyboard.type("and add a hole like the one in this photo ", { delay: 30 });
    await h.caption("📎 attaches images, PDFs and other files");
    const ab = await page.locator("#attachBtn").boundingBox();
    await h.glide(ab.x + ab.width / 2, ab.y + ab.height / 2);
    await page.setInputFiles("#attachInput", PHOTO);
    await page.waitForFunction(() => document.querySelectorAll("#attachList .att:not(.up)").length === 1, null, { timeout: 10000 });
    await sleep(1200);
    await h.clickSel("#sendBtn", 200);
    await h.caption("Every tool call and its result, live; the view updates as it edits");
    await page.waitForFunction(() => document.querySelectorAll("#log .msg.user").length === 2 && document.querySelector("#metrics").textContent.includes("last run"), null, { timeout: 60000 });
    await sleep(2500);
    await h.caption("");
  });
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
