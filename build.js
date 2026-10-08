// Builds the single-file index.html: inlines vendor libraries into src/app.html.
// Usage: node build.js
const fs = require("fs");
const read = (p) => fs.readFileSync(p, "utf8");
const libs = ["vendor/exceljs.min.js", "vendor/jspdf.umd.min.js", "vendor/jspdf.plugin.autotable.min.js"];
const scripts = libs
  .map((p) => "<script>/* " + p + " (MIT) */\n" + read(p).replace(/<\/script/gi, "<\\/script") + "\n</script>")
  .join("\n");
const out = read("src/app.html").replace("<!--VENDOR-->", () => scripts);
fs.writeFileSync("index.html", out);
console.log("index.html written, " + (out.length / 1024).toFixed(0) + " KB");
