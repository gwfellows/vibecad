// Browser test: model a part by hand, from an empty file to a solid, using only the GUI.
// Usage: node modeling.js <base_url> <screenshot_dir> [three_pkg_dir]
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
  const shot = (n) => page.screenshot({ path: path.join(OUT, `model_${n}.png`) });
  const settle = () => page.waitForTimeout(700);
  const SK = (fn, arg) => page.evaluate(([src, a]) => new Function("sk", "a", `return (${src})(sk, a)`)(window.vibecadSketch, a), [fn.toString(), arg]);
  const info = async () => (await page.textContent("#sketchInfo")).trim();
  const vol = async () => { const m = (await page.textContent("#partStatus")).match(/([\d.]+) mm³/); return m ? +m[1] : null; };
  const tree = () => page.$$eval("#tree li.feat .fid", (l) => l.map((x) => x.textContent));
  const at = async (u, v, shift = false) => {
    const [x, y] = await SK((sk, a) => sk.toScreen(a[0], a[1]), [u, v]);
    await page.mouse.move(x, y);
    if (shift) await page.keyboard.down("Shift");
    await page.mouse.click(x, y);
    if (shift) await page.keyboard.up("Shift");
    await page.waitForTimeout(350);
  };
  const mid = (id) => SK((sk, id) => { const e = sk.data().entities.find((x) => x.id === id); return [(e.p1[0] + e.p2[0]) / 2, (e.p1[1] + e.p2[1]) / 2]; }, id);
  const pt = (ref) => SK((sk, r) => sk.data().points.find((p) => p.ref === r).at, ref);
  const atRef = async (where, shift = false) => at(...where, shift);
  const dim = async (key, value) => {
    await page.keyboard.press(key);
    await page.waitForSelector("#dimEdit:not([hidden])", { timeout: 3000 });
    await page.fill("#dimEdit input", String(value));
    await page.press("#dimEdit input", "Enter");
    await settle();
  };
  const dimTool = async (con, value) => {
    await page.click(`#sketchTools [data-con=${con}]`);
    await page.waitForSelector("#dimEdit:not([hidden])", { timeout: 3000 });
    await page.fill("#dimEdit input", String(value));
    await page.press("#dimEdit input", "Enter");
    await settle();
  };
  const newSketch = async (choice) => {
    await page.click("#newSketchBtn");
    await page.click(`#newMenu ${choice}`);
    await page.waitForSelector("#sketchBar:not([hidden])", { timeout: 5000 });
    await settle();
  };
  const feature = async (btn, fill) => {
    await page.click(btn);
    await page.waitForSelector("#featMenu:not([hidden])", { timeout: 3000 });
    await fill();
    await page.click("#ffGo");
    await page.waitForTimeout(1500);
  };

  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  page.once("dialog", (d) => d.accept("handmade"));
  await page.click("#newBtn");
  await page.waitForFunction(() => document.querySelector("#partName").textContent.includes("handmade"), null, { timeout: 10000 });
  check("new empty part", (await tree()).length === 0);
  await page.click("#newSketchBtn");
  check("face option disabled until a face is picked", await page.isDisabled("#newMenu [data-face]"));
  await page.keyboard.press("Escape");
  await page.mouse.click(700, 500);

  // base plate: 40 x 20 rectangle at the origin, extruded 5
  await newSketch("[data-d=XY]");
  check("+ Sketch on XY opens the new sketch", (await SK((sk) => sk.active())) === "sketch1");
  await page.keyboard.press("r");
  await at(-10, -5);
  await at(20, 15);
  await page.keyboard.press("Escape");
  await atRef(await mid("rect1_bottom")); await dim("d", 40);
  await atRef(await mid("rect1_left")); await dim("d", 20);
  await atRef(await pt("rect1_bottom.p1")); await at(0, 0, true);
  await page.click("#sketchTools [data-con=coincident]");
  await settle();
  check("plate sketch fully constrained", /0 DOF/.test(await info()), await info());
  await feature("#extrudeBtn", async () => {
    check("first extrude defaults to a new body", (await page.inputValue("#ffMode")) === "new");
    await page.fill("#ffDist", "5");
  });
  check("extrude leaves the sketch", !(await page.isVisible("#sketchBar")));
  check("plate volume 40 x 20 x 5", Math.abs((await vol()) - 4000) < 0.5, `${await vol()}`);
  check("tree: sketch1, extrude1", (await tree()).join() === "sketch1,extrude1", (await tree()).join());
  await page.waitForTimeout(800);
  await shot("1_plate");

  // hole: sketch on the picked top face, circle dimensioned to the centre, cut through
  const [tx, ty] = await page.evaluate(() => window.vibecadView.toScreen(20, 10, 5));
  await page.mouse.click(tx, ty);
  await page.waitForTimeout(300);
  const pf = await page.evaluate(() => window.vibecadView.pickedFace());
  check("clicking the top face picks it", pf?.labels?.[0] === "extrude1.end", JSON.stringify(pf?.labels));
  await newSketch("[data-face]");
  check("sketch on the picked face", (await SK((sk) => sk.active())) === "sketch2");
  const fr = await SK((sk) => sk.data().frame);
  check("face sketch sits on the top face", Math.abs(fr.origin[2] - 5) < 1e-6 && fr.normal[2] === 1, JSON.stringify(fr));
  await page.keyboard.press("c");
  await at(18, 9);
  await at(21, 9);
  await page.keyboard.press("Escape");
  await atRef(await SK((sk) => { const c = sk.data().entities.find((e) => e.id === "circle1"); return [c.center[0], c.center[1] + c.r]; }));
  await dim("d", 6);
  await atRef(await pt("circle1.center")); await dimTool("dx", 20);
  await atRef(await pt("circle1.center")); await dimTool("dy", 10);
  check("hole sketch fully constrained", /0 DOF/.test(await info()), await info());
  await feature("#extrudeBtn", async () => {
    await page.check("#ffThru");
    await page.selectOption("#ffDir", "reverse");
    await page.selectOption("#ffMode", "cut");
  });
  const expect2 = 4000 - Math.PI * 9 * 5;
  check("hole cut through the plate", Math.abs((await vol()) - expect2) < 0.5, `${await vol()} vs ${expect2.toFixed(1)}`);
  await shot("2_hole");

  // ring: rectangle on XZ (sketch y = world Z) revolved about the sketch y axis, above the plate
  await newSketch("[data-d=XZ]");
  await page.keyboard.press("r");
  await at(25, 10); await at(29, 18);
  await page.keyboard.press("Escape");
  await feature("#revolveBtn", async () => {
    check("revolve offers the sketch lines as axes", (await page.$$eval("#ffAxis option", (o) => o.map((x) => x.textContent))).includes("rect1_left"));
    await page.selectOption("#ffAxis", "y_axis");
  });
  const ring = Math.PI * (29 * 29 - 25 * 25) * 8;
  await page.waitForTimeout(800);
  check("sketch overlay cleared after leaving the sketch", (await page.$$eval("#labels .lbl", (l) => l.length)) === 0);
  check("revolved ring adds its volume", Math.abs((await vol()) - expect2 - ring) < 1, `${await vol()} vs ${(expect2 + ring).toFixed(1)}`);
  check("tree holds every step", (await tree()).join() === "sketch1,extrude1,sketch2,extrude2,sketch3,revolve1", (await tree()).join());
  await page.click("#fitBtn");
  await page.waitForTimeout(800);
  await shot("3_ring");

  // new sketches go at the rollback bar
  await page.keyboard.press("Control+z");
  await settle();
  check("undo removes the revolve", !(await tree()).includes("revolve1"));
  const saved = await page.evaluate(() => fetch("/api/feature/extrude2").then((r) => r.json()));
  check("face reference saved semantically with a note", saved.profile?.sketch === "sketch2" &&
    JSON.stringify(await page.evaluate(() => fetch("/api/feature/sketch2").then((r) => r.json()))).includes('"feature":"extrude1","role":"end"'));

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
