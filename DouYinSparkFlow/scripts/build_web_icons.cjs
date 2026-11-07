// Rebuild from the vendored Lucide library; no package install is required.
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../webui");
const lucide = require(path.join(root, "static/lucide.min.js"));
const names = new Set(["pause", "play", "loader-circle", "info"]);
for (const file of fs.readdirSync(path.join(root, "templates"))) {
  if (!file.endsWith(".html")) continue;
  const source = fs.readFileSync(path.join(root, "templates", file), "utf8");
  for (const match of source.matchAll(/data-lucide="([a-z0-9-]+)"/g)) names.add(match[1]);
}
const icons = {};
for (const name of [...names].sort()) {
  const key = name.replace(/(^|-)(\w)/g, (_, prefix, letter) => letter.toUpperCase());
  if (!lucide.icons[key]) throw new Error(`Missing icon: ${name}`);
  icons[name] = lucide.icons[key];
}
const output = `/** Lucide v1.8.0 SVG subset. ISC and Feather MIT notices: lucide-LICENSE.txt. */
(() => {
  const icons = ${JSON.stringify(icons)};
  const ns = "http://www.w3.org/2000/svg";
  const createIcons = ({attrs = {}, root = document} = {}) => {
    root.querySelectorAll("[data-lucide]:not(svg)").forEach((placeholder) => {
      const name = placeholder.getAttribute("data-lucide");
      const nodes = icons[name];
      if (!nodes) return;
      const svg = document.createElementNS(ns, "svg");
      const attributes = {xmlns: ns, width: "24", height: "24", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", ...attrs};
      for (const attribute of placeholder.attributes) {
        if (attribute.name !== "data-lucide") attributes[attribute.name] = attribute.value;
      }
      attributes.class = ["lucide", "lucide-" + name, attributes.class || ""].join(" ").trim();
      for (const [key, value] of Object.entries(attributes)) svg.setAttribute(key, value);
      for (const [tag, properties] of nodes) {
        const child = document.createElementNS(ns, tag);
        for (const [key, value] of Object.entries(properties)) child.setAttribute(key, value);
        svg.appendChild(child);
      }
      placeholder.replaceWith(svg);
    });
  };
  window.lucide = {createIcons};
})();
`;
fs.writeFileSync(path.join(root, "static/icons.js"), output);
process.stdout.write(`${names.size} icons, ${Buffer.byteLength(output)} bytes\n`);
