# 外贸报价计算器 Export Quote Calculator

Single-page tool: `index.html` (open it directly in a browser, no server needed).

- Calculator: RMB cost → FOB / CIF USD prices (rebate, financing, custom costs).
- Quote List: save many quotes, tick some, and generate an English quotation as Excel and/or PDF
  (optional reference images).
- History: generated files are kept in the browser (IndexedDB); search by product code,
  description, quote date or unit-price range. Use *Export backup* regularly.

## Development

`index.html` is generated: it inlines the MIT-licensed libraries in `vendor/`
(ExcelJS, jsPDF, jsPDF-AutoTable) into `src/app.html`. Edit `src/app.html`, then run:

```
node build.js
```
