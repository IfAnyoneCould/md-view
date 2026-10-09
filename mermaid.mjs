// keeps one headless browser open so a diagram renders in ~400ms instead of
// mmdc's ~2.5s browser start. json lines in on stdin, json lines out
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { writeFileSync } from "node:fs";
import { createInterface } from "node:readline";

const [root, browserPath] = process.argv.slice(2);
const cli = join(root, "@mermaid-js", "mermaid-cli");
const { renderMermaid } = await import(pathToFileURL(join(cli, "src", "index.js")));
const { default: puppeteer } = await import(pathToFileURL(join(cli, "node_modules", "puppeteer", "lib", "puppeteer", "puppeteer.js")));

const browser = await puppeteer.launch({ executablePath: browserPath, headless: true });
const say = (o) => process.stdout.write(JSON.stringify(o) + "\n");
say({ ready: true });

for await (const line of createInterface({ input: process.stdin })) {
  const { id, code, out, config, scale } = JSON.parse(line);
  try {
    const { data } = await renderMermaid(browser, code, "png", {
      backgroundColor: "transparent",
      mermaidConfig: config,
      viewport: { width: 800, height: 600, deviceScaleFactor: scale || 2 },
    });
    writeFileSync(out, data);
    say({ id, ok: true });
  } catch (e) {
    say({ id, ok: false, error: String(e.message || e).split("\n")[0] });
  }
}
await browser.close();
