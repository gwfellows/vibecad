// Browser test of the agent panel against tests/gui/fake_agent_app.py (scripted agent, no model).
// Usage: node agent_panel.js <base_url> <screenshot_dir> [three_pkg_dir]
const { chromium } = require("playwright");
const path = require("path");

const [BASE, OUT, THREE] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

// fraction of pixels that differ noticeably between two PNG screenshots (decoded in the page)
const pixelDiff = (page, a, b) => page.evaluate(async ([a, b]) => {
  const load = async (b64) => { const img = new Image(); img.src = `data:image/png;base64,${b64}`; await img.decode(); return img; };
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  const px = (img) => { const c = new OffscreenCanvas(img.width, img.height); const g = c.getContext("2d"); g.drawImage(img, 0, 0); return g.getImageData(0, 0, img.width, img.height).data; };
  const [da, db] = [px(ia), px(ib)];
  if (da.length !== db.length) return 1;
  let n = 0;
  for (let i = 0; i < da.length; i += 4) if (Math.abs(da[i] - db[i]) + Math.abs(da[i + 1] - db[i + 1]) + Math.abs(da[i + 2] - db[i + 2]) > 30) n++;
  return n / (da.length / 4);
}, [a.toString("base64"), b.toString("base64")]);

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `agent_${n}.png`) });
  const vol = async () => { const m = (await page.textContent("#partStatus")).match(/([\d.]+) mm³/); return m ? +m[1] : null; };
  const ask = async (text) => {
    await page.fill("#prompt", text);
    await page.press("#prompt", "Enter");
    await page.waitForFunction(() => document.querySelector("#metrics").textContent.includes("working"), null, { timeout: 5000 }).catch(() => {});
    const busy = await page.isDisabled("#sendBtn") && !(await page.isDisabled("#stopBtn"));
    await page.waitForFunction(() => document.querySelector("#metrics").textContent.includes("last run"), null, { timeout: 60000 });
    await page.waitForTimeout(800);
    return busy;
  };

  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 });

  // turn 1: create
  const busy1 = await ask("Make a 60 x 40 plate");
  check("busy while the agent works (send off, stop on)", busy1);
  check("idle afterwards", !(await page.isDisabled("#sendBtn")) && (await page.isDisabled("#stopBtn")));
  check("user message in log", (await page.$$eval("#log .msg.user", (l) => l.map((x) => x.textContent))).some((t) => t.includes("60 x 40")));
  check("thinking shown collapsed", (await page.$$eval("#log details.think", (l) => l.length)) === 1);
  const rows1 = await page.$$eval("#log details.tool", (l) => l.map((d) => [d.querySelector(".name").textContent, d.querySelector(".res").textContent]));
  check("three tool rows", rows1.length === 3, JSON.stringify(rows1));
  check("apply_ops row shows volume change", /ok · – → 9600 mm³/.test(rows1[1]?.[1] || ""), rows1[1]?.[1]);
  check("render row has its image", (await page.$$eval("#log details.tool img", (l) => l.length)) === 1);
  check("new part opened in the GUI", (await page.textContent("#partName")).includes("fake_plate"));
  check("tree shows the agent's features", (await page.$$eval("#tree li.feat", (l) => l.length)) === 2);
  check("metrics summarize the run", /3 tool calls/.test(await page.textContent("#metrics")), await page.textContent("#metrics"));
  check("reply says no context for a fresh prompt", (await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent)).includes("[Active part") === false);
  await shot("1_create");
  // the view must fit the new part by itself: clicking Iso (the same fit) should change nothing
  const canvas = page.locator("#viewer canvas");
  await page.mouse.move(5, 890);
  await page.waitForTimeout(1000);
  const before = await canvas.screenshot();
  await page.click(".vtools [data-view=iso]");
  await page.mouse.move(5, 890);
  await page.waitForTimeout(1500);
  const changed = await pixelDiff(page, before, await canvas.screenshot());
  check("new part is framed without clicking Iso", changed < 0.01, `${(changed * 100).toFixed(2)}% of pixels differ`);
  const v1 = await vol();

  // turn 2: follow-up with a feature selected (not scoped); one rejected batch, one good one
  await page.click("#tree li.feat >> text=plate_sk");
  await page.waitForTimeout(300);
  await page.click("#tree li.feat >> text=/^plate$/");
  await ask("Add a 10 mm hole in the middle");
  const reply2 = await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent);
  check("prompt carries active part", reply2.includes("[Active part: parts/fake_plate.vcad.json]"), reply2.slice(0, 120));
  check("prompt carries the selection", reply2.includes("selected feature plate (extrude"));
  check("unscoped: no limit line", !reply2.includes("Edits are limited"));
  const res2 = await page.$$eval("#log details.tool .res", (l) => l.slice(-2).map((x) => x.textContent));
  check("rejected batch labelled", res2[0] === "rejected", res2[0]);
  check("good batch applied", /^ok · 9600 → /.test(res2[1]), res2[1]);
  check("hole reduced volume", (await vol()) < v1, `${v1} -> ${await vol()}`);
  check("param not changed by rejected typo", (await page.inputValue("#params tr >> nth=0 >> input")) === "60 mm");
  await shot("2_followup");

  // turn 3: scoped to the selected feature
  await page.click("#tree li.feat >> text=/^plate$/");  // toggle off
  await page.click("#tree li.feat >> text=/^plate$/");  // and on again
  await page.check("#scope");
  const v2 = await vol();
  await ask("Make it thicker");
  const reply3 = await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent);
  check("scoped prompt says edits are limited", reply3.includes("Edits are limited to plate and features that depend on it"), reply3.slice(0, 200));
  const res3 = await page.$$eval("#log details.tool .res", (l) => l.slice(-2).map((x) => x.textContent));
  check("out-of-scope edit rejected", res3[0] === "rejected", res3[0]);
  const pre3 = await page.$$eval("#log details.tool", (l) => l.at(-2).textContent);
  check("rejection explains the scope", pre3.includes("outside the current scope"));
  check("in-scope edit applied", (await vol()) > v2, `${v2} -> ${await vol()}`);
  check("user message shows the scope", (await page.$$eval("#log .msg.user .sel", (l) => l.at(-1).textContent)).includes("edits limited"));
  await shot("3_scoped");

  // the user can undo the agent's edits
  await page.click("#undoBtn");
  await page.waitForTimeout(1500);
  check("undo reverts the agent's last edit", Math.abs((await vol()) - v2) < 0.1, `${await vol()}`);

  // reload mid-conversation: transcript is replayed
  await page.reload();
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  await page.waitForTimeout(1000);
  check("transcript replayed after reload", (await page.$$eval("#log details.tool", (l) => l.length)) === 7);
  check("part still shown after reload", (await page.textContent("#partName")).includes("fake_plate"));

  // new chat clears the log
  await page.click("#resetBtn");
  await page.waitForTimeout(500);
  check("new chat clears the log", (await page.$$eval("#log > *", (l) => l.length)) === 0);

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
