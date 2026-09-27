// Browser test: working on real parts — importing CAD files (STEP / STL, reference or solid, units, placement),
// holes (clicked on a face, or at sketch points), pattern / mirror / shell, measure, section, export, the view gizmo.
// Also opens every part in USERPARTS (a folder of .vcad.json, optional) and edits one parameter of each.
// Usage: node realparts.js <base_url> <screenshot_dir> <fixtures_dir> [three_pkg_dir]
const { chromium } = require("playwright");
const path = require("path");

const [BASE, OUT, FIX, THREE] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };
const near = (a, b, tol) => a != null && b != null && Math.abs(a - b) <= tol;

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 }, acceptDownloads: true });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  let partName = "";
  page.on("dialog", (d) => d.accept(partName));
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `real_${n}.png`) });
  const state = () => page.evaluate(() => fetch("/api/state").then((r) => r.json()).then((s) => s.state));
  const vol = async () => (await state())?.volume;
  const idle = async () => {
    await page.waitForFunction(() => document.querySelector("#rebuild").hidden, null, { timeout: 60000 });
    await page.waitForTimeout(700);
  };
  const open = async (p) => {
    await page.selectOption("#partSelect", p);
    await page.waitForFunction((p) => document.querySelector("#partName").textContent.includes(p.split("/").pop()), p, { timeout: 30000 });
    await idle();
    await page.waitForTimeout(800);
  };
  const newPart = async (name) => {
    partName = name;
    await page.click("#newBtn");
    await page.waitForFunction((n) => document.querySelector("#partName").textContent.includes(n), name, { timeout: 15000 });
    await page.waitForTimeout(500);
  };
  const click3d = async (x, y, z, opts = {}) => {
    const [px, py] = await page.evaluate(([x, y, z]) => window.vibecadView.toScreen(x, y, z), [x, y, z]);
    if (opts.shift) await page.keyboard.down("Shift");
    await page.mouse.click(px, py);
    if (opts.shift) await page.keyboard.up("Shift");
    await page.waitForTimeout(400);
  };
  const importFile = async (file, { mode, units, place } = {}) => {
    await page.setInputFiles("#importFile", path.join(FIX, file));
    await page.waitForSelector("#imGo", { timeout: 30000 });
    if (mode) await page.selectOption("#imMode", mode);
    if (units) await page.selectOption("#imUnits", units);
    if (place) await page.selectOption("#imPlace", place);
    const size = await page.textContent("#imSize");
    await page.click("#imGo");
    await idle();
    return size;
  };
  const treeIds = () => page.$$eval("#tree li.feat", (l) => l.map((x) => x.dataset.id));

  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 20000 });

  // ── the ribbon: every tool has an icon and a label ──
  const labels = await page.$$eval("#modelTools button.rb", (l) => l.map((b) => [b.querySelector("svg") ? 1 : 0, b.textContent.trim()]));
  check("ribbon buttons have an icon and a label", labels.length >= 13 && labels.every(([i, t]) => i && t), JSON.stringify(labels.map((x) => x[1])));

  // ── import a phone as a reference body, then design a case plate on it ──
  await newPart("case_test");
  check("Hole, Shell, Export are off with no solid", await page.isDisabled("#holeBtn") && await page.isDisabled("#shellBtn") && await page.isDisabled("#exportBtn"));
  const phoneSize = await importFile("phone.step", { mode: "reference", place: "origin" });
  check("import form shows the size in the part", phoneSize.includes("71.5 × 146.7 × 9"), phoneSize);
  let st = await state();
  check("reference import adds a feature but no solid", st.features.some((f) => f.type === "import" && f.status === "ok") && st.volume == null,
    JSON.stringify(st.features.map((f) => [f.id, f.status, f.message])));
  check("the reference body is drawn", (await page.evaluate(() => window.vibecadView.refs())).length === 1);
  const ref = (await page.evaluate(() => window.vibecadView.refs()))[0];
  const imp = st.features.find((f) => f.type === "import");
  const fj = await page.evaluate((id) => fetch(`/api/feature/${id}`).then((r) => r.json()), imp.id);
  check("placed on the origin: centred in X/Y and on XY", JSON.stringify(fj.translate) === JSON.stringify([-200, -100, -30]), JSON.stringify(fj.translate));
  await shot("1_reference");
  // sketch on the reference's back face, extrude a plate under it
  await page.click(".vtools [data-view=iso]");
  await page.waitForTimeout(400);
  await page.click("#gizmo .ax[data-ax=Z][data-sgn='-1']");  // look up from below
  await page.waitForTimeout(500);
  const dir = await page.evaluate(() => window.vibecadView.viewDir());
  check("gizmo -Z looks up the Z axis", near(dir[2], 1, 1e-3), JSON.stringify(dir));
  await click3d(0, 0, 0);
  const picked = await page.evaluate(() => window.vibecadView.pickedFace());
  check("a reference face can be picked", picked && picked.labels[0].startsWith(`${ref}.face[`), JSON.stringify(picked));
  await page.click("#newSketchBtn");
  await page.click("#newMenu [data-face]");
  await page.waitForSelector("#sketchBar:not([hidden])", { timeout: 15000 });
  await idle();
  await page.keyboard.press("r");
  const sk = (fn, a) => page.evaluate(([src, a]) => new Function("sk", "a", `return (${src})(sk, a)`)(window.vibecadSketch, a), [fn.toString(), a]);
  const scr = (u, v) => sk((s, a) => s.toScreen(a[0], a[1]), [u, v]);
  for (const [u, v] of [[-30, -60], [30, 60]]) { const [x, y] = await scr(u, v); await page.mouse.click(x, y); await page.waitForTimeout(700); }
  await page.waitForTimeout(500);
  const nEnt = await sk((s) => s.data()?.entities.length);
  if (nEnt !== 4) { await shot("debug_rect"); console.log("sketch", await page.textContent("#sketchInfo"), await page.textContent("#log")); }
  check("a rectangle drawn on the reference face", nEnt === 4, String(nEnt));
  await page.click("#extrudeBtn");
  await page.fill("#ffDist", "2");
  await page.selectOption("#ffMode", "new");
  await page.click("#ffGo");
  await idle();
  check("plate extruded from the reference's face", near(await vol(), 60 * 120 * 2, 1), `${await vol()}`);
  const bb = (await state()).bbox;
  check("the plate sits against the phone's back (outside it, below z = 0)", bb && near(bb[2], 2, 1e-6), JSON.stringify(bb));
  await page.click(".vtools [data-view=iso]");
  await page.waitForTimeout(500);
  await shot("2_case_plate");

  // ── an STL (a mesh) sewn into a solid: a new body the size of the STEP version ──
  await importFile("phone.stl", { mode: "new", place: "origin" });
  st = await state();
  const stl = st.features.at(-1);
  check("STL imports as a solid body", stl.type === "import" && stl.status === "ok" && !stl.warnings?.length, JSON.stringify([stl.status, stl.message, stl.warnings]));
  check("its volume matches the STEP's within the mesh tolerance", near((await vol()) - 14400, 82046.4, 82046.4 * 0.01), `${(await vol()) - 14400}`);

  // ── units: an inch model comes in at the right size ──
  await newPart("inch_test");
  const inSize = await importFile("block_inches.step", { units: "in" });
  check("the units guess says inches for a 2 in block", inSize.includes("50.8 × 38.1 × 12.7"), inSize);
  check("scaled to mm", near(await vol(), 2 * 1.5 * 0.5 * 25.4 ** 3, 0.5), `${await vol()}`);

  // ── holes: click a face, choose a screw; volumes as the formulas say ──
  await page.click(".vtools [data-view=iso]");
  await page.waitForTimeout(400);
  const v0 = await vol();
  await click3d(25.4, 19.05, 12.7);  // middle of the top face
  await page.click("#holeBtn");
  await page.selectOption("#hoKind", "counterbore");
  await page.selectOption("#hoScrew", "M5");
  check("screw preset fills the sizes", (await page.inputValue("#hoD")) === "5.5" && (await page.inputValue("#hoCbD")) === "9.5");
  await page.click("#hoGo");
  await idle();
  const cb = Math.PI * 4.75 ** 2 * 5.4 + Math.PI * 2.75 ** 2 * (12.7 - 5.4);
  check("M5 counterbore removes what it should", near(v0 - (await vol()), cb, 0.05), `${v0} -> ${await vol()} (expected -${cb.toFixed(2)})`);
  const ids = await treeIds();
  check("the hole's position sketch and feature are in the tree", ids.includes("hole1_at") && ids.includes("hole1"), ids.join());
  const hj = await page.evaluate(() => fetch("/api/feature/hole1_at").then((r) => r.json()));
  check("the position is dimensioned from the origin", hj.constraints.some((c) => c.name === "hole1_x"), JSON.stringify(hj.constraints));
  // tapped blind hole
  const v1 = await vol();
  await click3d(10, 10, 12.7);
  await page.click("#holeBtn");
  await page.selectOption("#hoKind", "tapped");
  await page.selectOption("#hoScrew", "M4");
  check("tapped defaults to blind with the tap drill", (await page.inputValue("#hoD")) === "3.3" && (await page.inputValue("#hoExt")) === "blind");
  await page.fill("#hoDepth", "8");
  await page.click("#hoGo");
  await idle();
  const tr = 1.65, tap = Math.PI * tr * tr * 8 + Math.PI * tr * tr * (tr / Math.tan((59 * Math.PI) / 180)) / 3;
  check("M4 tapped blind hole with drill point", near(v1 - (await vol()), tap, 0.05), `${v1} -> ${await vol()} (expected -${tap.toFixed(3)})`);
  const tj = await page.evaluate(() => fetch("/api/feature/hole2").then((r) => r.json()));
  check("thread recorded", tj.thread === "M4x0.7", JSON.stringify(tj));

  // ── pattern the tapped hole, then mirror it ──
  const v2 = await vol();
  await page.click("#tree li.feat[data-id=hole2] .fid");
  await page.click("#patternBtn");
  check("pattern form ticks the selected feature", await page.isChecked("#featMenu .checks input[value=hole2]"));
  await page.selectOption("#paDir", "X");
  await page.fill("#paSp", "8");
  await page.fill("#paN", "3");
  await page.click("#paGo");
  await idle();
  check("pattern: two more tapped holes", near(v2 - (await vol()), 2 * tap, 0.1), `${v2} -> ${await vol()}`);
  const v3 = await vol();
  await page.click("#mirrorBtn");
  await page.selectOption("#miPl", "XZ");
  check("mirror asks where the plane is in world terms", (await page.textContent("#miAtL")) === "at Y =");
  await page.fill("#miOff", "19.05");
  await page.click("#miGo");
  await idle();
  check("mirror across the block's middle: one more hole", near(v3 - (await vol()), tap, 0.1), `${v3} -> ${await vol()}`);

  // ── edit a feature in its own form: double-click it in the tree ──
  const v5 = await vol();
  await page.dblclick("#tree li.feat[data-id=hole2] .fid");
  await page.waitForSelector("#efGo", { timeout: 10000 });
  check("double-click opens the hole's settings", (await page.textContent("#featMenu .ttl")).includes("hole2") && (await page.inputValue("#efDepth")) === "8");
  await page.fill("#efDepth", "10");
  await page.click("#efGo");
  await idle();
  // four tapped holes now (the original, two pattern copies, the mirror): each 2 mm deeper
  check("deeper tapped holes, patterned and mirrored copies too", near(v5 - (await vol()), 4 * Math.PI * 1.65 ** 2 * 2, 0.05), `${v5} -> ${await vol()}`);
  await page.click("#tree li.feat[data-id=pattern1] .fid");
  await page.waitForSelector("#details .editbtn");
  await page.click("#details .editbtn");
  await page.waitForSelector("#efGo");
  await page.fill("#efN", "2");
  const v6 = await vol();
  await page.click("#efGo");
  await idle();
  check("pattern count 3 → 2 via Edit", near((await vol()) - v6, Math.PI * 1.65 ** 2 * 10 + Math.PI * 1.65 ** 2 * (1.65 / Math.tan((59 * Math.PI) / 180)) / 3, 0.05), `${v6} -> ${await vol()}`);
  await page.click("#tree li.feat[data-id=block_inches1] .fid");
  const impEdit = await page.waitForFunction(() => document.querySelector("#details .editbtn")?.textContent.includes("Edit import"), null, { timeout: 10000 }).then(() => true, () => false);
  check("an import's settings are editable too", impEdit);

  // ── measure ──
  await page.click("#measureBtn");
  check("measure mode on", await page.evaluate(() => window.vibecadView.measuring()));
  await page.click(".vtools [data-view=front]");
  await page.waitForTimeout(400);
  await page.click(".vtools [data-view=iso]");
  await page.waitForTimeout(400);
  await click3d(40, 30, 12.7);  // top
  await click3d(50.8, 20, 5);   // right side
  await page.waitForTimeout(800);
  let mt = await page.textContent("#measurePanel");
  check("two faces: angle 90°", mt.includes("90°"), mt.slice(0, 300));
  await page.click("#measureBtn"); await page.click("#measureBtn");  // start over
  await click3d(4, 34, 12.7);  // top, back left: clear of the measure panel
  await page.click(".vtools [data-view=iso]"); await page.waitForTimeout(300);
  await page.click("#gizmo .ax[data-ax=Z][data-sgn='-1']"); await page.waitForTimeout(400);
  check("first pick kept", (await page.evaluate(() => window.vibecadView.measurePicks())).length === 1);
  await click3d(40, 30, 0);  // bottom, seen from below
  await page.waitForTimeout(900);
  mt = await page.textContent("#measurePanel");
  check("top to bottom: 12.7 mm apart", mt.includes("12.7 mm"), mt.slice(0, 300));
  check("part mass needs a material", mt.includes("Set a material"), mt.slice(-200));
  await shot("3_measure");
  await page.keyboard.press("Escape");
  check("Esc leaves measure mode", !(await page.evaluate(() => window.vibecadView.measuring())));

  // ── section ──
  await page.click(".vtools [data-view=iso]");
  await page.click("#sectionBtn");
  await page.waitForTimeout(300);
  let clip = await page.evaluate(() => window.vibecadView.clip());
  check("section on: a Y plane through the middle", clip.length === 1 && clip[0].normal[1] === 1 && near(-clip[0].constant, 19.05, 0.01), JSON.stringify(clip));
  await page.click("#sectionPanel [data-ax=X]");
  await page.evaluate(() => { const r = document.querySelector("#secT"); r.value = 0.25; r.dispatchEvent(new Event("input")); });
  clip = await page.evaluate(() => window.vibecadView.clip());
  check("X plane at a quarter", clip[0].normal[0] === 1 && near(-clip[0].constant, 12.7, 0.01), JSON.stringify(clip));
  await shot("4_section");
  await page.click("#sectionBtn");
  check("section off", (await page.evaluate(() => window.vibecadView.clip())).length === 0);

  // ── export ──
  for (const fmt of ["step", "stl", "3mf"]) {
    await page.click("#exportBtn");
    const [dl] = await Promise.all([page.waitForEvent("download", { timeout: 60000 }), page.click(`#featMenu a[data-fmt="${fmt}"]`)]);
    const p = await dl.path();
    const size = require("fs").statSync(p).size;
    const head = require("fs").readFileSync(p).slice(0, 20).toString();
    check(`export ${fmt}`, dl.suggestedFilename() === `inch_test.${fmt}` && size > 1000 && (fmt !== "step" || head.startsWith("ISO-10303-21")), `${dl.suggestedFilename()} ${size} B`);
  }

  // ── shell an imported block with its top open (shell before drilling: OCCT can't offset through holes) ──
  await newPart("shell_test");
  await importFile("block_inches.step", { units: "in" });
  await page.click(".vtools [data-view=iso]");
  await page.waitForTimeout(400);
  await click3d(25, 19, 12.7);
  await page.click("#shellBtn");
  check("shell form names the open face", (await page.textContent("#featMenu")).includes("Opens"));
  await page.fill("#shT", "2");
  await page.click("#shGo");
  await idle();
  st = await state();
  check("shell builds", st.features.at(-1).type === "shell" && st.features.at(-1).status === "ok", JSON.stringify(st.features.at(-1).message));
  const shellV = 50.8 * 38.1 * 12.7 - 46.8 * 34.1 * 10.7;
  check("shell leaves 2 mm walls and floor", near(await vol(), shellV, 0.5), `${await vol()} (expected ${shellV.toFixed(1)})`);

  // ── imported example as a base solid ──
  await newPart("reuse_test");
  await importFile("pillow_block.step");
  const pv = await vol();
  check("a STEP of a real part imports as the base solid", near(pv, 29540.964, 0.5), `${pv}`);

  // ── the user's own parts (if given): open each, edit a parameter, undo ──
  const parts = await page.$$eval("#partSelect option", (l) => l.map((o) => o.value).filter((v) => v.startsWith("userparts/")));
  for (const p of parts) {
    await open(p);
    st = await state();
    const errs = st.features.filter((f) => f.status === "error");
    check(`${p} opens without errors`, !errs.length && st.volume > 0, errs.map((f) => `${f.id}: ${f.message}`).join("; "));
    const prm = st.params.find((x) => typeof x.value === "number" && x.value > 1 && x.users.length);
    if (!prm) continue;
    const before = st.volume;
    const inp = page.locator(`#params tr[data-param="${prm.name}"] input`), want = String(+(prm.value * 1.1).toFixed(3));
    await inp.fill(want);
    await inp.press("Enter");
    let t = 0;
    // the new expression shows before the rebuild finishes: wait for the evaluated value
    for (; t < 240 && !near((await state()).params.find((x) => x.name === prm.name).value, +want, 1e-9); t++) await page.waitForTimeout(250);
    await idle();
    st = await state();
    if (Math.abs(st.volume - before) < 1e-6) console.log("  debug:", t, want, JSON.stringify(st.params.find((x) => x.name === prm.name)), await page.$$eval("#log .msg", (l) => l.slice(-3).map((x) => x.textContent)));
    check(`${p}: ${prm.name} +10% rebuilds and changes the part`, st.features.every((f) => f.status !== "error") && st.volume > 0 && Math.abs(st.volume - before) > 1e-6,
      `${before} -> ${st.volume}`);
    await page.evaluate(() => document.activeElement.blur());
    await page.keyboard.press("Control+z");
    for (let t = 0; t < 240 && !near((await state()).volume, before, 1e-3); t++) await page.waitForTimeout(250);
    await idle();
    check(`${p}: undo restores it`, near((await state()).volume, before, 1e-3));
    await shot(`user_${p.split("/").pop().replace(/\W+/g, "_")}`);
  }

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
