// Browser test: import a STEP as reference geometry, see it, sketch on it, project its holes.
// Usage: node imports.js <base_url> <screenshot_dir> [three_pkg_dir] (the examples server: examples/imports/nema17.step)
const { chromium } = require("playwright");
const path = require("path");

const [BASE, OUT, THREE] = process.argv.slice(2);
const STEP = path.join(__dirname, "..", "..", "examples", "imports", "nema17.step");
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `imports_${n}.png`) });
  const settle = () => page.waitForFunction(() => document.querySelector("#rebuild").hidden, null, { timeout: 30000 }).then(() => page.waitForTimeout(700));

  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  const name = `imp_${Date.now().toString(36)}`;
  page.once("dialog", (d) => d.accept(name));
  await page.click("#newBtn");
  await page.waitForFunction((n) => document.querySelector("#partName").textContent.includes(n), name, { timeout: 10000 });
  await page.setInputFiles("#importInput", STEP);
  await page.waitForSelector("#featMenu:not([hidden]) #imMode", { timeout: 15000 });
  check("the import form offers reference by default", (await page.inputValue("#imMode")) === "reference");
  await page.fill("#imT2", "-5");
  await page.click("#imGo");
  await settle();
  const tree = await page.$$eval("#tree li.feat .fid", (l) => l.map((x) => x.textContent));
  check("adds an import feature", tree.join() === "nema171", tree.join());
  const f = await page.evaluate(() => fetch("/api/feature/nema171").then((r) => r.json()));
  check("the file is stored with the part, placed as asked", f.file === "imports/nema17.step" && (f.mode ?? "reference") === "reference" && f.at[2] === -5, JSON.stringify(f));
  const mesh = await page.evaluate(() => fetch("/api/mesh").then((r) => r.json()));
  check("the view gets the reference body, not a solid", mesh.faces.length === 0 && mesh.refs?.[0]?.faces.length === 22, `${mesh.faces.length} / ${mesh.refs?.[0]?.faces.length}`);
  check("status shows no volume (reference only)", (await page.textContent("#partStatus")).includes("–"));
  await shot("1_reference");

  // click the motor's front face (z = -5 now) and sketch on it; project the face outline
  await page.click("#fitBtn");
  await page.waitForTimeout(600);
  const [x, y] = await page.evaluate(() => window.vibecadView.toScreen(0, -18, -5));
  await page.mouse.click(x, y);
  await page.waitForTimeout(400);
  const pf = await page.evaluate(() => window.vibecadView.pickedFace());
  check("clicking the reference picks its face", pf?.labels?.[0]?.startsWith("nema171.face[f"), JSON.stringify(pf?.labels));
  check("no hole into a reference face", await page.isDisabled("#holeBtn"));
  await page.click("#newSketchBtn");
  await page.click("#newMenu [data-face]");
  await page.waitForSelector("#sketchBar:not([hidden])", { timeout: 10000 });
  await page.waitForTimeout(800);
  const fr = await page.evaluate(() => window.vibecadSketch.data().frame);
  check("a sketch on the motor's face sits on it", Math.abs(fr.origin[2] + 5) < 1e-6 && fr.normal[2] === 1, JSON.stringify(fr));
  await page.click("#sketchTools [data-act=project]");
  await page.waitForTimeout(1500);
  const ext = await page.evaluate(() => window.vibecadSketch.data().entities.filter((e) => e.external).length);
  check("Project brings the motor face's edges into the sketch", ext >= 8, `${ext}`);
  await shot("2_sketch_on_reference");

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
