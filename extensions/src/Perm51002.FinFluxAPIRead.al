permissionset 51002 "FinFlux API Read"
{
    Assignable = true;
    Caption = 'FinFlux API Read';
    Permissions =
        tabledata "G/L Entry" = R,
        tabledata "G/L Account" = R,
        tabledata Customer = R,
        tabledata Vendor = R,
        page "FinFlux GL Entries API" = X;
}
