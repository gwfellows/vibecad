// Browser smoke test for vibecad-app: open, param edit, undo/redo, sketch mode, feature JSON edit, bad input,
// rollback bar, history, renders, part switch, new part. Run it with scripts/gui_smoke.sh.
// Usage: node smoke.js <base_url> <screenshot_dir> [three_pkg_dir]  (three_pkg_dir: serve three.js locally
// when the CDN is unreachable)
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [BASE, OUT, THREE] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok, detail }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const consoleErrors = [], pageErrors = [], badResponses = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`); });
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) => {
    const rel = route.request().url().split("three@0.170.0/")[1];
    route.fulfill({ path: path.join(THREE, rel), contentType: "application/javascript" });
  });
  const shot = (n) => page.screenshot({ path: path.join(OUT, `${n}.png`) });
  const status = () => page.textContent("#partStatus");
  const vol = async () => { const m = (await status()).match(/([\d.]+) mm³/); return m ? +m[1] : null; };

  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 }).catch(() => {});
  check("websocket connects", (await page.textContent("#conn")) === "connected");
  const opts = await page.$$eval("#partSelect option", (os) => os.map((o) => o.textContent));
  check("part list loads", opts.length >= 9, `${opts.length - 1} parts`);

  // open a part
  await page.selectOption("#partSelect", "l_bracket.vcad.json");
  await page.waitForFunction(() => document.querySelectorAll("#tree li.feat").length > 0, null, { timeout: 20000 });
  const nFeat = await page.$$eval("#tree li.feat", (l) => l.length);
  check("open l_bracket: tree shows 10 features", nFeat === 10, `${nFeat}`);
  check("status ok", (await status()).includes("ok"), await status());
  await sleep(1500);
  const v0 = await vol();
  check("volume shown", Math.abs(v0 - 24486.6) < 1, `${v0}`);
  const canvasOk = await page.evaluate(() => { const c = document.querySelector("#viewer canvas"); return c && c.width > 100; });
  check("3D canvas present", canvasOk);
  await shot("01_open");

  // edit a param through the table
  const widthInput = page.locator("#params tr", { hasText: "width" }).first().locator("input");
  await widthInput.fill("70");
  await widthInput.press("Enter");
  await page.waitForFunction((v) => { const m = document.querySelector("#partStatus").textContent.match(/([\d.]+) mm³/); return m && +m[1] !== v; }, v0, { timeout: 15000 }).catch(() => {});
  const v1 = await vol();
  check("param edit changes volume", v1 > v0, `${v0} -> ${v1}`);
  check("undo enabled after edit", !(await page.isDisabled("#undoBtn")));
  await shot("02_param_edit");

  // undo / redo buttons
  await page.click("#undoBtn");
  await page.waitForFunction((v) => document.querySelector("#partStatus").textContent.includes(String(v)), v0.toFixed(1).replace(/\.0$/, ""), { timeout: 15000 }).catch(() => {});
  check("undo restores volume", Math.abs((await vol()) - v0) < 0.2, `${await vol()}`);
  const widthAfterUndo = await widthInput.inputValue();
  check("param table shows undone value", widthAfterUndo !== "70", widthAfterUndo);
  await page.click("#redoBtn");
  await sleep(1500);
  check("redo re-applies", Math.abs((await vol()) - v1) < 0.2, `${await vol()}`);
  // keyboard undo
  await page.click("#viewer");
  await page.keyboard.press("Control+z");
  await sleep(1500);
  check("Ctrl+Z undoes", Math.abs((await vol()) - v0) < 0.2, `${await vol()}`);

  // sketch mode
  await page.click("#tree li.feat >> text=slot_sketch");
  await page.waitForSelector("#sketchBar:not([hidden])", { timeout: 10000 }).catch(() => {});
  check("click sketch enters sketch mode", await page.isVisible("#sketchBar"));
  await sleep(800);
  const info = await page.textContent("#sketchInfo");
  check("sketch bar shows DOF", /0 DOF/.test(info), info);
  const nLabels = await page.$$eval("#labels .lbl", (l) => l.length);
  check("sketch labels drawn", nLabels > 0, `${nLabels}`);
  await shot("03_sketch_mode");
  await page.keyboard.press("Escape");
  await sleep(300);
  check("Escape exits sketch mode", !(await page.isVisible("#sketchBar")));
  check("labels cleared after exit", (await page.$$eval("#labels .lbl", (l) => l.length)) === 0);

  // feature JSON edit
  await page.click("#tree li.feat >> text=corner_fillet");
  await page.waitForSelector("#details textarea", { timeout: 10000 });
  const txt = await page.inputValue("#details textarea");
  const j = JSON.parse(txt);
  check("details shows feature JSON", j.id === "corner_fillet", j.type);
  j.radius = 6;
  await page.fill("#details textarea", JSON.stringify(j, null, 1));
  const vBefore = await vol();
  await page.click("#applyFeat");
  await sleep(2000);
  check("apply feature edit changes volume", (await vol()) !== vBefore, `${vBefore} -> ${await vol()}`);
  await shot("04_feature_edit");

  // bad JSON in the feature editor shows an error, no crash
  await page.click("#tree li.feat >> text=corner_fillet"); // deselect
  await page.click("#tree li.feat >> text=corner_fillet"); // reselect
  await page.waitForSelector("#details textarea");
  await page.fill("#details textarea", "{ not json");
  await page.click("#applyFeat");
  await sleep(300);
  const lastMsg = await page.$$eval("#log .msg", (l) => l.at(-1)?.textContent || "");
  check("invalid JSON reports an error", /JSON error/.test(lastMsg), lastMsg.slice(0, 80));

  // invalid param expression: rejected cleanly
  const hd = page.locator("#params tr", { hasText: "hole_d" }).first().locator("input");
  await hd.fill("2 *");
  await hd.press("Enter");
  await sleep(1500);
  const msgs = await page.$$eval("#log .msg", (l) => l.slice(-2).map((x) => x.textContent));
  check("bad param expression reported", msgs.some((m) => /parse|error|Error/i.test(m)), msgs.join(" | ").slice(0, 160));
  check("part still ok after bad param", (await status()).includes("ok"), await status());
  check("rejected param field reverts", (await hd.inputValue()) === "5.5 mm", await hd.inputValue());
  await shot("05_bad_param");

  // per-feature parameters: ▸ on a tree row shows the numbers only that feature uses, editable in place
  await page.click('#tree li.feat[data-id=hole_sketch] .caret');
  const hsRows = await page.$$eval("#tree ol.fparams[data-feature=hole_sketch] li.fparam .pl", (l) => l.map((x) => x.textContent));
  check("expanding a sketch lists its dimensions and params", JSON.stringify(hsRows) === '["hole_d ƒ","hole_spacing ƒ","hole_z ƒ"]', JSON.stringify(hsRows));
  check("expanding doesn't select the feature", !(await page.$("#tree li.feat.selected[data-id=hole_sketch]")));
  await page.click('#tree li.feat[data-id=wall_sketch] .caret');
  const wsRows = await page.$$eval("#tree ol.fparams[data-feature=wall_sketch] li.fparam", (l) => l.map((x) => [x.querySelector(".pl").textContent, x.classList.contains("shared")]));
  check("a shared param is marked as shared", JSON.stringify(wsRows) === '[["width ƒ",true],["wall_h ƒ",false]]', JSON.stringify(wsRows));
  await page.click('#tree li.feat[data-id=base] .caret');
  check("an extrude shows its distance and the param behind it",
    (await page.textContent("#tree ol.fparams[data-feature=base] li.fparam .pl")) === "distance ƒ base_t");
  check("global params come first, then feature params", (await page.$$eval("#params tr", (l) => l.map((x) => x.dataset.param || "|"))).join(",")
    .match(/^width,depth,(\w+,)*\|,base_t,/), (await page.$$eval("#params tr", (l) => l.map((x) => x.dataset.param || "|"))).join(","));
  const vHole = await vol();
  const hdTree = page.locator("#tree ol.fparams[data-feature=hole_sketch] li.fparam[data-param=hole_d] input");
  await hdTree.fill("7 mm");
  await hdTree.press("Enter");
  await sleep(1500);
  check("editing it in the tree changes the part", (await vol()) < vHole - 1, `${vHole} -> ${await vol()}`);
  check("and the parameter table", (await page.inputValue("#params tr[data-param=hole_d] input")) === "7 mm");
  check("expansion survives the rebuild", !!(await page.$("#tree ol.fparams[data-feature=hole_sketch]")));
  await page.evaluate(() => document.activeElement.blur());
  await page.keyboard.press("Control+z");
  await sleep(1500);
  check("undo restores it", Math.abs((await vol()) - vHole) < 0.1);
  await shot("05b_feature_params");
  for (const id of ["hole_sketch", "wall_sketch", "base"]) await page.click(`#tree li.feat[data-id=${id}] .caret`);
  check("collapsed again", (await page.$$("#tree ol.fparams")).length === 0);

  // rollback bar: drag it above 'wall'
  const bar = page.locator("#tree li.rollbar");
  const target = page.locator("#tree li.feat >> text=wall_sketch");
  await page.evaluate(() => document.querySelector("#left").scrollTo(0, 0));
  const bb = await bar.boundingBox(), tb = await target.boundingBox();
  await page.mouse.move(bb.x + bb.width / 2, bb.y + bb.height / 2);
  await page.mouse.down();
  await page.mouse.move(tb.x + 20, tb.y + 2, { steps: 8 });
  await page.mouse.up();
  await sleep(2000);
  check("rollback note shown", await page.isVisible("#rollbackNote"), await page.textContent("#rollbackNote"));
  const rolled = await page.$$eval("#tree li.feat.rolled", (l) => l.length);
  check("features after bar greyed", rolled >= 8, `${rolled}`);
  check("status says rolled back", (await status()).includes("rolled back"));
  await shot("06_rollback");
  // drag back to the end
  await bar.scrollIntoViewIfNeeded();
  const bb2 = await bar.boundingBox();
  const last = await page.locator("#tree li.feat").last().boundingBox();
  await page.mouse.move(bb2.x + bb2.width / 2, bb2.y + bb2.height / 2);
  await page.mouse.down();
  await page.mouse.move(last.x + 20, last.y + last.height + 10, { steps: 8 });
  await page.mouse.up();
  await sleep(2000);
  check("rollback cleared", !(await page.isVisible("#rollbackNote")));

  // history + renders dialogs
  await page.click("#historyBtn");
  await page.waitForSelector("#historyDlg[open]", { timeout: 5000 }).catch(() => {});
  const nHist = await page.$$eval("#history li", (l) => l.length);
  check("history dialog lists changes", nHist >= 3, `${nHist}`);
  await shot("07_history");
  await page.click("#historyDlg button");
  await page.click("#rendersBtn");
  await page.waitForFunction(() => { const i = document.querySelector("#rendersImg"); return i.complete && i.naturalWidth > 0; }, null, { timeout: 30000 }).catch(() => {});
  check("renders dialog image loads", await page.evaluate(() => document.querySelector("#rendersImg").naturalWidth > 0));
  await shot("08_renders");
  await page.click("#rendersDlg button");

  // switch part
  await page.selectOption("#partSelect", "hex_standoff.vcad.json");
  await page.waitForFunction(() => document.querySelector("#partName").textContent.includes("hex_standoff"), null, { timeout: 15000 }).catch(() => {});
  check("switch to hex_standoff", (await page.textContent("#partName")).includes("hex_standoff"));
  check("selection cleared on switch", (await page.textContent("#selName")) === "");
  await sleep(1500);
  await shot("09_hex");

  // new part via the prompt() dialog
  page.once("dialog", (d) => d.accept("gui_made"));
  await page.click("#newBtn");
  await sleep(1500);
  check("new part becomes active", (await page.textContent("#partName")).includes("gui_made"), await page.textContent("#partName"));
  const hasNew = (await page.$$eval("#partSelect option", (os) => os.map((o) => o.textContent))).some((o) => o.includes("gui_made"));
  check("new part in part list", hasNew);
  check("empty part status", (await status()) !== null, await status());
  await shot("10_new_part");

  // new part with a name that already exists: error, no crash
  page.once("dialog", (d) => d.accept("gui_made"));
  await page.click("#newBtn");
  await sleep(1000);
  const lastMsg2 = await page.$$eval("#log .msg", (l) => l.at(-1)?.textContent || "");
  check("duplicate new part reports error", /exists/i.test(lastMsg2), lastMsg2.slice(0, 100));

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  const realConsole = consoleErrors.filter((e) => !/Failed to load resource/.test(e));
  check("no console errors (besides 4xx logs)", realConsole.length === 0, realConsole.join(" | ").slice(0, 300));
  fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify({ results, consoleErrors, pageErrors, badResponses }, null, 1));
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  console.log("4xx/5xx responses:", badResponses);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
