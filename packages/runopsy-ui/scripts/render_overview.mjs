/**
 * Print docs/overview.html to a PDF in the repository root.
 *
 * Lives here rather than in the repository's scripts/ directory for the same reason
 * render_ui.mjs does: ESM resolves imports from the file's own location, and playwright
 * is a dependency of this package.
 *
 *   cd packages/runopsy-ui && npm run overview
 */

import { chromium } from "playwright";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..", "..");
// One argument picks the language: no argument renders the English original, "tr"
// renders the owner's translated copy. The English one is the customer-facing artifact
// and the source of truth; the translation carries a footer saying so.
const LANG = process.argv[2] ?? "en";
const SOURCE = resolve(ROOT, "docs", LANG === "en" ? "overview.html" : `overview.${LANG}.html`);
const OUT = resolve(ROOT, LANG === "en" ? "Runopsy-overview.pdf" : `Runopsy-overview.${LANG}.pdf`);

const browser = await chromium.launch();
const page = await browser.newPage();
await page.goto(pathToFileURL(SOURCE).href, { waitUntil: "networkidle" });
await page.pdf({
  path: OUT,
  format: "A4",
  printBackground: true,
  // Margins come from the @page rule so the source and the PDF agree; overriding them
  // here would silently reflow a document written to fit five pages.
  preferCSSPageSize: true,
});
await browser.close();
console.log(`wrote ${OUT}`);
