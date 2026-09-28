// Screenshots for README.md (docs/img/), taken from the running GUI. Not a test: run through
//   ONLY=readme_shots scripts/gui_smoke.sh docs/img
// Usage: node readme_shots.js <base_url> <agent_base_url> <out_dir> [three_pkg_dir]
const { chromium } = require("playwright");
const path = require("path");

const [BASE, AGENT, OUT, THREE] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 860 } });
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `${n}.png`) });
  const open = async (base, part) => {
    await page.goto(base);
    await page.evaluate(() => { try { localStorage.clear(); } catch {} });
    await page.waitForSelector("#conn.live", { timeout: 15000 });
    if (part) {
      await page.selectOption("#partSelect", part);
      await sleep(2500);
    }
  };
  const caret = (id) => page.click(`#tree li.feat[data-id=${id}] .caret`);
  const world = (x, y, z) => page.evaluate(([x, y, z]) => window.vibecadView.toScreen(x, y, z), [x, y, z]);

  // 1. the whole app: tree with a feature's own parameters open, the selected feature highlighted
  await open(BASE, "pillow_block.vcad.json");
  await caret("housing_sketch");
  await caret("pocket_cut");
  await page.click("#tree li.feat[data-id=pocket_cut] .fid");
  await sleep(1200);
  await page.mouse.move(1000, 850);
  await shot("overview");

  // 2. the sketch editor: CAD-style dimensions, constraint glyphs, icon palette
  await page.click("#tree li.feat[data-id=housing_sketch] .fid");
  await page.waitForSelector("#sketchBar:not([hidden])");
  await sleep(1500);
  await page.mouse.move(700, 450);
  await shot("sketch");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await sleep(500);

  // 3. edges picked in the view, fillet form open
  await open(BASE, "l_bracket.vcad.json");
  await page.click("#fitBtn");
  await sleep(800);
  let [x, y] = await world(30, 0, 50);
  await page.mouse.click(x, y);
  await sleep(700);
  [x, y] = await world(30, 5, 50);
  await page.keyboard.down("Shift");
  await page.mouse.click(x, y);
  await page.keyboard.up("Shift");
  await sleep(700);
  await page.click("#filletBtn");
  await page.fill("#ffSize", "2");
  await sleep(300);
  await shot("edges");
  await page.keyboard.press("Escape");

  // 4. the agent panel: a run's tool calls, and a message with references to an edge and a face
  await open(AGENT);
  await page.fill("#prompt", "Make a 60 x 40 plate");
  await page.press("#prompt", "Enter");
  await page.waitForFunction(() => document.querySelector("#metrics").textContent.includes("last run"), null, { timeout: 60000 });
  await sleep(1500);
  await page.click("#prompt");
  await page.keyboard.type("Round ");
  await page.click("#refBtn");
  [x, y] = await world(10, -20, 4);
  await page.mouse.click(x, y);
  await page.waitForFunction(() => document.querySelectorAll("#prompt .ref").length === 1, null, { timeout: 5000 });
  await page.keyboard.type("with a 2 mm fillet, and put an M4 tapped hole in the middle of ");
  await page.click("#refBtn");
  [x, y] = await world(15, 8, 4);
  await page.mouse.move(x, y);
  await sleep(200);
  await page.mouse.click(x, y);
  await sleep(500);
  await page.mouse.move(1000, 850);
  await sleep(300);
  await shot("agent");

  // 5. real parts: import a phone as a solid, shell it outward with the screen open (a case), section it
  let partName = "";
  page.on("dialog", (d) => d.accept(partName));
  const FIX = process.env.FIXTURES;
  const idle = async () => { await page.waitForFunction(() => document.querySelector("#rebuild").hidden, null, { timeout: 60000 }); await sleep(900); };
  await open(BASE);
  await page.click("#newBtn");
  await page.fill("#npName", "case_demo");
  await page.click("#npCreate");
  await sleep(1200);
  await page.setInputFiles("#importFile", path.join(FIX, "phone.step"));
  await page.waitForSelector("#imGo", { timeout: 30000 });
  await page.selectOption("#imMode", "new");
  await page.selectOption("#imPlace", "origin");
  await sleep(300);
  await shot("import");
  await page.click("#imGo");
  await idle();
  await page.click(".vtools [data-view=iso]"); await sleep(300);
  await page.click("#gizmo .ax[data-ax=Z][data-sgn='-1']"); await sleep(400);
  [x, y] = await world(-20, -40, 0);
  await page.mouse.click(x, y); await sleep(400);
  await page.click("#shellBtn");
  await page.selectOption("#shDir", "out");
  await page.fill("#shT", "1.6");
  await page.click("#shGo");
  await idle();

  // 6. holes: click the plate's top, M5 counterbore
  await open(BASE, "mounting_plate.vcad.json");
  await page.click(".vtools [data-view=iso]"); await sleep(400);
  [x, y] = await world(-20, 15, 8);
  await page.mouse.click(x, y); await sleep(400);
  await page.click("#holeBtn");
  await page.selectOption("#hoKind", "counterbore");
  await page.selectOption("#hoScrew", "M5");
  await sleep(300);
  await shot("holes");
  await page.keyboard.press("Escape");
  await page.mouse.click(1000, 850);

  // section through two counterbores and the countersinks' row: their profiles in the cut
  await page.click("#sectionBtn");
  await page.evaluate(() => { const r = document.querySelector("#secT"); r.value = 8 / 60; r.dispatchEvent(new Event("input")); });
  await sleep(700);
  await page.mouse.move(1000, 850);
  await shot("section");
  await page.click("#sectionBtn");

  // 7. measure: two counterbores' axes, and the part's mass
  await page.click("#measureBtn");
  for (const [px, py] of [[-32, -22], [32, -22]]) {  // the far inside wall of each counterbore, as seen from the iso view
    [x, y] = await world(px - 4 * 0.7071, py + 4 * 0.7071, 6);
    await page.mouse.click(x, y); await sleep(900);
  }
  await page.mouse.move(1000, 850);
  await sleep(600);
  await shot("measure");
  await page.keyboard.press("Escape");

  // 8. loft: the duct adapter, with the inside loft's form open on its two inset sections
  await open(BASE, "duct_adapter.vcad.json");
  await page.dblclick("#tree li.feat[data-id=bore] .fid");
  await page.waitForSelector("#efGo");
  await sleep(800);
  await page.mouse.move(1000, 850);
  await shot("loft");
  await page.keyboard.press("Escape");

  // 9. sweep: a bent tube, a ring along a line-arc-line centreline
  await open(BASE, "bent_tube.vcad.json");
  await page.click("#tree li.feat[data-id=tube] .fid");
  await sleep(1200);
  await page.mouse.move(1000, 850);
  await shot("sweep");

  // 10. the SVG drawing export of the mounting plate, rendered on its own
  await open(BASE, "mounting_plate.vcad.json");
  const svg = await page.evaluate(() => fetch("/api/export?fmt=svg").then((r) => r.text()));
  const dp = await browser.newPage({ viewport: { width: 1400, height: 990 } });
  await dp.setContent(`<body style="margin:0;background:#fff">${svg.replace(/width="[\d.]+mm" height="[\d.]+mm"/, 'width="1400" height="990"')}</body>`);
  await sleep(400);
  await dp.screenshot({ path: path.join(OUT, "drawing.png") });

  await browser.close();
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
