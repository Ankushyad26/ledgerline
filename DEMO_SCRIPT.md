# Demo script

## Before recording or the interview
1. Open the live link about a minute early (the Render free tier sleeps), then click **↺ Reset demo data**.
2. Check the chip in the top bar shows the AI reader (e.g. `gemini · gemini-3.6-flash`). If the API is down, the app still runs in offline mode; say so honestly.
3. Have `samples/` downloaded locally, so you can also drag-and-drop a file (it proves it's a real upload, not a canned run).

## 5-minute video (≈ 650 words spoken)

**0:00–0:30 · The problem.**
"AP teams open every invoice, find the PO in a spreadsheet, check the numbers and decide. It's slow, and a tired person
makes expensive mistakes: paying twice, paying a fraudster's bank account, paying for laptops that were never ordered.
Ledgerline takes an invoice and returns a decision with every step visible."

**0:30–1:45 · Happy path (01).** Drag `01_happy_path_nimbus.pdf` onto the upload box.
- Narrate the stages as they light up: "It reads the text layer, extracts the fields, checks they add up, finds the vendor
  by GSTIN, confirms the bank account matches our master, checks the invoice number was never seen before, matches PO-1001,
  and reconciles both lines: quantity, price and GST type."
- Point to the decision card: **Approved**, payment due date, posted to the ledger. Open **"How the decision was reached"**
  for the 8-step trace: that's the step-by-step reasoning the brief asks for.
- Open the **PO ledger**: PO-1001 is now 100% consumed.

**1:45–2:45 · Edge case: split PO over-billing (02 → 03).**
- Run 02: approved. "This vendor is delivering 25 laptops across several invoices; 20 are now billed."
- Run 03: **Flagged for review**. Open stage 8: "It bills 7 more, but only 5 are still open on the PO. It flags it for review, explains the
  arithmetic, and routes to Procurement with the exact fix."

**2:45–3:15 · Edge case: bank details changed (08).**
- "This is the most expensive mistake in AP. The invoice looks perfect and matches the PO, but it asks us to pay a new bank account."
- Show the review flag: it compares the account with the vendor master and quotes the note printed on the invoice. The action is
  to call back on the number in the master, never the one on the invoice.

**3:15–3:45 · Edge case: billed before delivery (12).**
- "The PO is for 30 monitors and the invoice is correct against it, but stores have only received 18. This is the
  three-way match: PO, goods receipt and invoice. It flags the invoice and routes it to stores."

**3:45–4:30 · Edge case: scanned invoice with no PO (06), plus human review.**
- "A phone scan with no PO number. It reads the image, ranks the vendor's open POs by item similarity and amount, and
  suggests PO-1005 at 95%, but policy says an inferred PO is never auto-approved."
- Point at the confidence column (real OCR confidence, about 87%). Type a reviewer note, click **Approve & post**, and show the audit trail tab.
- If you have time, open #10 (missing fields): **Correct extracted data**, type the invoice number and date, re-run → Approved.

**4:30–5:00 · Dashboard and close.**
- Dashboard: straight-through rate, value approved, risk caught, top exception reasons.
- "The design principle: AI reads, rules decide, humans own the exceptions. Every decision traces to a rule a finance
  lead can read on the Rules page."

## Live interview runbook
Order: **Reset → 01 → 02 → 03 → 08 → 12 → 07 → 06 (approve it) → 10 (correct it) → Dashboard → Rules.**
Spare scenarios if they ask: 05 (price variance), 09 (tax-inclusive, plus payment terms), 11 (blocked vendor), 13 (bundled lump-sum line).
If asked "where do the thresholds live?", open **Rules & policy**: they're in `policy.json`, part of the dataset, not hard-coded.
Batch demo: select several PDFs at once in the upload box. They queue up and process in order on the dashboard.
To prove it's live: upload one of their files, or rename and re-upload a sample.

## Questions to expect
- **Why not let the LLM decide?** It's non-deterministic and hard to audit. A CFO needs "rule X failed", not "the model thought so".
  The LLM does what rules can't (reading arbitrary layouts and scans); rules do what LLMs shouldn't (deciding to pay).
- **How do you know the extraction is right?** A schema with no inferred values; per-field confidence (low confidence on a
  critical field → review); an independent parser cross-check; and arithmetic checks that catch most misreads.
- **What if the AI misreads a field?** The cross-check and arithmetic usually catch it, and low confidence flags the invoice for review.
  The reviewer corrects the field, every check re-runs, and the change (old → new, who, why) is in the audit trail.
- **Why process one at a time?** Two invoices against the same PO, processed in parallel, could both pass against the
  same open balance. Order is a control, not a limitation; at scale you'd lock per PO.
- **What if the API is down?** It falls back to the text layer / OCR parser and records that it did.
- **Why flag for review rather than reject the bank change?** Vendors legitimately change banks. Verification is the fix; rejecting
  creates vendor friction.
- **How would this scale to hundreds a month?** Mailbox intake, a queue, ERP connectors for POs and posting, per-vendor
  tolerance tuning from reviewer decisions, and a GRN for a true three-way match.
- **What's the ROI?** About 10–15 min of manual work per invoice down to seconds for straight-through invoices; reviewers only
  see exceptions, with the reason and the fix pre-written; duplicate and fraud payments are blocked.
