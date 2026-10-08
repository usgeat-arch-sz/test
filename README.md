# 外贸报价计算器 Export Quote Calculator

- **Calculator** – RMB cost → FOB / CIF USD prices (rebate, financing, any number of custom cost items).
- **Quote List** – save many quotes, tick some, generate an English quotation as Excel and/or PDF
  (optional reference images). Long lists paginate: the table header repeats on every page.
- **History** – search generated quotations by product code, description, quote date or unit-price range.

## Windows: double-click `QuoteCalculator.exe` (no Python needed)

Download `QuoteCalculator.exe` from the **latest** release
(https://github.com/usgeat-arch-sz/test/releases/tag/latest), put it in its own folder and double-click it.
A black window opens (keep it open while using; close it to quit) and the calculator opens in your browser.
Records and generated files are saved in a `报价数据` folder next to the .exe.
The exe is built automatically by `.github/workflows/build-exe.yml` (PyInstaller).

## Run with Python (saves everything into a folder you choose)

```
python server.py                        # opens http://127.0.0.1:8765/
python server.py --data-dir D:\Quotes   # choose the folder (remembered in config.json)
```

Python 3.8+, standard library only. Data folder layout:

```
items/<id>.json     quote-list entries
quotes/<id>.json    quotation records (used for search)
files/*.xlsx|pdf    the generated quotation files
```

The folder can also be changed in the app (History → Data Folder). Existing data is not moved;
copy the three sub-folders yourself if you want it in the new place.
The server listens on 127.0.0.1 only and rejects requests from other websites.

## Without Python

Opening `index.html` directly still works; records are then kept in the browser (IndexedDB).
Use *Export backup* / *Import backup* (History tab) to move browser data into the folder version.

## Development

`index.html` is generated: it inlines the MIT-licensed libraries in `vendor/`
(ExcelJS, jsPDF, jsPDF-AutoTable) into `src/app.html`. Edit `src/app.html`, then run `node build.js`.
