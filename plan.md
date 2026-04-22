# PubRecSearch

This system aims to build a large database and datalake for public federal records pertaining to individuals for investigative purposes. 

Requirements: 
1. Automate scraping all sources listed, with sufficient tracking to not duplicate entire systems repeatedly. 
2. Monitor scraping activities and report errors, gaps, and issues. 
3. Store scrape metadata (time, data fetched, etc) in logs for full auditability
4. Store files collected in Cloudflare R2 storage
5. Keep a database of individuals with references to their R2-stored files, data source, date captured and other key information such as relationship to the sourced document. 
6. Other features that facilitate this project. 

# Data systems to include: 

## Federal Financial & Tax Records

- IRS Form 990 – Searchable via ProPublica Nonprofit Explorer or IRS Tax Exempt Organization Search
- SEC EDGAR – All filings continuously published at edgar.sec.gov
- USASpending.gov – Federal contract and grant awards including sole proprietor individuals


## Legal & Law Enforcement

- DOJ Press Releases – Named defendants published at justice.gov as cases are filed/resolved
- PACER – Technically requires a free account but is unrestricted public access with nominal per-page fees (borderline inclusion)
- OFAC SDN List – Fully public, bulk-downloadable at treasury.gov
- ATF Federal Firearms Licensee List – Downloadable at atf.gov
- National Sex Offender Public Website (NSOPW) – Continuously updated, fully public
- BOP Inmate Locator – Publicly searchable at bop.gov


## Elections & Political Finance

- FEC.gov – Full donor and candidate finance data, bulk downloadable
- Lobbying Disclosure Act Database – Searchable at lda.senate.gov
- FARA Database – Searchable at fara.gov


## Professional Licensing & Credentials

- FAA Airmen Inquiry – Publicly searchable at amsrvs.amsrvs.faa.gov
- FCC License Search – Fully public at fcc.gov/uls
- NPPES NPI Registry – Bulk downloadable and searchable at npiregistry.cms.hhs.gov


## Healthcare

- Open Payments (CMS) – Fully public at openpaymentsdata.cms.gov
- OIG Exclusions List – Publicly searchable and downloadable at oig.hhs.gov
- FDA Debarment List – Published at fda.gov


## Property, Intellectual & Business

- USPTO Patent Database – Fully public at patents.google.com / USPTO.gov
- USPTO Trademark Database – Fully public at tmsearch.uspto.gov
- Copyright Office Records – Searchable at cocatalog.loc.gov
- SAM.gov Exclusions – Publicly searchable, no login required for exclusions data


## Congressional & Executive Records

- Congressional Record – Fully public at congress.gov
- House Financial Disclosures – Publicly searchable at disclosures.house.gov
- Senate Financial Disclosures – Publicly searchable at efds.senate.gov
- OGE Financial Disclosures – Executive branch officials at oge.gov


## Environment & Safety

- EPA Enforcement & Compliance (ECHO) – Publicly searchable at echo.epa.gov
- NTSB Accident Database – Fully public at ntsb.gov
- CPSC Recall Database – Fully public at cpsc.gov