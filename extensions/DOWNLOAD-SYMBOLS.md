# FinFlux BC Extension — Symbol download fix

## Your latest error explained

```
/v2.0/Sandbox/dev/packages → Not Found (404)
```

This means **your tenant has no BC environment named `Sandbox`**.

You are also still running the **wrong VS Code command**:
- ❌ `AL: Download Symbols` — needs a live BC environment (Production or Sandbox)
- ✅ `AL: Download Symbols from Global Sources` — no environment needed

---

## Fix now (choose ONE method)

### Method A — VS Code Global Sources (recommended)

1. Open **`FinFlux Backend/extensions`** in VS Code
2. Update **AL Language** extension to **v17+** (`ms-dynamics-smb.al`)
3. `Ctrl+Shift+P` → **`AL: Download Symbols from Global Sources`**
4. Country/region: **`w1`** (already set in `.vscode/settings.json`)
5. Wait for `.alpackages` to contain `Microsoft_*.app` files
6. `Ctrl+Shift+P` → **`AL: Package`**
7. Upload `.app` to BC → **Extension Management**

### Method B — Offline script (no VS Code cloud command)

In PowerShell:

```powershell
cd "c:\Users\TICKMARKS\OneDrive - tickandtie.com\Documents\TFlux\FinFlux Backend\extensions"
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\download-global-symbols.ps1
```

Or in VS Code: **`Ctrl+Shift+B`** (runs the default build task = download symbols)

Then: **`AL: Package`**

---

## If you need F5 publish to a Sandbox later

1. BC Admin Center → note your **exact** sandbox environment name
2. Add to `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Flux – Sandbox",
      "request": "launch",
      "type": "al",
      "environmentType": "Sandbox",
      "environmentName": "YOUR_EXACT_SANDBOX_NAME",
      "tenant": "00f86bc4-5db7-4308-90b3-c016fe4e38c1",
      "authentication": "AAD",
      "schemaUpdateMode": "Synchronize",
      "startupObjectId": 50101,
      "startupObjectType": "Page",
      "breakOnError": "All",
      "launchBrowser": false
    }
  ]
}
```

Replace `YOUR_EXACT_SANDBOX_NAME` with the name from Admin Center (case-sensitive).

---

## Tamil

- `Sandbox` environment ungal tenant-la illa — adhanala 404 varuthu
- `AL: Download Symbols` use pannatheenga
- `AL: Download Symbols from Global Sources` use pannunga
- Illana `download-global-symbols.ps1` script run pannunga
- `.app` create pannitu Production-la upload pannunga
