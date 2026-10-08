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

## Company server (several people share one set of records)

Download `QuoteServer.zip` from the same release page, extract it on the Windows server
(e.g. `D:\QuoteTool`), then right-click `install-server.bat` -> **Run as administrator**.
It opens firewall port 8765, registers a start-at-boot task and starts the program.
Records, generated files, `users.json` and daily `backups/` live in the `QuoteData` folder next to the exe.

1. On the server itself open `http://127.0.0.1:8765/` and create the administrator account
   (or without a browser: `QuoteCalculator.exe user add admin --admin --data-dir D:\QuoteTool\QuoteData`).
2. The administrator adds the other users in the web page (History tab -> Users).
3. Colleagues open `http://<server-ip>:8765/` and sign in. Records are shared; only the creator or an
   administrator can delete a record.

Windows Server 2012 / Windows 7 cannot run the default build; use `QuoteServer-compat.zip` (Python 3.8) instead.
`uninstall-server.bat` removes the task and firewall rule (data is kept).

Security notes: passwords are stored as salted PBKDF2 hashes, sessions are signed cookies (12 h),
5 failed logins lock that user/IP for 10 minutes. The built-in server speaks plain HTTP unless you pass
`--cert fullchain.pem --key privkey.pem`; **do not expose a plain-HTTP port to the internet**
(passwords would travel unencrypted) - use HTTPS or a VPN for access from outside the company.

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
