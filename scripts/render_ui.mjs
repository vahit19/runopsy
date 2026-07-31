/**
 * Screenshot the web view from a real store, for the README.
 *
 * The terminal images come from real output via scripts/render_demo.py; these come
 * from a real browser rendering a real recorded run. Nothing in the README is drawn by
 * hand, which matters more in this project than in most: a picture of a diagnosis is
 * exactly the artefact that must not be a mock when the argument is that confident
 * statements can be checked.
 *
 *   node scripts/render_ui.mjs http://127.0.0.1:8971
 *
 * WebGL needs software rendering in headless Chromium, hence the ANGLE flags — without
 * them the 3D canvas comes out blank and the screenshot silently shows nothing.
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, "..", "docs", "images");
const BASE = process.argv[2] ?? "http://127.0.0.1:8971";
const VIEWPORT = { width: 1280, height: 780 };

async function main() {
  mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch({
    args: [
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
      "--disable-lcd-text",
    ],
  });
  const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 });

  const problems = [];
  page.on("pageerror", (error) => problems.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(message.text());
  });

  await page.goto(BASE, { waitUntil: "networkidle" });

  // The map only exists once the graph and diagnosis have loaded; waiting on the
  // selector rather than a timeout keeps this honest on a slow machine.
  await page.waitForSelector(".react-flow__node", { timeout: 30_000 });
  await page.waitForTimeout(700);
  await page.screenshot({ path: resolve(OUT, "ui-2d.png") });
  console.log("wrote docs/images/ui-2d.png");

  await page.click("button.toggle3d");
  await page.waitForSelector(".map3d canvas", { timeout: 30_000 });
  // Let the orbit rig move off its starting angle and the first frames settle.
  await page.waitForTimeout(2_500);

  const painted = await page.evaluate(() => {
    const canvas = document.querySelector(".map3d canvas");
    if (!canvas) return { ok: false, reason: "no canvas" };
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    return { ok: Boolean(gl), reason: gl ? "webgl live" : "no webgl context" };
  });
  if (!painted.ok) {
    throw new Error(`3D view did not render: ${painted.reason}`);
  }

  await page.screenshot({ path: resolve(OUT, "ui-3d.png") });
  console.log(`wrote docs/images/ui-3d.png (${painted.reason})`);

  await browser.close();

  if (problems.length) {
    console.log("\npage reported problems:");
    for (const problem of problems.slice(0, 5)) console.log("  " + problem);
    process.exitCode = 1;
  }
}

await main();
