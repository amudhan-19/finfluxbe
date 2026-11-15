# Flux — FinFlux G/L Entry API Extension

**Publisher:** Tick & Tie  
**Version:** 1.0.0.0  
**Platform:** Business Central 27.x (SaaS)

## Troubleshooting — symbol download errors

> **Quick fix:** See [DOWNLOAD-SYMBOLS.md](./DOWNLOAD-SYMBOLS.md)

### Error: `/v2.0/production/dev/packages` → Internal Server Error

You ran **`AL: Download Symbols`** while connected to **Production**. Microsoft blocks this on SaaS.

**Fix (recommended — no Sandbox needed):**

1. VS Code → `Ctrl+Shift+P`
2. Run **`AL: Download Symbols from Global Sources`** (NOT `AL: Download Symbols`)
3. Then **`AL: Package`** to create the `.app` file
4. Upload to Production via BC → **Extension Management**

Requires AL Language extension **v17+**.

### Alternative — Sandbox cloud download

1. Create a Sandbox in [BC Admin Center](https://businesscentral.dynamics.com/admin)
2. Set `"environmentName"` in `.vscode/launch.json` to your sandbox name
3. Select **Flux – Sandbox** launch config in VS Code status bar
4. Run **`AL: Download Symbols`**

### Offline script fallback

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\download-symbols.ps1
```

Then: **AL: Compile** → **AL: Package**

Upload the `.app` to **Production** via BC → **Extension Management** → **Upload**.

### Version mismatch

`app.json` must match your BC version (Help → About in BC). If your tenant runs BC 28, change `platform` / `application` to `28.0.0.0` and `runtime` to `16.0`.

## Purpose

The Flux extension exposes additional G/L Entry fields to the FinFlux AI commentary engine. The standard BC `generalLedgerEntries` API endpoint omits four fields needed for transaction attribution:

- **Source Type** — Customer, Vendor, Bank Account, etc.
- **Source No.** — The source record number
- **Bal. Account Type** — Balancing account type
- **Bal. Account No.** — Balancing account number

This extension provides those fields via a dedicated read-only custom API endpoint and also makes them visible in the Business Central General Ledger Entries page.

## AL Objects

| Object | Type | Description |
|--------|------|-------------|
| `51001 FinFlux GL Entries API` | API Page | Read-only OData/JSON API exposing G/L Entry fields for FinFlux |
| `51002 FinFlux API Read` | Permission Set | Assignable read-only permission set for API access |

> **Note:** BC 27.5+ Base Application already shows Source Type, Source No., Bal. Account Type, and Bal. Account No. on the General Ledger Entries page, so no page extension is required.

## Custom API Endpoint

After publishing, the FinFlux API is available at:

```
GET https://api.businesscentral.dynamics.com/v2.0/{tenantId}/{environment}/api/tickandtie/finflux/v1.0/companies({companyId})/finfluxGlEntries
```

### Response Fields

| JSON Field | BC Field |
|------------|----------|
| `id` | SystemId |
| `entryNumber` | Entry No. |
| `postingDate` | Posting Date |
| `documentNumber` | Document No. |
| `documentType` | Document Type |
| `accountNumber` | G/L Account No. |
| `description` | Description |
| `amount` | Amount |
| `debitAmount` | Debit Amount |
| `creditAmount` | Credit Amount |
| `sourceType` | Source Type |
| `sourceNumber` | Source No. |
| `balAccountType` | Bal. Account Type |
| `balAccountNumber` | Bal. Account No. |
| `externalDocumentNumber` | External Document No. |
| `globalDimension1Code` | Global Dimension 1 Code |
| `globalDimension2Code` | Global Dimension 2 Code |
| `lastModifiedDateTime` | SystemModifiedAt |

### Example Queries

```
# Latest 200 entries
GET .../finfluxGlEntries?$top=200

# Filter by posting date range
GET .../finfluxGlEntries?$filter=postingDate ge 2025-01-01 and postingDate le 2025-12-31

# Filter by account number
GET .../finfluxGlEntries?$filter=accountNumber eq '40000'

# Filter by source type
GET .../finfluxGlEntries?$filter=sourceType eq 'Customer'

# Paginate
GET .../finfluxGlEntries?$top=200&$skip=200
```

## Testing with Postman

1. **Authentication:** OAuth 2.0 with Azure AD.
   - Token URL: `https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token`
   - Scope: `https://api.businesscentral.dynamics.com/.default`

2. **Get Company ID:**
   ```
   GET https://api.businesscentral.dynamics.com/v2.0/{tenantId}/production/api/v2.0/companies
   ```

3. **Call the FinFlux endpoint:**
   ```
   GET https://api.businesscentral.dynamics.com/v2.0/{tenantId}/production/api/tickandtie/finflux/v1.0/companies({companyId})/finfluxGlEntries?$top=10
   ```

4. **Assign** the `FinFlux API Read` permission set to the service account or user making API calls.

## Extension Scope

This extension is **read-only**. It does not post journal entries, write back to G/L entries, create reconciliation records, or modify any Business Central data.

## Publishing

1. Open the project in VS Code with the AL Language extension installed.
2. Run **AL: Download Symbols** (`Ctrl+Shift+P` → `AL: Download Symbols`).
3. Run **AL: Publish** (`F5` or `Ctrl+Shift+P` → `AL: Publish`).
4. The extension targets the `production` environment for tenant `00f86bc4-5db7-4308-90b3-c016fe4e38c1`.
