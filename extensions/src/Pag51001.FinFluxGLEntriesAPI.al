page 51001 "FinFlux GL Entries API"
{
    PageType = API;
    APIPublisher = 'tickandtie';
    APIGroup = 'finflux';
    APIVersion = 'v1.0';
    EntityName = 'finfluxGlEntry';
    EntitySetName = 'finfluxGlEntries';
    SourceTable = "G/L Entry";
    DelayedInsert = true;
    Editable = false;
    InsertAllowed = false;
    ModifyAllowed = false;
    DeleteAllowed = false;
    ODataKeyFields = SystemId;

    layout
    {
        area(content)
        {
            repeater(Group)
            {
                field(id; Rec.SystemId)
                {
                    Caption = 'Id';
                }
                field(entryNumber; Rec."Entry No.")
                {
                    Caption = 'Entry Number';
                }
                field(postingDate; Rec."Posting Date")
                {
                    Caption = 'Posting Date';
                }
                field(documentNumber; Rec."Document No.")
                {
                    Caption = 'Document Number';
                }
                field(documentType; Rec."Document Type")
                {
                    Caption = 'Document Type';
                }
                field(accountNumber; Rec."G/L Account No.")
                {
                    Caption = 'Account Number';
                }
                field(description; Rec.Description)
                {
                    Caption = 'Description';
                }
                field(amount; Rec.Amount)
                {
                    Caption = 'Amount';
                }
                field(debitAmount; Rec."Debit Amount")
                {
                    Caption = 'Debit Amount';
                }
                field(creditAmount; Rec."Credit Amount")
                {
                    Caption = 'Credit Amount';
                }
                field(sourceType; Rec."Source Type")
                {
                    Caption = 'Source Type';
                }
                field(sourceNumber; Rec."Source No.")
                {
                    Caption = 'Source Number';
                }
                field(balAccountType; Rec."Bal. Account Type")
                {
                    Caption = 'Bal. Account Type';
                }
                field(balAccountNumber; Rec."Bal. Account No.")
                {
                    Caption = 'Bal. Account Number';
                }
                field(externalDocumentNumber; Rec."External Document No.")
                {
                    Caption = 'External Document Number';
                }
                field(globalDimension1Code; Rec."Global Dimension 1 Code")
                {
                    Caption = 'Global Dimension 1 Code';
                }
                field(globalDimension2Code; Rec."Global Dimension 2 Code")
                {
                    Caption = 'Global Dimension 2 Code';
                }
                field(lastModifiedDateTime; Rec.SystemModifiedAt)
                {
                    Caption = 'Last Modified Date Time';
                }
            }
        }
    }
}
