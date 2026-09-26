// Browser test: holes from the GUI, sized from the fastener table.
// Usage: node holes.js <base_url> <screenshot_dir> [three_pkg_dir]
const { chromium } = require("playwright");
const path = require("path");

const [BASE, OUT, THREE] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `holes_${n}.png`) });
  const post = (url, body) => page.evaluate(([u, b]) => fetch(u, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) }).then((r) => r.json()), [url, body]);
  const vol = () => page.evaluate(() => fetch("/api/state").then((r) => r.json()).then((s) => s.state.volume));
  const settle = () => page.waitForFunction(() => document.querySelector("#rebuild").hidden, null, { timeout: 20000 }).then(() => page.waitForTimeout(600));

  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  const name = `holes_${Date.now().toString(36)}`;
  await post("/api/new", { path: `parts/${name}.vcad.json`, name });
  await post("/api/ops", { message: "plate", ops: [
    { op: "add_feature", feature: { id: "sk", type: "sketch", plane: { datum: "XY" } } },
    { op: "add_rectangle", sketch: "sk", id: "r", width: 60, height: 40, center: [0, 0] },
    { op: "add_feature", feature: { id: "plate", type: "extrude", profile: { sketch: "sk" }, distance: 10 } }] });
  await page.reload();
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  await page.waitForTimeout(1500);
  await page.click("#fitBtn");
  await page.waitForTimeout(600);
  check("Hole is off until a face is picked", await page.isDisabled("#holeBtn"));

  // click the top face at (10, 5), Hole -> counterbore M5
  const [x, y] = await page.evaluate(() => window.vibecadView.toScreen(10, 5, 10));
  await page.mouse.click(x, y);
  await page.waitForTimeout(400);
  check("picking the top face enables Hole", !(await page.isDisabled("#holeBtn")));
  const v0 = await vol();
  await page.click("#holeBtn");
  await page.waitForSelector("#featMenu:not([hidden]) #hoKind", { timeout: 5000 });
  await page.selectOption("#hoKind", "counterbore");
  await page.selectOption("#hoSize", "M5");
  check("the form shows the sizes it will use", (await page.textContent("#hoInfo")).includes("⌀5.5 clearance, c'bore ⌀10 × 5.4"), await page.textContent("#hoInfo"));
  await page.click("#hoGo");
  await settle();
  const tree = await page.$$eval("#tree li.feat .fid", (l) => l.map((x) => x.textContent));
  check("adds a hole sketch and a hole", tree.join() === "sk,plate,hole_sk1,hole1", tree.join());
  const expect1 = Math.PI * 25 * 5.4 + Math.PI * 2.75 ** 2 * (10 - 5.4);
  check("counterbored M5 removes the right volume", Math.abs(v0 - (await vol()) - expect1) < 0.01, `${v0 - (await vol())} vs ${expect1}`);
  const sk = await page.evaluate(() => fetch("/api/sketch/hole_sk1.json").then((r) => r.json()));
  const p = sk.entities.find((e) => e.type === "point");
  check("the hole centre is where the face was clicked", p && Math.abs(Math.abs(p.p1?.[0] ?? p.at?.[0]) - 10) < 0.05, JSON.stringify(p));
  check("its sketch is fully constrained", sk.dof === 0, `${sk.dof} DOF`);
  check("the tree shows a hole icon", await page.$eval('#tree li.feat[data-id="hole1"] .ico svg', (s) => s.innerHTML.includes("stroke-dasharray")));
  await shot("1_cbore");

  // a sketch with two points placed with the Point tool, then Hole at its points: tapped M3, 8 deep
  await post("/api/ops", { message: "pts sketch", ops: [{ op: "add_feature", feature: { id: "pts", type: "sketch", plane: { face: { feature: "plate", role: "end" } } } }] });
  await page.waitForTimeout(800);
  await page.click('#tree li.feat[data-id="pts"] .fid');
  await page.waitForSelector("#sketchBar:not([hidden])");
  await page.waitForTimeout(800);
  check("sketch Hole is off without points", await page.isDisabled("#holeSkBtn"));
  await page.keyboard.press("p");
  for (const [u, v] of [[-20, -10], [-20, 10]]) {
    const [sx, sy] = await page.evaluate(([u, v]) => window.vibecadSketch.toScreen(u, v), [u, v]);
    await page.mouse.click(sx, sy);
    await page.waitForTimeout(700);
  }
  await page.keyboard.press("Escape");
  const n = await page.evaluate(() => window.vibecadSketch.data().entities.filter((e) => e.type === "point").length);
  check("the Point tool places points", n === 2, `${n}`);
  check("sketch Hole turns on", !(await page.isDisabled("#holeSkBtn")));
  const v1 = await vol();
  await page.click("#holeSkBtn");
  await page.waitForSelector("#featMenu:not([hidden]) #hoKind", { timeout: 5000 });
  await page.selectOption("#hoKind", "tapped");
  await page.selectOption("#hoSize", "M3");
  await page.fill("#hoDepth", "8");
  await page.click("#hoGo");
  await settle();
  const expect2 = 2 * (Math.PI * 1.25 ** 2 * 8 + Math.PI / 3 * 1.25 ** 2 * (1.25 / Math.tan(59 * Math.PI / 180)));
  check("two tapped M3 holes, 8 deep", Math.abs(v1 - (await vol()) - expect2) < 0.01, `${v1 - (await vol())} vs ${expect2}`);
  const f = await page.evaluate(() => fetch("/api/feature/hole2").then((r) => r.json()));
  check("the hole feature records size and kind", f.kind === "tapped" && f.size === "M3" && f.sketch === "pts", JSON.stringify(f));
  check("the sketch closes after adding holes", !(await page.isVisible("#sketchBar")));
  await shot("2_tapped");

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
