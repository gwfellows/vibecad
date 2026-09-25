// Browser test of the sketch editor against tests/gui/fake_agent_app.py (scripted agent, no model).
// Usage: node sketch_editor.js <base_url> <screenshot_dir> [three_pkg_dir]
const { chromium } = require("playwright");
const path = require("path");

const [BASE, OUT, THREE] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };
const near = (a, b, tol = 1e-3) => a && b && Math.abs(a[0] - b[0]) < tol && Math.abs(a[1] - b[1]) < tol;

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const wsLog = [];
  page.on("websocket", (ws) => { ws.on("framereceived", (f) => wsLog.push(String(f.payload).slice(0, 160))); ws.on("framesent", (f) => wsLog.push("SENT " + String(f.payload).slice(0, 300))); });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  if (THREE) await page.route("https://cdn.jsdelivr.net/npm/three@0.170.0/**", (route) =>
    route.fulfill({ path: path.join(THREE, route.request().url().split("three@0.170.0/")[1]), contentType: "application/javascript" }));
  const shot = (n) => page.screenshot({ path: path.join(OUT, `sketch_${n}.png`) });
  const SK = (fn, arg) => page.evaluate(([src, a]) => new Function("sk", "a", `return (${src})(sk, a)`)(window.vibecadSketch, a), [fn.toString(), arg]);
  const data = () => SK((sk) => sk.data());
  const entity = async (id) => (await data())?.entities.find((e) => e.id === id);
  const info = async () => (await page.textContent("#sketchInfo")).trim();
  const scr = (u, v) => SK((sk, a) => sk.toScreen(a[0], a[1]), [u, v]);
  const settle = () => page.waitForTimeout(700);
  const click = async (u, v, { shift = false } = {}) => {
    const [x, y] = await scr(u, v);
    await page.mouse.move(x, y);
    if (shift) await page.keyboard.down("Shift");
    await page.mouse.click(x, y);
    if (shift) await page.keyboard.up("Shift");
    await page.waitForTimeout(350);
  };
  const drag = async (from, to) => {
    const [x0, y0] = await scr(...from), [x1, y1] = await scr(...to);
    await page.mouse.move(x0, y0);
    await page.mouse.down();
    for (let k = 1; k <= 8; k++) await page.mouse.move(x0 + ((x1 - x0) * k) / 8, y0 + ((y1 - y0) * k) / 8);
    await page.waitForTimeout(400);
    await page.mouse.up();
    await settle();
  };
  const dimTo = async (value) => {
    await page.keyboard.press("d");
    await page.waitForSelector("#dimEdit:not([hidden])", { timeout: 3000 });
    const initial = await page.inputValue("#dimEdit input");
    await page.fill("#dimEdit input", String(value));
    await page.press("#dimEdit input", "Enter");
    await settle();
    return initial;
  };

  // a part with one empty sketch on XY
  await page.goto(BASE);
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  await page.evaluate(async () => {
    const post = (u, b) => fetch(u, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) });
    await post("/api/new", { path: "parts/sk_test.vcad.json", name: "sk_test" });
    await post("/api/ops", { ops: [{ op: "add_feature", feature: { id: "sk", type: "sketch", plane: { datum: "XY" } } }], message: "empty sketch" });
  });
  await page.reload();
  await page.waitForSelector("#conn.live", { timeout: 15000 });
  await page.waitForFunction(() => [...document.querySelectorAll("#tree li.feat .fid")].some((x) => x.textContent === "sk"));
  await page.click("#tree li.feat >> text=/^sk$/");
  await page.waitForSelector("#sketchBar:not([hidden])");
  await settle();
  check("empty sketch opens in the editor", /0 DOF/.test(await info()), await info());
  check("constraint tools disabled with nothing selected", await page.isDisabled("#sketchTools [data-con=horizontal]"));

  // rectangle: 4 lines, 4 DOF (position + size)
  await page.keyboard.press("r");
  check("R picks the rectangle tool", (await SK((sk) => sk.tool())) === "rect");
  await click(-10, -5);
  await click(20, 15);
  const ents = (await data()).entities.map((e) => e.id).sort();
  check("rectangle drawn as 4 lines", ents.join() === "rect1_bottom,rect1_left,rect1_right,rect1_top", ents.join());
  check("rectangle has 4 DOF", /4 DOF/.test(await info()), await info());
  check("free geometry is not marked fixed", !(await entity("rect1_bottom")).fixed);
  await page.keyboard.press("Escape");

  // dimension it and pin a corner to the origin: 0 DOF
  await click(5, -5);
  check("click selects a line", JSON.stringify(await SK((sk) => sk.selection())) === '["rect1_bottom"]');
  check("Dim enabled for a line", !(await page.isDisabled("#sketchTools [data-con=dim]")));
  check("new dimension starts at the measured value", (await dimTo(30)) === "30");
  check("width dimension: 3 DOF", /3 DOF/.test(await info()), await info());
  await click(-10, 5);
  await dimTo(20);
  await click(-10, -5);
  await click(0, 0, { shift: true });
  check("shift-click adds the origin", JSON.stringify(await SK((sk) => sk.selection())) === '["rect1_bottom.p1","origin"]');
  check("coincident enabled for two points", !(await page.isDisabled("#sketchTools [data-con=coincident]")));
  await page.click("#sketchTools [data-con=coincident]");
  await settle();
  check("fully constrained", /0 DOF · fully constrained/.test(await info()), await info());
  check("constrained geometry is marked fixed", (await data()).entities.every((e) => e.fixed));
  check("tree shows the sketch DOF", (await page.textContent("#tree li.feat.selected .meta")) === "0 DOF");
  await shot("1_rect");

  // a free line, dragged: the endpoint follows the cursor; the constrained rectangle can't be dragged
  await page.click("#fitBtn");
  await page.waitForTimeout(300);
  await page.keyboard.press("l");
  await click(2, 25);
  await click(12, 30);
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  const l1 = await entity("line1");
  check("line drawn", l1 && near(l1.p1, [2, 25]) && near(l1.p2, [12, 30]), JSON.stringify(l1));
  check("Esc returns to select", (await SK((sk) => sk.tool())) === "select");
  await drag([12, 30], [8, 34]);
  check("dragging a free endpoint moves it", near((await entity("line1")).p2, [8, 34], 1e-3), JSON.stringify((await entity("line1")).p2));
  await drag([30, 20], [35, 25]);
  check("fully constrained corner doesn't move", near((await entity("rect1_top")).p1, [30, 20]), JSON.stringify((await entity("rect1_top")).p1));
  check("hint explains why", /fully constrained/.test(await page.textContent("#sketchHint")));
  await page.keyboard.press("Escape");

  // change a dimension by double-clicking its label
  await page.locator("#labels .dim[data-name=rect1_bottom_len]").dblclick();
  await page.waitForSelector("#dimEdit:not([hidden])", { timeout: 3000 });
  check("double-click opens the value editor", (await page.inputValue("#dimEdit input")) === "30");
  await page.fill("#dimEdit input", "40");
  await page.press("#dimEdit input", "Enter");
  await settle();
  check("edited dimension resizes the rectangle", near((await entity("rect1_bottom")).p2, [40, 0]), JSON.stringify((await entity("rect1_bottom")).p2));

  // a conflicting dimension is shown, and undo removes it
  await click(0, 10);
  await dimTo(25);
  check("conflict reported in the bar", /conflicting/.test(await info()), await info());
  check("conflicting constraint labels marked", (await page.$$eval("#labels .bad", (l) => l.length)) >= 1);
  await shot("2_conflict");
  await page.keyboard.press("Control+z");
  await settle();
  check("undo clears the conflict", !/conflicting/.test(await info()) && /4 DOF/.test(await info()), await info());  // line1 is still free

  // circle: draw, resize by dragging the rim, then dimension the diameter
  await page.keyboard.press("c");
  await click(10, -12);
  await click(14, -12);
  const c1 = await entity("circle1");
  check("circle drawn", c1 && Math.abs(c1.r - 4) < 1e-3, JSON.stringify(c1));
  await page.keyboard.press("Escape");
  await drag([10, -8], [10, -6]);
  check("dragging the rim resizes the circle", Math.abs((await entity("circle1")).r - 6) < 0.05, `r = ${(await entity("circle1")).r}`);
  await click(16, -12);
  check("Dim default for a circle is its diameter", Math.abs(+(await dimTo(8)) - 12) < 0.05);
  check("diameter applied", Math.abs((await entity("circle1")).r - 4) < 1e-3, `r = ${(await entity("circle1")).r}`);
  const dl = page.locator("#labels .dim[data-name=circle1_d]");
  check("diameter shown CAD-style as ⌀ value", (await dl.textContent()) === "⌀8", await dl.textContent());
  check("dimension name in its tooltip", (await dl.getAttribute("title")).startsWith("circle1_d"));
  check("palette buttons are icons with tooltips", await page.$$eval("#sketchTools button", (l) => l.every((b) => b.querySelector("svg") && b.title)));

  // box select, construction toggle, delete
  await drag([-5, 22], [15, 37]);
  check("box select picks the line inside", JSON.stringify(await SK((sk) => sk.selection())) === '["line1"]', JSON.stringify(await SK((sk) => sk.selection())));
  await page.keyboard.press("g");
  await settle();
  check("G makes it construction geometry", (await entity("line1")).construction === true);
  await click(5, 29.5);
  await page.keyboard.press("Delete");
  await settle();
  check("Delete removes the entity", !(await entity("line1")));
  const dofBefore = (await data()).dof;
  const hGlyph = page.locator("#labels .glyph", { hasText: /^H$/ }).first();
  await hGlyph.click();
  check("clicking a glyph selects its constraint", (await SK((sk) => sk.selection()))[0]?.startsWith("#"));
  await page.keyboard.press("Delete");
  await settle();
  check("deleting a constraint frees a DOF", (await data()).dof === dofBefore + 1, `${dofBefore} -> ${(await data()).dof}`);
  await page.keyboard.press("Control+z");
  await settle();
  check("undo restores it", (await data()).dof === dofBefore);

  // ask the agent about a selection: the prompt carries the entities and their constraints
  await click(20, 20);
  await page.click("#sketchTools [data-act=ask]");
  check("Ask agent focuses the prompt", await page.evaluate(() => document.activeElement.id === "prompt"));
  await page.fill("#prompt", "What holds this line? (echo)");
  await page.press("#prompt", "Enter");
  await page.waitForFunction(() => document.querySelector("#metrics").textContent.includes("last run"), null, { timeout: 30000 });
  const reply = await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent);
  check("agent receives the selected entity", reply.includes("In sketch sk the user selected: rect1_top"), reply.slice(0, 200));
  check("and the constraints on it", reply.includes("horizontal(rect1_top)"));
  check("user message shows the selection", (await page.$$eval("#log .msg.user .sel", (l) => l.at(-1).textContent)).includes("rect1_top"));
  await shot("3_done");

  // freehand marks go with the next prompt, then disappear; they never enter the part
  await page.evaluate(() => document.activeElement.blur());  // the prompt box still has focus from the last message
  await page.keyboard.press("m");
  check("M picks the mark tool", (await SK((sk) => sk.tool())) === "mark");
  const top = await entity("rect1_top");
  const [mx0, my0] = await scr(top.p2[0] + 2, top.p2[1] + 1), [mx1, my1] = await scr(top.p1[0] - 2, top.p1[1] + 1);
  await page.mouse.move(mx0, my0);
  await page.mouse.down();
  for (let k = 1; k <= 12; k++) await page.mouse.move(mx0 + ((mx1 - mx0) * k) / 12, my0 + ((my1 - my0) * k) / 12 - Math.sin((Math.PI * k) / 12) * 6);
  await page.mouse.up();
  await page.waitForTimeout(200);
  check("a stroke is recorded", (await SK((sk) => sk.marks().length)) === 1);
  check("Clear marks enabled", !(await page.isDisabled("#sketchTools [data-act=clearmarks]")));
  const before = await page.$$eval("#log .msg.agent", (l) => l.length);
  await page.fill("#prompt", "Round this edge (echo)");
  await page.press("#prompt", "Enter");
  await page.waitForFunction((n) => document.querySelectorAll("#log .msg.agent").length > n, before, { timeout: 30000 });
  const markReply = await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent);
  check("agent gets the mark as sketch coordinates", markReply.includes("drew 1 freehand mark(s) on sketch sk"), markReply.slice(0, 160));
  check("with the geometry it passes near", /passes near rect1_top/.test(markReply), markReply.slice(0, 400));
  check("marks cleared once sent", (await SK((sk) => sk.marks().length)) === 0);
  check("marks never reach the part", !JSON.stringify(await page.evaluate(() => fetch("/api/feature/sk").then((r) => r.json()))).includes("mark"));
  // ⌖ Reference in a sketch: the next entity clicked goes into the message as a chip
  await page.evaluate(() => document.activeElement.blur());
  await SK((sk) => sk.select([]));
  check("selection cleared", (await SK((sk) => sk.selection())).length === 0);
  await page.click("#prompt");
  await page.keyboard.type("Why can't I move ");
  await page.click("#refBtn");
  check("sketch reference hint", (await page.textContent("#refHint")).includes("sketch entity"));
  await click(20, 20);
  await page.waitForTimeout(300);
  check("clicked entity becomes a chip", JSON.stringify(await page.$$eval("#prompt .ref.sketch", (l) => l.map((x) => x.textContent))) === '["rect1_top"]',
    await page.innerHTML("#prompt"));
  await page.keyboard.type("? (echo)");
  const nBefore = await page.$$eval("#log .msg.agent", (l) => l.length);
  await page.press("#prompt", "Enter");
  await page.waitForFunction((n) => document.querySelectorAll("#log .msg.agent").length > n, nBefore, { timeout: 30000 })
    .catch(async () => console.log("DEBUG", wsLog.slice(-12).join("\n"), nBefore, await page.innerHTML("#prompt"), (await page.$$eval("#log > *", (l) => l.slice(-3).map((x) => x.outerHTML.slice(0, 300)))).join("\n")));
  const skRef = await page.$$eval("#log .msg.agent", (l) => l.at(-1).textContent);
  check("agent gets the sketch reference", skRef.includes("@sketch:sk/rect1_top = rect1_top in sketch sk"), skRef.slice(0, 400));
  await page.evaluate(() => document.activeElement.blur());
  await page.keyboard.press("Escape");

  // Esc clears the selection, then leaves the sketch
  await page.click("#viewer canvas", { position: { x: 5, y: 5 } }).catch(() => {});
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  check("Esc leaves the sketch", !(await page.isVisible("#sketchBar")));

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300));
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  await browser.close();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("SCRIPT ERROR", e); process.exit(2); });
