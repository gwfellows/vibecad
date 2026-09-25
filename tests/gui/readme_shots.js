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

  await browser.close();
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
