# Career boards ranked by tech postings (2026-09-09)

## How this was made

- Source: the `ats-scrapers` **jobhive** dataset, manifest generated `2026-09-09T22:30:04Z` (5,129,319 postings, 80,390 companies, 65 ATSes).
- Script: `scripts/rank_boards_from_jobhive.py` (needs a throwaway venv with `pyarrow fsspec aiohttp pyyaml`; see its docstring — those are **not** project dependencies).
- Only the ATSes `ats_boards` can drive are scanned (41 of 65): aggregators (EURES, Bundesagentur, Arbetsförmedlingen, …), region-locked ATSes (Gupy/Beisen/HRMOS/Wanted) and Avature (needs Browserbase) are out.
- Target countries: the union of the `location` tiers of every profile under `config/` — 47 codes: `AE AL AM AT AZ BA BE BG CH CY CZ DE DK EE ES FI FR GE GR HR HU IE IS IT KG KZ LI LT LU LV MD ME MK MT NL NO PL PT RO RS SE SI SK TR UA UZ XK`.
- A posting counts as **tech** when its title matches `engineer|developer|software|programmer|data|devops|cloud|backend|frontend|full.?stack|embedded|firmware|intern|trainee|graduate|working student|werkstudent|harjoittelija|kehittäjä`, and as **junior** when it additionally does *not* match `senior|lead|principal|staff|manager|director|head of|architect|vp\b`.
- Rows: every `(company, ats)` with at least 5 tech postings in those countries — 1312 of 59,203 boards seen.
- `Board total` is the board's worldwide posting count; boards over ~500 need a per-board `include:` in `config/sources.yaml` or the scrape takes forever.
- The board column comes from that ATS's `companies.csv`. Where the tenant is missing from it (mostly SuccessFactors), the origin of one of its job URLs is used instead — that origin *is* what those scrapers take as their slug, but it stays a guess until `scripts/probe_boards.py` says otherwise.

Boards per ATS: successfactors 179, workday 176, smartrecruiters 163, greenhouse 154, oracle 87, join_com 77, teamtailor 76, recruitee 71, cornerstone 48, phenom 39, softgarden 39, ashby 36, workable 30, lever 25, icims 24, bamboohr 17, jazzhr 16, personio 13, eightfold 9, breezy 9, pinpoint 4, jobvite 4, rippling 4, recruiterbox 3, darwinbox 2, google 1, tiktok 1, amazon 1, tesla 1, dayforce 1, uber 1, taleo 1.

## What went into config/sources.yaml

> Hand-written. `scripts/rank_boards_from_jobhive.py` regenerates everything above this
> section and would overwrite it — paste it back, or move it, if you re-run the script.

63 boards were added on 2026-09-10, roughly the top of this table by tech postings, minus:

- boards already configured (Bosch, SAP, Nokia, Ericsson, Microsoft, Wärtsilä, Volvo, Ubisoft,
  Spotify, Adyen, N26, Celonis, Wolt, Oura, Veriff, Wise, Supercell, Pipedrive);
- **Jobgether** (485 tech, rank 2) and **Bjak** (363, rank 6) — an aggregator and a tenant that
  reposts the same remote role once per country (PL 95, ES 95, SE 95, PT 94 … of 3 066). Neither
  is a company career board;
- the Oracle tenants the dataset only knows by their pod hostname — `eubt.fa.us6.oraclecloud.com`
  (255 tech), `Fa Etjb Saasfaprod1`, `Egup`, `Edel`, `Ialmme`, `Hdpc`. Real boards, but nothing
  says whose, so the postings would arrive with a meaningless company name;
- retail/logistics boards with a bad ratio: LIDL (79 tech of 23 170), JYSK (101 of 2 269),
  Circle K (14 of 9 900), Jerónimo Martins (5 of 1 960);
- **Speechify** (157 tech of 1 086) — one remote role duplicated across many countries;
- the second copy of a company that runs two ATSes at once (Cisco, Mastercard, Thermo Fisher and
  Arcadis each appear twice with identical counts; only one entry per company was added);
- the dedicated-scraper big-tech boards (Google 204, TikTok 110, Amazon 64, Tesla 53). They are
  still commented out in `config/sources.yaml`: 10 000+ postings each and no per-posting
  description fetch, so someone should watch the first run.

Seven boards from well below the top of the table were added anyway, because they sit in the
tier-1/tier-2 countries the rest of the list barely covers: Aiven, Smartly, OP-Palvelut,
Deloitte Nordic, Winthrop Technologies, Nexer Group, Vattenfall.

Together the 63 cover **6 834 tech postings** in the target countries (4 545 of them
non-senior), on boards carrying 81 267 postings worldwide.

All 63 were then verified with `python scripts/probe_boards.py`; every one answered with real
postings and none was rejected. Three Cornerstone tenants (GMV, OHB, imec) needed
`options: {site_id: N}` — see the config comment.

## Things to know about this dataset

- **`company` is not a display name.** For SmartRecruiters/Greenhouse/Ashby it is the tenant slug
  (`BoschGroup`, `aiven36`, `teampicnic`), for Phenom it is the careers host
  (`careers.thalesgroup.com`), and for many Oracle tenants it is the pod hostname. That is why
  most new entries in `config/sources.yaml` carry a `company:` override.
- **120 SuccessFactors tenants are missing from `companies.csv`** — including Alstom, Atos and
  Deloitte Germany. Their board column here is the origin of one of their job URLs; that origin
  is exactly what the SuccessFactors scraper takes as a slug, and it probed clean for all nine
  we used, but treat it as a guess elsewhere.
- **`posted_at` is empty for every SuccessFactors and Workday row**, and `country_iso` is empty
  for a large share of Workday and Oracle rows (their location string is "2 Locations" or a bare
  city name). Both gaps show up in a live scrape too, not just in the dataset.
- **Stale postings are listed as current.** Probe samples came back dated 2025-05-26 (TD SYNNEX),
  2025-07-28 (Picnic), 2025-11-20 (act digital). `max_age_days` in the profile is what saves us.
- **Two Oracle tenants report exactly 10 000 postings** — a scraper page cap, not a real count.
- `welcometothejungle` was dropped from the scan: its 280 "boards" are the WTTJ job board, which
  jobscraper already scrapes through its own `wttj` source.

## Ranking

| # | Company | ATS | Board (URL or slug) | Target postings | Tech | Non-senior tech | Board total | Top countries |
| --: | --- | --- | --- | --: | --: | --: | --: | --- |
| 1 | Sopra Steria | smartrecruiters | https://careers.smartrecruiters.com/SopraSteria1 | 1747 | 521 | 356 | 1953 | FR 888, DE 251, NL 121 |
| 2 | Jobgether | lever | https://jobs.lever.co/jobgether | 1148 | 485 | 214 | 3729 | ES 154, CH 149, DE 142 |
| 3 | Devoteam | smartrecruiters | https://careers.smartrecruiters.com/devoteam | 825 | 385 | 280 | 944 | FR 346, PT 220, ES 68 |
| 4 | Bosch Group | smartrecruiters | https://careers.smartrecruiters.com/BoschGroup | 1308 | 380 | 327 | 4843 | DE 783, PT 216, ES 66 |
| 5 | Thales Group | phenom | https://careers.thalesgroup.com | 1920 | 371 | 243 | 2608 | FR 1477, DE 112, IT 94 |
| 6 | Bjak | ashby | https://jobs.ashbyhq.com/bjakcareer | 769 | 363 | 291 | 3066 | PL 95, ES 95, SE 95 |
| 7 | Inetum | smartrecruiters | https://careers.smartrecruiters.com/inetum2 | 911 | 308 | 225 | 1530 | PT 441, FR 321, PL 79 |
| 8 | Hitachi | workday | https://hitachi.wd1.myworkdayjobs.com/hitachi | 831 | 290 | 221 | 4215 | PL 194, DE 167, SE 151 |
| 9 | eubt.fa.us6.oraclecloud.com | oracle | https://eubt.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 2643 | 255 | 249 | 10000 | DE 2643 |
| 10 | Alstom Transport | successfactors | https://jobsearch.alstom.com | 1080 | 214 | 167 | 2282 | FR 655, DE 74, IT 71 |
| 11 | Google | google | ats: google | 336 | 204 | 127 | 3360 | IE 98, PL 62, DE 49 |
| 12 | ATOS International | successfactors | https://jobs.atos.net | 710 | 198 | 130 | 952 | FR 397, DE 92, NL 82 |
| 13 | Speechify | greenhouse | https://job-boards.greenhouse.io/speechify | 157 | 157 | 80 | 1086 | DE 23, ES 19, FR 18 |
| 14 | Drees & Sommer SE | smartrecruiters | https://careers.smartrecruiters.com/DreesSommerSE | 1492 | 157 | 77 | 1501 | DE 1380, NL 39, AT 16 |
| 15 | Allianz | phenom | https://careers.allianz.com | 806 | 154 | 122 | 1630 | FR 291, DE 201, ES 127 |
| 16 | Liebherr-International S.A. | successfactors | https://careers.liebherr.com | 928 | 142 | 118 | 1161 | DE 596, FR 93, AT 81 |
| 17 | GMV | cornerstone | https://gmv.csod.com/ux/ats/careersite/1/home?c=gmv | 198 | 140 | 121 | 216 | ES 155, DE 22, PL 12 |
| 18 | Roche | phenom | https://careers.roche.com | 451 | 139 | 85 | 1203 | ES 121, CH 114, DE 93 |
| 19 | Deloitte GmbH Wirtschaftsprüfungsgesellschaft | successfactors | https://jobs.deloitte.de | 596 | 138 | 85 | 596 | DE 596 |
| 20 | AECOM | smartrecruiters | https://careers.smartrecruiters.com/aecom2 | 374 | 136 | 58 | 5209 | IE 124, ES 79, PL 67 |
| 21 | ABB | phenom | https://careers.abb | 455 | 134 | 105 | 2072 | DE 143, PL 76, CH 42 |
| 22 | ALTEN | smartrecruiters | https://careers.smartrecruiters.com/alten | 777 | 131 | 107 | 1133 | FR 618, DE 129, AT 25 |
| 23 | Fa Etjb Saasfaprod1 | oracle | https://fa-etjb-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 366 | 130 | 109 | 394 | IT 306, ES 54, DE 3 |
| 24 | Deloitte Netherlands | smartrecruiters | https://careers.smartrecruiters.com/deloittenetherlands | 670 | 130 | 81 | 675 | NL 670 |
| 25 | Talan | smartrecruiters | https://careers.smartrecruiters.com/talan | 383 | 129 | 68 | 470 | FR 301, ES 38, CH 14 |
| 26 | JPMorgan Chase | oracle | https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001 | 280 | 127 | 81 | 7378 | IE 82, DE 61, PL 33 |
| 27 | WenS | successfactors | https://jobs.werkenvoornederland.nl | 2014 | 121 | 92 | 4974 | NL 1971, AT 33, PT 10 |
| 28 | Ramboll | smartrecruiters | https://careers.smartrecruiters.com/ramboll3 | 683 | 115 | 41 | 1160 | DE 294, DK 99, NO 98 |
| 29 | Atlas Copco Group | successfactors | https://career5.successfactors.eu/career?company=atlascopcoP | 304 | 112 | 101 | 1284 | DE 87, BE 79, CZ 34 |
| 30 | Abb | workday | https://abb.wd3.myworkdayjobs.com/External_Career_Page | 350 | 111 | 92 | 2073 | DE 123, PL 53, CH 35 |
| 31 | TikTok | tiktok | ats: tiktok | 341 | 110 | 86 | 4176 | DE 92, IE 71, FR 54 |
| 32 | Levio | successfactors | https://jobsearch.createyourowncareer.com/BFS_Health_Finance | 664 | 110 | 84 | 978 | DE 485, NL 67, ES 21 |
| 33 | act digital | smartrecruiters | https://careers.smartrecruiters.com/AlterSolutions | 228 | 110 | 77 | 261 | PL 93, PT 64, FR 50 |
| 34 | Egup | oracle | https://egup.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX | 241 | 106 | 94 | 2294 | IT 98, IE 88, DE 13 |
| 35 | ABOUT YOU SE & Co. KG | smartrecruiters | https://careers.smartrecruiters.com/aboutyougmbh | 203 | 103 | 27 | 207 | DE 203 |
| 36 | Cisco | phenom | https://careers.cisco.com | 158 | 102 | 73 | 1293 | IE 37, PL 30, NO 21 |
| 37 | JYSK | smartrecruiters | https://careers.smartrecruiters.com/jysk | 1805 | 101 | 12 | 2269 | DE 453, BE 219, IT 200 |
| 38 | Sia | smartrecruiters | https://careers.smartrecruiters.com/sia | 391 | 96 | 65 | 592 | FR 139, NL 106, BE 97 |
| 39 | EssilorLuxottica Group | successfactors | https://careers.essilorluxottica.com | 220 | 94 | 86 | 4557 | IT 113, FR 52, DE 15 |
| 40 | OHB | cornerstone | https://career-ohb.csod.com/ux/ats/careersite/1/home?c=career-ohb | 229 | 93 | 73 | 229 | DE 222, CZ 7 |
| 41 | Cisco | workday | https://cisco.wd5.myworkdayjobs.com/cisco_careers | 128 | 91 | 67 | 1305 | IE 37, PL 23, NO 21 |
| 42 | Microsoft | eightfold | https://microsoft.eightfold.ai/careers | 205 | 90 | 43 | 2186 | IE 33, NL 22, FR 19 |
| 43 | DHL | phenom | https://careers.dhl.com | 5270 | 89 | 62 | 8890 | DE 4659, NL 164, PL 100 |
| 44 | Salesforce | workday | https://salesforce.wd12.myworkdayjobs.com/external_career_site | 281 | 89 | 60 | 1449 | IE 125, NL 32, FR 30 |
| 45 | Deutsche Börse AG | successfactors | https://career.deutsche-boerse.com | 208 | 85 | 64 | 225 | DE 82, CZ 75, LU 38 |
| 46 | Abbott | workday | https://abbott.wd5.myworkdayjobs.com/abbottcareers | 318 | 81 | 62 | 2686 | IE 74, DE 70, NL 53 |
| 47 | Turner & Townsend | smartrecruiters | https://careers.smartrecruiters.com/turnertownsend | 296 | 80 | 21 | 3069 | DE 67, IE 65, ES 37 |
| 48 | LIDL | successfactors | https://jobs.lidl | 21746 | 79 | 74 | 23170 | DE 16188, NL 1416, FR 1371 |
| 49 | SIXT | smartrecruiters | https://careers.smartrecruiters.com/sixt | 321 | 78 | 65 | 506 | DE 243, ES 33, PT 21 |
| 50 | Picnic | greenhouse | https://job-boards.greenhouse.io/teampicnic | 307 | 78 | 60 | 307 | DE 158, NL 118, FR 31 |
| 51 | Edel | oracle | https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 211 | 75 | 67 | 922 | DE 70, FR 40, NL 21 |
| 52 | Wavestone | smartrecruiters | https://careers.smartrecruiters.com/wavestone1 | 272 | 75 | 53 | 285 | FR 256, CH 6, LU 6 |
| 53 | Vodafone Procurement Company S.a.r.l. | successfactors | https://opportunities.vodafone.com | 573 | 74 | 47 | 1215 | DE 453, ES 36, PT 31 |
| 54 | Nebius | greenhouse | https://job-boards.greenhouse.io/nebius | 139 | 74 | 23 | 377 | NL 84, DE 26, FI 15 |
| 55 | T-Systems Iberia | smartrecruiters | https://careers.smartrecruiters.com/T-SystemsIberia | 118 | 72 | 39 | 118 | ES 118 |
| 56 | Analogdevices | workday | https://analogdevices.wd1.myworkdayjobs.com/external | 96 | 72 | 27 | 833 | IE 74, DE 7, ES 7 |
| 57 | Renesas Electronics | smartrecruiters | https://careers.smartrecruiters.com/renesaselectronics | 107 | 72 | 24 | 890 | DE 47, PL 29, IT 11 |
| 58 | TD SYNNEX | phenom | https://careers.tdsynnex.com | 260 | 71 | 56 | 752 | ES 112, IT 27, FR 17 |
| 59 | Elastic | greenhouse | https://job-boards.greenhouse.io/elastic | 118 | 71 | 10 | 353 | ES 34, PT 21, IE 20 |
| 60 | Kiongroup | workday | https://kiongroup.wd3.myworkdayjobs.com/kiongroup | 397 | 70 | 62 | 969 | DE 112, FR 101, ES 60 |
| 61 | BCG | phenom | https://careers.bcg.com | 284 | 70 | 61 | 909 | DE 55, PT 44, FR 39 |
| 62 | Arion Recruitment | teamtailor | https://arionrecruitment-1705579161.teamtailor.com | 91 | 69 | 49 | 100 | SE 84, DE 2, BE 2 |
| 63 | Stryker | workday | https://stryker.wd1.myworkdayjobs.com/strykercareers | 223 | 69 | 45 | 1420 | DE 68, PL 57, IE 53 |
| 64 | Henkel | cornerstone | https://henkel.csod.com/ux/ats/careersite/1/home?c=henkel | 232 | 68 | 59 | 1005 | DE 121, FR 26, NL 20 |
| 65 | Jci | workday | https://jci.wd5.myworkdayjobs.com/jci | 314 | 68 | 52 | 2631 | DE 88, IE 51, FR 43 |
| 66 | Redcare Pharmacy | smartrecruiters | https://careers.smartrecruiters.com/redcare-pharmacy | 236 | 68 | 21 | 236 | DE 145, NL 77, IT 4 |
| 67 | Synnex | workday | https://synnex.wd5.myworkdayjobs.com/tdsynnexcareers | 227 | 67 | 53 | 753 | ES 84, IT 27, FR 15 |
| 68 | PirelliTyres | successfactors | https://jobs.pirelli.com | 116 | 66 | 64 | 127 | IT 62, DE 43, FR 3 |
| 69 | jobs-placeme | icims | https://jobs-placeme.icims.com | 227 | 66 | 53 | 227 | IE 227 |
| 70 | Philips | phenom | https://www.careers.philips.com | 177 | 65 | 56 | 831 | DE 56, NL 39, PL 27 |
| 71 | PM&S RECURSOS SL | successfactors | https://empleo.es.deloitte.com | 257 | 65 | 49 | 257 | ES 257 |
| 72 | Celonis | greenhouse | https://job-boards.greenhouse.io/celonis | 142 | 65 | 37 | 280 | DE 57, ES 56, NL 9 |
| 73 | Amazon | amazon | ats: amazon | 276 | 64 | 47 | 34601 | FR 164, LU 82, AT 16 |
| 74 | sentinellabs | greenhouse | https://job-boards.greenhouse.io/sentinellabs | 87 | 63 | 3 | 217 | CZ 60, PL 7, NL 6 |
| 75 | Körber Group | successfactors | https://jobs.koerber.com/pharma | 187 | 62 | 54 | 285 | DE 97, PT 58, CH 14 |
| 76 | Marsh | phenom | https://careers.marsh.com | 363 | 62 | 50 | 1928 | DE 81, PL 71, FR 44 |
| 77 | State Street | phenom | https://careers.statestreet.com | 185 | 59 | 37 | 1166 | PL 123, IE 38, DE 9 |
| 78 | Arcadis | eightfold | https://arcadis.eightfold.ai/careers | 269 | 59 | 35 | 1345 | DE 117, NL 76, BE 26 |
| 79 | Arcadis | oracle | https://ebcs.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 269 | 59 | 35 | 1341 | DE 117, NL 76, BE 28 |
| 80 | Spektrum | greenhouse | https://job-boards.greenhouse.io/spektrum | 179 | 59 | 35 | 200 | NO 57, BE 47, FR 30 |
| 81 | Thermo Fisher | phenom | https://jobs.thermofisher.com | 405 | 58 | 54 | 3043 | DE 82, IT 56, NL 49 |
| 82 | Delivery Hero | smartrecruiters | https://careers.smartrecruiters.com/deliveryhero | 197 | 58 | 29 | 971 | ES 78, DE 31, IT 26 |
| 83 | Indie Campers | greenhouse | https://job-boards.greenhouse.io/indiecampers | 454 | 57 | 57 | 697 | IT 110, ES 73, FR 67 |
| 84 | Candidate Experience Site - Campus | oracle | https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 116 | 57 | 55 | 1305 | PL 35, FR 27, DE 20 |
| 85 | knowmad mood | teamtailor | https://knowmadmood.teamtailor.com | 97 | 57 | 43 | 100 | ES 97 |
| 86 | Mastercard | phenom | https://careers.mastercard.com | 138 | 57 | 2 | 1102 | IE 57, PT 37, DE 9 |
| 87 | EVERIENCE | smartrecruiters | https://careers.smartrecruiters.com/EVERIENCE | 345 | 56 | 40 | 351 | FR 266, NL 29, BE 26 |
| 88 | AVL List GmbH | successfactors | https://jobs.avl.com | 114 | 55 | 48 | 211 | DE 34, IT 32, SE 15 |
| 89 | Danaher | phenom | https://jobs.danaher.com | 243 | 55 | 35 | 1340 | PL 55, DE 42, SE 27 |
| 90 | Huawei Research Center Germany | teamtailor | https://huaweiresearchcentergermanyaustria.teamtailor.com | 95 | 55 | 27 | 100 | DE 94, AT 1 |
| 91 | Mastercard | workday | https://mastercard.wd1.myworkdayjobs.com/corporatecareers | 127 | 55 | 2 | 1058 | IE 56, PT 33, DE 7 |
| 92 | The Exploration Company | ashby | https://jobs.ashbyhq.com/the-exploration-company | 82 | 54 | 38 | 94 | DE 47, FR 33, IT 2 |
| 93 | Tesla | tesla | ats: tesla | 93 | 53 | 38 | 6839 | BE 54, FR 38, LU 1 |
| 94 | imec | cornerstone | https://imec.csod.com/ux/ats/careersite/1/home?c=imec | 139 | 52 | 41 | 161 | BE 107, NL 21, DE 4 |
| 95 | Worldline | successfactors | https://jobs.worldline.com | 171 | 52 | 35 | 326 | FR 51, DE 39, PL 35 |
| 96 | Ialmme | oracle | https://ialmme.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/ciklum-career | 64 | 52 | 25 | 246 | PL 46, CZ 17, ES 1 |
| 97 | Daikin Europe NV | successfactors | https://careers.daikin.eu/dag | 169 | 51 | 46 | 185 | BE 55, FR 29, PL 20 |
| 98 | Scalable GmbH | smartrecruiters | https://careers.smartrecruiters.com/scalablegmbh | 94 | 51 | 20 | 94 | DE 93, IT 1 |
| 99 | SES | successfactors | https://careers.ses.com | 78 | 51 | 19 | 140 | LU 60, DE 13, NL 3 |
| 100 | Datadog | greenhouse | https://job-boards.greenhouse.io/datadog | 96 | 51 | 13 | 449 | FR 52, NL 14, IE 10 |
| 101 | Thermofisher | workday | https://thermofisher.wd5.myworkdayjobs.com/thermofishercareers | 342 | 50 | 47 | 3049 | DE 60, IT 52, NL 46 |
| 102 | AGCO | successfactors | https://careers.agcocorp.com | 159 | 50 | 45 | 294 | DE 117, IT 25, FR 11 |
| 103 | Aptiv | workday | https://aptiv.wd5.myworkdayjobs.com/aptiv_careers | 121 | 50 | 33 | 724 | PL 50, FR 27, DE 27 |
| 104 | Euroclear | oracle | https://don.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1003 | 117 | 50 | 32 | 137 | PL 92, BE 20, FR 4 |
| 105 | isaraerospace | greenhouse | https://job-boards.greenhouse.io/isaraerospace | 96 | 49 | 37 | 97 | DE 81, SE 8, NO 7 |
| 106 | Sargent & Lundy | icims | https://international-sargentlundy.icims.com | 75 | 49 | 20 | 1955 | DE 75 |
| 107 | B. Braun Melsungen AG | successfactors | https://jobs.bbraun.com | 443 | 48 | 36 | 902 | DE 316, ES 39, PL 36 |
| 108 | Databricks | greenhouse | https://job-boards.greenhouse.io/databricks | 116 | 48 | 12 | 868 | DE 33, NL 28, FR 19 |
| 109 | CMA-CGM | successfactors | https://jobs.cmacgm-group.com/CEVALogistics | 548 | 47 | 37 | 1171 | FR 230, DE 127, NL 108 |
| 110 | Jj | workday | https://jj.wd5.myworkdayjobs.com/displacedemployees | 246 | 47 | 30 | 1758 | BE 51, CH 50, NL 38 |
| 111 | Bayer | successfactors | https://jobs.bayer.com | 237 | 47 | 29 | 606 | DE 141, PL 44, FR 15 |
| 112 | AFRY | smartrecruiters | https://careers.smartrecruiters.com/afry | 694 | 47 | 22 | 934 | SE 296, DE 104, CZ 76 |
| 113 | Eurofins | smartrecruiters | https://careers.smartrecruiters.com/eurofins | 1206 | 46 | 43 | 2548 | FR 582, DE 184, NL 156 |
| 114 | Securitas | smartrecruiters | https://careers.smartrecruiters.com/securitas | 1023 | 46 | 41 | 1167 | DE 321, PL 280, NL 140 |
| 115 | Doctolib | greenhouse | https://job-boards.greenhouse.io/doctolib | 104 | 46 | 12 | 130 | DE 50, FR 43, IT 11 |
| 116 | Kuehne+Nagel | phenom | https://jobs.kuehne-nagel.com | 408 | 45 | 31 | 1069 | DE 178, FR 59, NL 32 |
| 117 | Bayer | eightfold | https://bayer.eightfold.ai/careers | 224 | 45 | 27 | 586 | DE 127, PL 45, FR 15 |
| 118 | UMGC | workday | https://umgc.wd1.myworkdayjobs.com/umgc_careers | 47 | 44 | 44 | 169 | DE 37, IT 5, ES 3 |
| 119 | Capco | greenhouse | https://job-boards.greenhouse.io/capco | 160 | 44 | 23 | 716 | PL 52, DE 40, BE 25 |
| 120 | Open jobs at ANDRITZ | successfactors | https://careers.andritz.com | 203 | 43 | 36 | 556 | AT 80, DE 75, IT 25 |
| 121 | Terma | successfactors | https://career5.successfactors.eu/career?company=termaas | 79 | 43 | 32 | 91 | DK 56, DE 15, NL 6 |
| 122 | WPP Media | greenhouse | https://job-boards.greenhouse.io/wppmedia | 308 | 43 | 32 | 1020 | DE 127, NL 41, PL 36 |
| 123 | Merck Group | phenom | https://careers.merckgroup.com | 169 | 43 | 26 | 871 | DE 55, CH 23, FR 20 |
| 124 | Kyndryl | workday | https://kyndryl.wd5.myworkdayjobs.com/kyndrylprofessionalcareers | 114 | 43 | 23 | 1050 | ES 43, NL 29, IT 17 |
| 125 | Fa Extu Saasfaprod1 | oracle | https://fa-extu-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 67 | 43 | 12 | 213 | PL 50, DE 5, ES 4 |
| 126 | Grafana Labs | greenhouse | https://job-boards.greenhouse.io/grafanalabs | 58 | 43 | 6 | 131 | SE 15, ES 14, DE 11 |
| 127 | Efuf | oracle | https://efuf.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 601 | 42 | 41 | 717 | DE 307, IT 67, NL 61 |
| 128 | Applied Materials | eightfold | https://appliedmaterials.eightfold.ai/careers | 77 | 42 | 39 | 1926 | IT 28, DE 27, FR 9 |
| 129 | Enovos International S.A | successfactors | https://jobs.encevo.eu | 147 | 42 | 33 | 147 | LU 147 |
| 130 | HPE | phenom | https://careers.hpe.com | 77 | 42 | 32 | 1150 | IE 16, FR 10, NL 8 |
| 131 | Honeywell | oracle | https://ibqbjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 139 | 42 | 30 | 1349 | DE 39, NL 25, PL 24 |
| 132 | Danaher | workday | https://danaher.wd1.myworkdayjobs.com/danaherjobs | 170 | 42 | 25 | 1341 | PL 42, SE 27, DE 22 |
| 133 | HelloFresh | greenhouse | https://job-boards.greenhouse.io/hellofresh | 168 | 42 | 13 | 458 | DE 99, PL 34, NL 14 |
| 134 | AccorHotel | smartrecruiters | https://careers.smartrecruiters.com/AccorHotel | 1374 | 41 | 39 | 6262 | FR 916, ES 85, NL 83 |
| 135 | Fyld | jazzhr | https://mainfyld.applytojob.com | 58 | 41 | 39 | 59 | PT 55, BE 3 |
| 136 | KBC GROEP NV | successfactors | https://careers.kbc-group.com | 230 | 41 | 37 | 262 | CZ 165, BE 65 |
| 137 | Emerson Career Site | oracle | https://hdjq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 86 | 41 | 32 | 1019 | DE 38, IT 13, PL 9 |
| 138 | SGS | smartrecruiters | https://careers.smartrecruiters.com/sgs | 914 | 41 | 32 | 4410 | FR 310, ES 171, PL 125 |
| 139 | Airliquidehr | workday | https://airliquidehr.wd3.myworkdayjobs.com/airliquideexternalcareer | 451 | 41 | 31 | 1103 | FR 255, IT 52, DE 35 |
| 140 | BESIX | smartrecruiters | https://careers.smartrecruiters.com/besix | 291 | 41 | 28 | 342 | BE 209, NL 72, LU 7 |
| 141 | SONS Germany | softgarden | https://sonsgermany.career.softgarden.de/ | 354 | 40 | 40 | 354 | DE 348, CH 4, AT 2 |
| 142 | DTU Career Site | oracle | https://efzu.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001 | 160 | 40 | 39 | 160 | DK 160 |
| 143 | Deloitte | smartrecruiters | https://careers.smartrecruiters.com/deloittenordic | 155 | 40 | 36 | 156 | SE 77, FI 33, DK 29 |
| 144 | STOW Group | smartrecruiters | https://careers.smartrecruiters.com/stowgroup | 101 | 40 | 34 | 146 | BE 54, CZ 16, FR 13 |
| 145 | Intesa Sanpaolo Group | successfactors | https://jobs.intesasanpaolo.com | 105 | 40 | 32 | 306 | IT 96, FR 3, CZ 2 |
| 146 | Westinghouse Electric Company, LLC | successfactors | https://careers.westinghousenuclear.com | 159 | 40 | 32 | 1127 | FR 45, IT 41, DE 25 |
| 147 | xebiacee | greenhouse | https://job-boards.greenhouse.io/xebiacee | 61 | 40 | 10 | 72 | PL 54, IT 5, SE 2 |
| 148 | sungrow-emea | personio | https://sungrow-emea.jobs.personio.com | 96 | 39 | 38 | 148 | DE 38, PL 17, IT 13 |
| 149 | SuperMicroComputer | successfactors | https://jobs.supermicro.com | 136 | 39 | 38 | 976 | NL 97, ES 9, DE 8 |
| 150 | Kraft Heinz | eightfold | https://kraftheinz.eightfold.ai/careers | 121 | 39 | 34 | 810 | NL 101, ES 4, SE 3 |
| 151 | Nokia | oracle | https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 90 | 39 | 33 | 608 | PT 35, FI 18, PL 15 |
| 152 | Hpe | workday | https://hpe.wd5.myworkdayjobs.com/acjobsite | 87 | 39 | 28 | 1355 | IE 23, CZ 12, FR 10 |
| 153 | GE HealthCare | phenom | https://careers.gehealthcare.com | 127 | 39 | 26 | 986 | FR 31, NO 19, DE 17 |
| 154 | Medtronic (Redeploymentmedtroniccareers) | workday | https://medtronic.wd1.myworkdayjobs.com/redeploymentmedtroniccareers | 185 | 39 | 18 | 1183 | NL 45, IE 34, PL 29 |
| 155 | Affirm | greenhouse | https://job-boards.greenhouse.io/affirm | 40 | 39 | 13 | 199 | PL 20, ES 20 |
| 156 | Sigma Software | smartrecruiters | https://careers.smartrecruiters.com/sigmasoftware2 | 48 | 39 | 7 | 109 | PL 45, DE 2, PT 1 |
| 157 | Growin | jazzhr | https://growininsights.applytojob.com | 59 | 38 | 38 | 59 | PT 51, BE 8 |
| 158 | Careers at Marriott | oracle | https://ejwl.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 460 | 38 | 35 | 9924 | IT 130, DE 108, ES 64 |
| 159 | Landskill | jazzhr | https://therosdigitalportugal.applytojob.com | 60 | 38 | 35 | 60 | PT 57, BE 3 |
| 160 | Veolia Environnement SA | smartrecruiters | https://careers.smartrecruiters.com/VeoliaEnvironnementSA | 1889 | 38 | 35 | 2903 | FR 1197, ES 366, BE 100 |
| 161 | BorgWarner | workday | https://borgwarner.wd5.myworkdayjobs.com/borgwarner_careers | 96 | 38 | 33 | 314 | PL 49, PT 21, DE 19 |
| 162 | Jan De Nul Group | icims | https://careers-jandenul.icims.com | 85 | 38 | 32 | 101 | BE 82, LU 3 |
| 163 | Ericsson | successfactors | https://career2.successfactors.eu/career?company=Ericsson | 61 | 38 | 30 | 499 | PL 23, SE 19, DE 7 |
| 164 | Statestreet | workday | https://statestreet.wd1.myworkdayjobs.com/global | 124 | 38 | 28 | 1166 | PL 72, IE 32, LU 7 |
| 165 | Fa Eoic Saasfaprod1 | oracle | https://fa-eoic-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_12001 | 483 | 38 | 26 | 664 | FR 435, IT 24, BE 8 |
| 166 | Justera Group | teamtailor | https://justergroupab.teamtailor.com | 65 | 38 | 25 | 74 | SE 65 |
| 167 | Medtronic | workday | https://medtronic.wd1.myworkdayjobs.com/medtroniccareers | 164 | 38 | 17 | 1122 | NL 36, IE 30, PL 30 |
| 168 | Kanadevia Inova | smartrecruiters | https://careers.smartrecruiters.com/kanadeviainova | 120 | 38 | 15 | 197 | DE 41, IT 29, CH 27 |
| 169 | Rhe (R1111) | workday | https://rhe.wd3.myworkdayjobs.com/R1111 | 617 | 37 | 34 | 940 | DE 355, ES 88, PL 53 |
| 170 | Winthrop Technologies | workable | https://apply.workable.com/winthrop-technologies | 189 | 37 | 34 | 224 | FI 75, SE 38, IE 38 |
| 171 | ArcelorMittal | oracle | https://emfg.fa.em4.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 541 | 37 | 31 | 791 | FR 200, PL 164, DE 63 |
| 172 | Deloitte General Services | successfactors | https://jobs.deloitte.lu | 102 | 37 | 30 | 102 | LU 102 |
| 173 | Simon-Kucher | cornerstone | https://simon-kucher.csod.com/ux/ats/careersite/1/home?c=simon-kucher | 87 | 37 | 24 | 132 | DE 34, PL 16, ES 9 |
| 174 | Exadel | greenhouse | https://job-boards.greenhouse.io/externaljobboards | 42 | 37 | 5 | 75 | PL 42 |
| 175 | Veeva Systems | lever | https://jobs.lever.co/veeva | 203 | 37 | 4 | 897 | ES 64, DE 55, PL 23 |
| 176 | NSC Global | icims | https://careers-nscglobal.icims.com | 36 | 36 | 36 | 44 | NO 15, SE 11, FI 5 |
| 177 | Pandox Belgium | teamtailor | https://pandox-belgium-careers.teamtailor.com | 39 | 36 | 36 | 39 | BE 39 |
| 178 | Freseniusmedicalcare | workday | https://freseniusmedicalcare.wd3.myworkdayjobs.com/fme | 203 | 36 | 35 | 2997 | DE 81, PL 59, PT 18 |
| 179 | Citi | workday | https://citi.wd5.myworkdayjobs.com/2 | 197 | 36 | 18 | 4398 | PL 121, IE 46, FR 10 |
| 180 | EY Global Services | successfactors | https://careers.ey.com/ey | 207 | 36 | 7 | 8321 | IE 138, LU 57, CZ 10 |
| 181 | Mdlz | workday | https://mdlz.wd3.myworkdayjobs.com/external | 140 | 35 | 32 | 1369 | PL 28, ES 20, FR 20 |
| 182 | K-tronik GmbH | join_com | https://join.com/companies/k-tronik | 54 | 35 | 30 | 54 | DE 54 |
| 183 | Netcompany | smartrecruiters | https://careers.smartrecruiters.com/netcompany1 | 77 | 35 | 27 | 159 | DK 34, BE 18, PL 7 |
| 184 | Bundesdruckerei  GmbH | softgarden | https://bundesdruckerei.career.softgarden.de/ | 83 | 35 | 25 | 83 | DE 83 |
| 185 | Carrier | workday | https://carrier.wd5.myworkdayjobs.com/jobs | 267 | 35 | 22 | 1109 | DE 105, NL 34, FR 33 |
| 186 | Veeam Software | greenhouse | https://job-boards.greenhouse.io/veeamsoftware | 55 | 35 | 22 | 231 | PL 24, DE 12, PT 6 |
| 187 | ARHS | smartrecruiters | https://careers.smartrecruiters.com/arhs | 70 | 35 | 21 | 78 | LU 29, PL 18, BE 16 |
| 188 | InPost | smartrecruiters | https://careers.smartrecruiters.com/InPost | 134 | 35 | 18 | 134 | FR 62, PL 43, NL 23 |
| 189 | CoreWeave Europe | greenhouse | https://job-boards.greenhouse.io/coreweaveu | 36 | 35 | 11 | 73 | PL 31, SE 2, IE 2 |
| 190 | Edbz | oracle | https://edbz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX | 44 | 34 | 34 | 685 | DE 33, PL 6, DK 2 |
| 191 | Voith GmbH | successfactors | https://jobs.voith.com | 197 | 34 | 32 | 389 | DE 179, IT 8, AT 5 |
| 192 | Vattenfall | smartrecruiters | https://careers.smartrecruiters.com/Vattenfall | 315 | 34 | 27 | 316 | SE 213, DE 70, NL 20 |
| 193 | NEXTON | smartrecruiters | https://careers.smartrecruiters.com/NEXTON | 167 | 34 | 26 | 167 | FR 167 |
| 194 | schubergphilis | greenhouse | https://job-boards.greenhouse.io/schubergphilis | 59 | 34 | 26 | 61 | NL 58, SE 1 |
| 195 | Döhler GmbH | successfactors | https://jobs.doehler.com | 198 | 34 | 22 | 223 | DE 176, NL 16, PL 4 |
| 196 | Everpure | greenhouse | https://job-boards.greenhouse.io/purestorage | 46 | 34 | 22 | 319 | CZ 38, FR 3, DE 3 |
| 197 | BEUMER Group | smartrecruiters | https://careers.smartrecruiters.com/BEUMERGroup1 | 139 | 34 | 20 | 229 | DE 67, NL 12, BE 10 |
| 198 | Mirantis | smartrecruiters | https://careers.smartrecruiters.com/mirantis | 39 | 34 | 14 | 89 | PL 16, CZ 11, ES 6 |
| 199 | DNV careers | oracle | https://ecyq.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 106 | 33 | 31 | 255 | NO 51, NL 20, DE 11 |
| 200 | Xebia | recruitee | _(unknown — not in companies.csv)_ | 39 | 33 | 30 | 41 | NL 30, BE 9 |
| 201 | AbbVie | smartrecruiters | https://careers.smartrecruiters.com/abbvie | 294 | 33 | 26 | 1838 | DE 132, FR 59, IE 47 |
| 202 | KPMG Advisory Careers | successfactors | https://careers.kpmg.it | 205 | 33 | 26 | 205 | IT 205 |
| 203 | Anton Paar | smartrecruiters | https://careers.smartrecruiters.com/AntonPaar1 | 160 | 33 | 24 | 224 | AT 101, DE 28, IT 14 |
| 204 | Tomra | smartrecruiters | https://careers.smartrecruiters.com/tomra | 83 | 32 | 32 | 179 | DE 18, PL 15, FR 13 |
| 205 | Susquehanna International Group, LLP | icims | https://careers-sig.icims.com | 51 | 32 | 31 | 264 | IE 51 |
| 206 | Demegroup | workday | https://demegroup.wd3.myworkdayjobs.com/careers_external | 63 | 32 | 30 | 123 | BE 44, NL 17, LU 1 |
| 207 | Goodbaby (Europe) GmbH & Co. KG | successfactors | https://careers.cybex-online.com | 182 | 32 | 26 | 197 | DE 149, IT 14, CZ 8 |
| 208 | Ubisoft | smartrecruiters | https://careers.smartrecruiters.com/ubisoft2 | 94 | 32 | 20 | 284 | FR 83, SE 8, ES 2 |
| 209 | IQVIA | workday | https://iqvia.wd1.myworkdayjobs.com/iqvia | 357 | 32 | 18 | 1922 | DE 131, ES 49, PL 44 |
| 210 | STRABAG | cornerstone | https://strabag.csod.com/ux/ats/careersite/2/home?c=strabag | 1559 | 31 | 31 | 1670 | DE 1293, AT 110, PL 101 |
| 211 | Eurazeo | cornerstone | https://eurazeo.csod.com/ux/ats/careersite/1/home?c=eurazeo | 39 | 31 | 30 | 48 | FR 37, IT 2 |
| 212 | Fa Exxd Saasfaprod1 | oracle | https://fa-exxd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_3 | 93 | 31 | 30 | 93 | DE 93 |
| 213 | EWOR GmbH | teamtailor | https://eworgmbh.teamtailor.com | 51 | 31 | 28 | 100 | DE 7, IT 6, PL 6 |
| 214 | TYTAN Technologies GmbH | recruitee | https://tytantechnologiesgmbh.recruitee.com | 61 | 31 | 26 | 61 | DE 61 |
| 215 | Icfcjb | oracle | https://icfcjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Aerospace | 71 | 31 | 24 | 883 | CZ 49, IT 8, FR 7 |
| 216 | Hctz | oracle | https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001 | 54 | 31 | 16 | 701 | CZ 31, IE 10, DE 6 |
| 217 | Alan | ashby | https://jobs.ashbyhq.com/alan | 91 | 30 | 27 | 114 | FR 78, BE 11, ES 2 |
| 218 | Erste Group | successfactors | https://erstegroup-careers.com | 331 | 30 | 25 | 406 | CZ 198, AT 133 |
| 219 | MSD | phenom | https://jobs.msd.com | 160 | 30 | 24 | 595 | NL 63, DE 24, FR 16 |
| 220 | Huawei Europe | teamtailor | https://huaweidusseldorf-1719303222.teamtailor.com | 86 | 30 | 21 | 100 | DE 20, PL 16, NL 14 |
| 221 | Digital Realty Global | oracle | https://hdep.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 54 | 30 | 19 | 81 | NL 14, FR 12, DE 11 |
| 222 | aaff B.V. | recruitee | https://aaff.recruitee.com | 330 | 29 | 29 | 330 | NL 330 |
| 223 | VDK Groep B.V. | recruitee | https://vdkgroep.recruitee.com | 493 | 29 | 26 | 659 | NL 493 |
| 224 | Nexer Group | teamtailor | https://nexergroup.teamtailor.com | 99 | 29 | 22 | 100 | SE 96, DK 2, CZ 1 |
| 225 | IRIUM Portugal | jazzhr | https://iriumportugal.applytojob.com | 40 | 29 | 21 | 56 | PT 40 |
| 226 | TransPerfect | recruitee | https://transperfect.recruitee.com | 103 | 29 | 19 | 598 | ES 43, PL 14, PT 12 |
| 227 | ELCA Global Career site | oracle | https://iaaras.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 47 | 29 | 8 | 47 | CH 47 |
| 228 | IMC | greenhouse | https://job-boards.greenhouse.io/imc | 46 | 28 | 28 | 175 | NL 27, CH 11, DK 8 |
| 229 | Arista Networks | smartrecruiters | https://careers.smartrecruiters.com/aristanetworks | 33 | 28 | 22 | 241 | PL 14, IE 13, NO 2 |
| 230 | Flextronics | workday | https://flextronics.wd1.myworkdayjobs.com/careers | 72 | 28 | 21 | 1558 | PL 19, AT 16, IE 13 |
| 231 | Axis | workday | https://axis.wd3.myworkdayjobs.com/external_career_site | 47 | 28 | 20 | 101 | SE 38, FR 3, DE 3 |
| 232 | NOV | oracle | https://egay.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 67 | 28 | 20 | 604 | NO 37, DK 13, NL 7 |
| 233 | TELEFÓNICA GLOBAL TECHNOLOGY, S.A. | successfactors | https://jobs.telefonica.com | 179 | 28 | 19 | 276 | ES 176, BE 2, DE 1 |
| 234 | SQLI | smartrecruiters | https://careers.smartrecruiters.com/sqli1 | 113 | 28 | 18 | 238 | FR 80, ES 13, BE 7 |
| 235 | AccorCorpo | smartrecruiters | https://careers.smartrecruiters.com/AccorCorpo | 117 | 28 | 16 | 264 | FR 106, ES 5, DE 4 |
| 236 | Deerns Groep BV | recruitee | https://jobsdeerns.recruitee.com | 71 | 28 | 16 | 81 | NL 25, IT 22, FR 13 |
| 237 | John Sisk & Son | icims | https://careers-sisk.icims.com | 148 | 28 | 12 | 189 | IE 49, DK 35, NL 26 |
| 238 | Egis Group | smartrecruiters | https://careers.smartrecruiters.com/EgisGroup | 458 | 28 | 7 | 1776 | FR 383, IE 46, PL 21 |
| 239 | Anyone AI | ashby | https://jobs.ashbyhq.com/anyone-ai | 87 | 27 | 27 | 209 | PL 11, DE 11, SE 10 |
| 240 | Roland Berger | smartrecruiters | https://careers.smartrecruiters.com/rolandberger | 114 | 27 | 23 | 196 | DE 50, NL 20, PT 9 |
| 241 | Designer Group | workable | https://apply.workable.com/designer-group-1 | 40 | 27 | 17 | 49 | IE 24, DE 16 |
| 242 | Iawmqy | oracle | https://iawmqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/careers | 48 | 27 | 11 | 452 | IE 19, NO 12, PL 6 |
| 243 | Microchip | workday | https://microchiphr.wd5.myworkdayjobs.com/external | 40 | 27 | 11 | 500 | DE 15, IE 10, FR 9 |
| 244 | cbs Corporate Business Solutions GmbH | recruitee | https://cbsconsulting.recruitee.com | 145 | 27 | 9 | 183 | DE 127, AT 7, CH 5 |
| 245 | CERN | smartrecruiters | https://careers.smartrecruiters.com/CERN | 56 | 26 | 26 | 56 | CH 56 |
| 246 | Prada S.p.A. | successfactors | https://jobs.pradagroup.com | 122 | 26 | 25 | 227 | IT 86, DE 12, FR 10 |
| 247 | Aumovio | smartrecruiters | https://careers.smartrecruiters.com/aumovio | 110 | 26 | 22 | 555 | DE 91, PT 5, CZ 5 |
| 248 | Salzgitter Konzern | successfactors | https://jobs.salzgitter-ag.com/alle | 222 | 26 | 22 | 239 | DE 221, NL 1 |
| 249 | GKN Aerospace Careers | successfactors | https://careers.gknaerospace.com | 95 | 26 | 21 | 277 | NL 79, SE 9, DE 6 |
| 250 | nestleHRprdRMK | successfactors | https://jobdetails.nestle.com | 326 | 26 | 21 | 2034 | CH 81, PL 65, DE 51 |
| 251 | CRH Jobs | successfactors | https://jobs.crh.com | 85 | 26 | 18 | 1835 | BE 32, NL 27, FR 11 |
| 252 | EMW, Inc. | workable | https://apply.workable.com/emw | 52 | 26 | 17 | 54 | BE 37, NL 11, PT 2 |
| 253 | Iliad - Free | smartrecruiters | https://careers.smartrecruiters.com/Iliad-Free | 231 | 26 | 17 | 236 | FR 231 |
| 254 | Version 1 | smartrecruiters | https://careers.smartrecruiters.com/version1 | 56 | 26 | 11 | 149 | IE 56 |
| 255 | Ambu A/S | successfactors | https://jobs.ambu.com | 72 | 26 | 10 | 131 | DK 34, DE 25, FR 4 |
| 256 | JetBrains | greenhouse | https://job-boards.greenhouse.io/jetbrains | 62 | 26 | 8 | 72 | DE 49, NL 11, CZ 1 |
| 257 | Fin | greenhouse | https://job-boards.greenhouse.io/intercom | 46 | 26 | 4 | 126 | IE 42, DE 4 |
| 258 | Colgate-Palmolive | successfactors | https://jobs.colgate.com | 81 | 25 | 25 | 532 | DE 20, PL 15, IT 8 |
| 259 | Marquardt Group | cornerstone | https://marquardt-group.csod.com/ux/ats/careersite/1/home?c=marquardt-group | 97 | 25 | 25 | 166 | DE 97 |
| 260 | Dürr Group | oracle | https://fa-eurk-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cx_2 | 110 | 25 | 22 | 110 | DE 110 |
| 261 | GRIFOLS, S.A. | successfactors | https://jobsearch.grifols.com | 147 | 25 | 22 | 855 | ES 85, DE 45, IE 8 |
| 262 | Red Bull | smartrecruiters | https://careers.smartrecruiters.com/RedBull | 269 | 25 | 21 | 1199 | AT 81, DE 54, IT 33 |
| 263 | Bureau Veritas UK | successfactors | https://careers.bureauveritas.com | 736 | 25 | 20 | 2015 | FR 345, ES 167, IT 96 |
| 264 | Jobubersicht bei AXA | icims | https://careers-de-axa.icims.com | 319 | 25 | 20 | 320 | DE 165, CH 152, AT 2 |
| 265 | FMC Technologies, Inc. | successfactors | https://careers.technipfmc.com | 35 | 25 | 20 | 165 | PL 29, NO 4, NL 1 |
| 266 | Melia Hotels International | successfactors | https://careers.melia.com | 486 | 25 | 20 | 585 | ES 351, DE 54, IT 38 |
| 267 | BDO Belgium | icims | https://careers-bdobelgium.icims.com | 141 | 25 | 18 | 141 | BE 141 |
| 268 | Satispay | ashby | https://jobs.ashbyhq.com/satispay | 85 | 25 | 18 | 88 | IT 50, LU 17, ES 7 |
| 269 | Rockwellautomation | workday | https://rockwellautomation.wd1.myworkdayjobs.com/external_rockwell_automation | 59 | 25 | 17 | 418 | PL 45, IT 7, DK 3 |
| 270 | Fa Errt Saasfaprod1 | oracle | https://fa-errt-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001 | 52 | 25 | 14 | 202 | FR 40, CH 9, DE 2 |
| 271 | NielsenIQ | smartrecruiters | https://careers.smartrecruiters.com/nielseniq | 86 | 25 | 14 | 396 | PL 20, DE 15, FR 13 |
| 272 | Betclic Group | breezy | https://betclic-group.breezy.hr | 53 | 25 | 11 | 66 | FR 51, PL 1, PT 1 |
| 273 | Emit | oracle | https://emit.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001 | 158 | 25 | 9 | 3946 | SE 116, IE 42 |
| 274 | LinkedIn Job Wrapping | greenhouse | https://job-boards.greenhouse.io/artefactlinkedin | 39 | 25 | 8 | 104 | FR 21, BE 9, DE 5 |
| 275 | sumup | greenhouse | https://job-boards.greenhouse.io/sumup | 227 | 25 | 8 | 404 | FR 75, IT 52, DE 48 |
| 276 | Nttlimited | workday | https://nttlimited.wd3.myworkdayjobs.com/ntt_careers | 66 | 24 | 21 | 832 | CZ 15, DE 10, NL 10 |
| 277 | ebm-papst | cornerstone | https://ebmpapst.csod.com/ux/ats/careersite/4/home?c=ebmpapst | 136 | 24 | 20 | 136 | DE 136 |
| 278 | SCHOTT AG | successfactors | https://join.schott.com | 148 | 24 | 20 | 226 | DE 136, CH 10, CZ 2 |
| 279 | Sonova AG | successfactors | https://jobs.sonova.com | 181 | 24 | 20 | 406 | FR 39, PL 35, DE 29 |
| 280 | Ing | workday | https://ing.wd3.myworkdayjobs.com/icsausdir | 44 | 24 | 18 | 716 | LU 24, ES 20 |
| 281 | Lingaro | lever | https://jobs.lever.co/lingarogroup | 31 | 24 | 18 | 70 | PL 31 |
| 282 | Snowflake | phenom | https://careers.snowflake.com | 36 | 24 | 15 | 366 | PL 11, DE 10, SE 4 |
| 283 | mongodb | greenhouse | https://job-boards.greenhouse.io/mongodb | 57 | 24 | 4 | 406 | IE 47, DE 6, PL 2 |
| 284 | Magna | workday | https://magna.wd3.myworkdayjobs.com/magna | 171 | 23 | 23 | 1396 | DE 76, AT 38, CZ 28 |
| 285 | TalentGo | cornerstone | https://talentgo.csod.com/ux/ats/careersite/1/home?c=talentgo | 65 | 23 | 21 | 65 | ES 64, BE 1 |
| 286 | Pwc | workday | https://pwc.wd3.myworkdayjobs.com/global_campus_careers | 80 | 23 | 19 | 6406 | LU 74, ES 4, IE 2 |
| 287 | Socomec | oracle | https://iahxgs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cx_1001 | 65 | 23 | 19 | 115 | FR 49, BE 5, NL 4 |
| 288 | REXEL | smartrecruiters | https://careers.smartrecruiters.com/rexel1 | 284 | 23 | 18 | 744 | FR 158, NL 38, DE 22 |
| 289 | BASF SE | successfactors | https://basf.jobs/dark_blue_EMEA | 210 | 23 | 17 | 718 | DE 139, ES 33, NL 10 |
| 290 | Natixis in Portugal | smartrecruiters | https://careers.smartrecruiters.com/natixisinportugal | 62 | 23 | 17 | 62 | PT 62 |
| 291 | Hardis | cornerstone | https://hardis.csod.com/ux/ats/careersite/4/home?c=hardis | 73 | 23 | 13 | 73 | FR 69, PL 2, ES 1 |
| 292 | Twoday Denmark | teamtailor | https://twodaydenmark.teamtailor.com | 34 | 23 | 10 | 34 | DK 34 |
| 293 | UCB Pharma | phenom | https://careers.ucb.com | 159 | 23 | 9 | 304 | BE 124, CH 16, DE 12 |
| 294 | Dev | smartrecruiters | https://careers.smartrecruiters.com/dev2 | 208 | 23 | 7 | 4728 | NL 199, IE 5, DE 2 |
| 295 | Garrett Advancing Motion | oracle | https://ehth.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001 | 26 | 22 | 22 | 122 | CZ 26 |
| 296 | Getinge Holding B.V. & Co. KG | successfactors | https://careers.getinge.com | 98 | 22 | 22 | 234 | DE 47, FR 19, SE 10 |
| 297 | Nova Founders Capital | greenhouse | https://job-boards.greenhouse.io/novafounders | 51 | 22 | 22 | 82 | PT 29, DK 20, SE 2 |
| 298 | Ecolab | phenom | https://jobs.ecolab.com | 84 | 22 | 21 | 748 | DE 24, FR 15, IT 9 |
| 299 | Serco | successfactors | https://career2.successfactors.eu/career?company=SercoGroup | 57 | 22 | 21 | 776 | BE 16, DE 12, CZ 8 |
| 300 | Smiths Group | smartrecruiters | https://careers.smartrecruiters.com/smithsgroup2 | 61 | 22 | 19 | 530 | FR 20, DE 18, IE 5 |
| 301 | Webasto | successfactors | https://jobs.webasto.com | 128 | 22 | 19 | 182 | DE 74, LU 33, PT 13 |
| 302 | OCTO Technology | smartrecruiters | https://careers.smartrecruiters.com/octotechnology | 40 | 22 | 17 | 40 | FR 40 |
| 303 | Puig S.L. | successfactors | https://jobs.puig.com | 149 | 22 | 17 | 226 | FR 70, ES 37, DE 20 |
| 304 | SchwarzIT Sourcing GmbH & Co. KG | successfactors | https://schwarz.jobs.schwarz/es | 30 | 22 | 16 | 72 | ES 30 |
| 305 | IG&H | recruitee | https://igh.recruitee.com | 61 | 22 | 15 | 61 | NL 59, PT 2 |
| 306 | Box | greenhouse | https://job-boards.greenhouse.io/boxinc | 27 | 22 | 13 | 149 | PL 26, DE 1 |
| 307 | EOS https://app2.greenhouse.io/job_boards/4008206002/settings | greenhouse | https://job-boards.greenhouse.io/eositsolutions | 49 | 22 | 13 | 148 | IE 29, FI 4, NL 3 |
| 308 | Workday | workday | https://workday.wd5.myworkdayjobs.com/workday | 50 | 22 | 12 | 389 | IE 22, PL 7, IT 6 |
| 309 | Artefact | greenhouse | https://job-boards.greenhouse.io/artefact | 38 | 22 | 6 | 120 | FR 20, BE 9, DE 5 |
| 310 | Miratech | smartrecruiters | https://careers.smartrecruiters.com/miratech1 | 34 | 22 | 5 | 292 | ES 15, PL 14, PT 3 |
| 311 | Kla | workday | https://kla.wd1.myworkdayjobs.com/search | 48 | 21 | 21 | 996 | DE 35, BE 6, FR 3 |
| 312 | METRO/MAKRO | smartrecruiters | https://careers.smartrecruiters.com/metromakro | 1322 | 21 | 20 | 1572 | FR 371, DE 371, PL 221 |
| 313 | Power the World | workday | https://monolithicpower.wd12.myworkdayjobs.com/mps_careers | 26 | 21 | 20 | 247 | ES 14, CH 6, DE 5 |
| 314 | Wabtec | smartrecruiters | https://careers.smartrecruiters.com/wabtec | 94 | 21 | 20 | 670 | DE 36, CZ 20, FR 17 |
| 315 | Applus IDIADA | smartrecruiters | https://careers.smartrecruiters.com/applusidiada1 | 37 | 21 | 19 | 207 | ES 28, CZ 3, DE 2 |
| 316 | Adtran (ADTRAN) | workday | https://adtran.wd3.myworkdayjobs.com/ADTRAN | 35 | 21 | 18 | 91 | DE 28, PL 4, CH 1 |
| 317 | Veralto | phenom | https://jobs.veralto.com | 68 | 21 | 18 | 428 | DE 32, FR 8, NL 8 |
| 318 | BDO | smartrecruiters | https://careers.smartrecruiters.com/BDO4 | 154 | 21 | 16 | 154 | NL 154 |
| 319 | BIL | cornerstone | https://bil.csod.com/ux/ats/careersite/1/home?c=bil | 74 | 21 | 16 | 74 | LU 70, CH 3, FR 1 |
| 320 | IOTA GROUP | smartrecruiters | https://careers.smartrecruiters.com/IOTAGROUP/iota | 69 | 21 | 14 | 121 | FR 49, SE 6, CH 5 |
| 321 | Software Mind | smartrecruiters | https://careers.smartrecruiters.com/softwaremind | 26 | 21 | 9 | 45 | PL 26 |
| 322 | FeverUp | greenhouse | https://job-boards.greenhouse.io/feverup | 46 | 21 | 6 | 617 | ES 41, DE 2, IT 2 |
| 323 | Nexthink | smartrecruiters | https://careers.smartrecruiters.com/nexthink | 47 | 21 | 6 | 104 | ES 25, CH 12, FR 4 |
| 324 | AstraZeneca | eightfold | https://astrazeneca.eightfold.ai/careers | 154 | 21 | 4 | 791 | DE 44, ES 36, IE 22 |
| 325 | ElevenLabs | ashby | https://jobs.ashbyhq.com/elevenlabs | 79 | 20 | 20 | 251 | PL 12, DE 12, SE 9 |
| 326 | Ac | workday | https://arrow.wd1.myworkdayjobs.com/ac | 61 | 20 | 19 | 480 | DE 13, PL 13, FR 10 |
| 327 | Linde | cornerstone | https://linde.csod.com/ux/ats/careersite/1/home?c=linde | 162 | 20 | 19 | 883 | DE 83, FR 17, PT 10 |
| 328 | Terumo-Europe | successfactors | https://careers.terumo-europe.com | 60 | 20 | 19 | 79 | BE 42, DE 11, FR 3 |
| 329 | D-ploy | workable | https://apply.workable.com/d-ploy | 44 | 20 | 18 | 91 | BE 10, DE 9, CH 8 |
| 330 | MULTIVAC | successfactors | https://jobs.multivac.com | 124 | 20 | 18 | 124 | DE 110, AT 12, CH 2 |
| 331 | Celestica Jobs | successfactors | https://careers.celestica.com | 22 | 20 | 17 | 1054 | IE 15, ES 6, DE 1 |
| 332 | Fa Eups Saasfaprod1 | oracle | https://fa-eups-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/ULSolutionsCareers | 44 | 20 | 17 | 428 | IT 17, DE 12, PL 8 |
| 333 | Boston Scientific | eightfold | https://bostonscientific.eightfold.ai/careers | 56 | 20 | 16 | 551 | ES 9, PL 9, FR 9 |
| 334 | Boston Scientific | successfactors | https://jobs.bostonscientific.com | 57 | 20 | 16 | 596 | FR 10, PL 9, ES 7 |
| 335 | EER Poland | teamtailor | https://eerpoland.teamtailor.com | 42 | 20 | 16 | 64 | PL 38, DE 3, PT 1 |
| 336 | Avaloq | smartrecruiters | https://careers.smartrecruiters.com/avaloq1 | 68 | 20 | 14 | 150 | CH 36, DE 29, LU 3 |
| 337 | B3 Consulting Poland | teamtailor | https://b3consultingpoland.teamtailor.com | 39 | 20 | 14 | 62 | PL 39 |
| 338 | Fincons Group | jazzhr | https://fincons.applytojob.com | 70 | 20 | 14 | 71 | IT 42, CH 27, BE 1 |
| 339 | Intermedia Intelligent Communications | pinpoint | https://intermedia.pinpointhq.com | 22 | 20 | 14 | 51 | PT 22 |
| 340 | CITECH | smartrecruiters | https://careers.smartrecruiters.com/CITECH | 55 | 20 | 13 | 55 | FR 55 |
| 341 | expleo-jobs-pt-en | icims | https://expleo-jobs-pt-en.icims.com | 24 | 20 | 13 | 24 | PT 24 |
| 342 | Coherent Corp. US | oracle | https://hcwp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 54 | 20 | 12 | 730 | DE 29, SE 16, CH 7 |
| 343 | Woodward | workday | https://woodward.wd5.myworkdayjobs.com/woodward | 73 | 20 | 12 | 176 | DE 50, PL 23 |
| 344 | GROPYUS | greenhouse | https://job-boards.greenhouse.io/gropyus | 92 | 20 | 10 | 92 | DE 76, AT 16 |
| 345 | AXON | greenhouse | https://job-boards.greenhouse.io/axonag | 29 | 20 | 9 | 514 | DE 12, BE 6, NL 6 |
| 346 | H&M Group | smartrecruiters | https://careers.smartrecruiters.com/HMGroup | 446 | 20 | 9 | 1633 | DE 113, NL 50, SE 47 |
| 347 | eDreams ODIGEO | cornerstone | https://odigeo.csod.com/ux/ats/careersite/1/home?c=odigeo | 51 | 20 | 5 | 51 | ES 50, PT 1 |
| 348 | toast | greenhouse | https://job-boards.greenhouse.io/toast | 21 | 20 | 5 | 334 | IE 21 |
| 349 | Fticonsulting (FTIConsultingCareers) | workday | https://fticonsulting.wd108.myworkdayjobs.com/FTIConsultingCareers | 45 | 19 | 19 | 223 | FR 21, ES 8, BE 8 |
| 350 | Instone Real Estate | successfactors | https://jobs.instone.de/instone | 62 | 19 | 19 | 62 | DE 62 |
| 351 | Jci (JCI Confidential) | workday | https://jci.wd5.myworkdayjobs.com/JCI_Confidential | 82 | 19 | 19 | 262 | NL 45, DE 25, BE 11 |
| 352 | Trane Technologies | workday | https://tranetechnologies.wd12.myworkdayjobs.com/Trane_Technologies_Careers | 70 | 19 | 18 | 1658 | FR 20, DE 12, PL 7 |
| 353 | Fa Erav Saasfaprod1 | oracle | https://fa-erav-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/SEW-jobs-and-career | 127 | 19 | 17 | 127 | DE 94, FR 18, AT 5 |
| 354 | Novanta | workday | https://novanta.wd5.myworkdayjobs.com/novanta-careers | 47 | 19 | 16 | 158 | ES 21, DE 20, CZ 6 |
| 355 | Welo Global | lever | https://jobs.lever.co/weloglobal | 125 | 19 | 13 | 546 | ES 31, FR 14, DE 13 |
| 356 | Colliers International EMEA | smartrecruiters | https://careers.smartrecruiters.com/ColliersInternationalEMEA | 96 | 19 | 12 | 107 | DE 30, PL 27, NL 12 |
| 357 | DKV Mobility | cornerstone | https://dkv-mobility.csod.com/ux/ats/careersite/1/home?c=dkv-mobility | 76 | 19 | 12 | 93 | DE 65, IT 5, PL 3 |
| 358 | PROSTAFF Schweiz GmbH | join_com | https://join.com/companies/prostaff | 36 | 19 | 12 | 36 | CH 36 |
| 359 | Wärtsilä Oyj Abp. | successfactors | https://careers.wartsila.com | 55 | 19 | 12 | 132 | FI 21, NL 10, FR 9 |
| 360 | Wood | oracle | https://ehif.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 38 | 19 | 10 | 1039 | IT 28, ES 3, NO 3 |
| 361 | Citrus Global Ltd | smartrecruiters | https://careers.smartrecruiters.com/citrusgloballtd | 82 | 19 | 5 | 120 | DE 34, CH 17, FR 8 |
| 362 | Graphcore | greenhouse | https://job-boards.greenhouse.io/graphcore | 19 | 19 | 5 | 190 | PL 19 |
| 363 | Exadel Inc (Website) | greenhouse | https://job-boards.greenhouse.io/exadelinc | 23 | 19 | 3 | 46 | PL 23 |
| 364 | DataCamp | greenhouse | https://job-boards.greenhouse.io/datacamp | 22 | 19 | 1 | 33 | BE 18, PT 4 |
| 365 | nord-security | ashby | https://jobs.ashbyhq.com/nord-security | 23 | 19 | 1 | 136 | PL 22, FR 1 |
| 366 | Referral Board | greenhouse | https://job-boards.greenhouse.io/referralsuseonly | 40 | 19 | 1 | 104 | ES 17, DE 6, PT 5 |
| 367 | Congatec AG | successfactors | https://jobs.congatec.com | 34 | 18 | 18 | 48 | DE 32, CZ 2 |
| 368 | PowerCo - Careers | successfactors | https://careers.powerco.de/PowerCo_SE | 61 | 18 | 18 | 76 | ES 51, DE 10 |
| 369 | Provinzial Versicherung | successfactors | https://karriere.provinzial.com/Provinzial | 170 | 18 | 18 | 170 | DE 170 |
| 370 | Tetra Pak | successfactors | https://jobs.tetrapak.com | 42 | 18 | 18 | 315 | FR 8, SE 7, NL 7 |
| 371 | Ferrero International S.A | successfactors | https://jobs.ferrero.com | 174 | 18 | 17 | 505 | FR 57, IT 34, DE 27 |
| 372 | WIK Group | cornerstone | https://wikacademy.csod.com/ux/ats/careersite/1/home?c=wikacademy | 115 | 18 | 17 | 169 | DE 79, CH 18, PL 9 |
| 373 | CACI | eightfold | https://caci.eightfold.ai/careers | 45 | 18 | 15 | 1835 | DE 42, IT 3 |
| 374 | CACI | workday | https://caci.wd1.myworkdayjobs.com/external | 45 | 18 | 15 | 1858 | DE 42, IT 3 |
| 375 | DZ PRIVATBANK | successfactors | https://jobs.dz-privatbank.com | 59 | 18 | 15 | 59 | LU 39, DE 18, CH 2 |
| 376 | Cronos Europa | breezy | https://cronoseuropa.breezy.hr | 52 | 18 | 14 | 52 | BE 21, PL 17, LU 7 |
| 377 | 1GLOBAL | workable | https://apply.workable.com/1global | 30 | 18 | 13 | 38 | DE 13, PT 11, NL 2 |
| 378 | Chiesi Farmaceutici S.p.A. | successfactors | https://careers.chiesi.com | 114 | 18 | 13 | 144 | IT 80, FR 10, ES 8 |
| 379 | Auctane | greenhouse | https://job-boards.greenhouse.io/auctane | 19 | 18 | 12 | 24 | PL 10, ES 9 |
| 380 | PPG | phenom | https://careers.ppg.com | 149 | 18 | 12 | 712 | PL 50, NL 32, DE 22 |
| 381 | INNIO | jobvite | https://jobs.jobvite.com/innio | 100 | 18 | 12 | 226 | AT 71, DE 20, PL 3 |
| 382 | Amgen | workday | https://amgen.wd1.myworkdayjobs.com/careers | 94 | 18 | 11 | 1756 | PT 44, NL 16, IE 11 |
| 383 | soweloconsulting | jazzhr | https://soweloconsulting.applytojob.com | 44 | 18 | 9 | 82 | PL 18, DE 9, NL 7 |
| 384 | Alarm.com | greenhouse | https://job-boards.greenhouse.io/alarmcom | 18 | 18 | 8 | 87 | PL 16, NL 1, ES 1 |
| 385 | Ingrammicro | workday | https://ingrammicro.wd5.myworkdayjobs.com/ingrammicro | 91 | 18 | 8 | 587 | DE 21, ES 11, FR 10 |
| 386 | Merlin Digital Partner | teamtailor | https://merlindigitalpartner-1613753675.teamtailor.com | 30 | 18 | 8 | 30 | ES 30 |
| 387 | Truecaller | greenhouse | https://job-boards.greenhouse.io/truecaller | 21 | 18 | 5 | 33 | SE 21 |
| 388 | Smartly | greenhouse | https://job-boards.greenhouse.io/smartlyio | 21 | 18 | 1 | 70 | FI 16, DE 4, ES 1 |
| 389 | Icbpjb | oracle | https://icbpjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/LazardProfessionalCareers | 57 | 17 | 17 | 102 | FR 50, BE 3, DE 1 |
| 390 | Schindler Group | successfactors | https://job.schindler.com/Schindler | 434 | 17 | 17 | 772 | DE 131, CH 112, FR 89 |
| 391 | expleo-jobs-fr-fr | icims | https://expleo-jobs-fr-fr.icims.com | 499 | 17 | 16 | 499 | FR 499 |
| 392 | Fa Exdu Saasfaprod1 | oracle | https://fa-exdu-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 258 | 17 | 16 | 273 | DE 242, FR 16 |
| 393 | Kion Scs | workday | https://kiongroup.wd3.myworkdayjobs.com/kion_scs | 39 | 17 | 16 | 359 | DE 13, ES 8, IT 7 |
| 394 | Sidel Group | cornerstone | https://sidelgroup.csod.com/ux/ats/careersite/1/home?c=sidelgroup | 56 | 17 | 16 | 135 | FR 28, IT 18, PT 6 |
| 395 | SYNTEGON | smartrecruiters | https://careers.smartrecruiters.com/SYNTEGON | 79 | 17 | 16 | 118 | DE 35, NL 15, CH 12 |
| 396 | Black Semiconductor GmbH | ashby | https://jobs.ashbyhq.com/blacksemiconductor | 36 | 17 | 15 | 36 | DE 36 |
| 397 | dormakaba International Holding GmbH | successfactors | https://jobs.dormakaba.com/Legic | 139 | 17 | 15 | 346 | DE 65, FR 25, CH 18 |
| 398 | Horse Powertrain | teamtailor | https://horse.teamtailor.com | 36 | 17 | 15 | 48 | ES 34, SE 2 |
| 399 | Stoneridge | workday | https://stoneridge.wd5.myworkdayjobs.com/careers | 26 | 17 | 15 | 72 | NL 23, SE 3 |
| 400 | Tenova S.p.A. | successfactors | https://careers.tenova.com | 27 | 17 | 15 | 36 | IT 21, DE 6 |
| 401 | enapply-danone | icims | https://enapply-danone.icims.com | 47 | 17 | 14 | 299 | FR 33, BE 8, PL 5 |
| 402 | Sensor Tower | ashby | https://jobs.ashbyhq.com/sensor-tower | 20 | 17 | 14 | 73 | PL 14, PT 6 |
| 403 | Agilent | workday | https://agilent.wd5.myworkdayjobs.com/agilent_careers | 43 | 17 | 13 | 393 | ES 13, DE 13, DK 6 |
| 404 | expleo-jobs-es-en | icims | https://expleo-jobs-es-en.icims.com | 75 | 17 | 13 | 75 | ES 75 |
| 405 | Outokumpu Oyj | successfactors | https://careers.outokumpu.com | 63 | 17 | 13 | 85 | PL 28, SE 11, DE 8 |
| 406 | Alcon | workday | https://alcon.wd5.myworkdayjobs.com/careers_alcon | 59 | 17 | 11 | 374 | DE 26, PL 17, ES 6 |
| 407 | PlayStation Global | greenhouse | https://job-boards.greenhouse.io/sonyinteractiveentertainmentglobal | 20 | 17 | 11 | 180 | IE 15, NL 2, DE 2 |
| 408 | Paloaltonetworks | workday | https://paloaltonetworks.wd5.myworkdayjobs.com/panwexternalcareers | 91 | 17 | 10 | 1496 | FR 19, ES 13, SE 13 |
| 409 | Tetra Tech | cornerstone | https://tetratech.csod.com/ux/ats/careersite/1/home?c=tetratech | 27 | 17 | 7 | 185 | IE 27 |
| 410 | Moniepoint | greenhouse | https://job-boards.greenhouse.io/moniepoint | 29 | 17 | 6 | 149 | ES 15, PL 12, PT 2 |
| 411 | Aiven | greenhouse | https://job-boards.greenhouse.io/aiven36 | 29 | 17 | 5 | 38 | FI 23, IE 5, SE 1 |
| 412 | Nagarro | smartrecruiters | https://careers.smartrecruiters.com/nagarro1 | 54 | 17 | 5 | 898 | DE 38, FR 8, AT 2 |
| 413 | AstraZeneca | workday | https://astrazeneca.wd3.myworkdayjobs.com/careers | 144 | 17 | 3 | 1150 | DE 52, ES 35, FR 16 |
| 414 | Third-Party Job Posts | greenhouse | https://job-boards.greenhouse.io/cloudbedsthirdpartyboard | 28 | 17 | 0 | 111 | PT 9, CH 4, IE 3 |
| 415 | ATEXIS | smartrecruiters | https://careers.smartrecruiters.com/ATEXIS | 70 | 16 | 16 | 74 | FR 40, ES 29, SE 1 |
| 416 | Clifford Chance | smartrecruiters | https://careers.smartrecruiters.com/CliffordChance | 82 | 16 | 16 | 166 | DE 29, NL 21, PL 13 |
| 417 | Gusti Leder GmbH | recruitee | https://gustileder.recruitee.com | 60 | 16 | 16 | 67 | DE 48, PL 11, AT 1 |
| 418 | ire-erac | icims | https://ire-erac.icims.com | 17 | 16 | 16 | 17 | IE 17 |
| 419 | KWS Saat SE | successfactors | https://jobs.kws.com | 91 | 16 | 16 | 125 | DE 60, FR 7, NL 6 |
| 420 | Omicron Iadzgs | oracle | https://omicron-iadzgs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/OmicronCareers | 32 | 16 | 16 | 52 | AT 26, DE 3, PL 2 |
| 421 | Top Closers | recruiterbox | https://topclosers.hire.trakstar.com/jobs | 32 | 16 | 16 | 1298 | DE 19, ES 13 |
| 422 | BERTRANDT AG | successfactors | https://bertrandt.jobs.hr.cloud.sap | 121 | 16 | 14 | 1412 | FR 85, CZ 25, ES 11 |
| 423 | Rotork | smartrecruiters | https://careers.smartrecruiters.com/rotork1 | 38 | 16 | 14 | 129 | IT 10, NL 9, DE 7 |
| 424 | Sana Klinikum Lichtenberg | oracle | https://fa-eycl-saasfaeuraprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4025 | 1108 | 16 | 14 | 1108 | DE 1107, CH 1 |
| 425 | A.P. Moller - Maersk | workday | https://maersk.wd3.myworkdayjobs.com/maersk_careers | 163 | 16 | 13 | 1966 | DE 48, DK 34, PL 22 |
| 426 | AT&S Austria Technologie & | successfactors | https://career.ats.net | 56 | 16 | 13 | 90 | AT 56 |
| 427 | Cargill, Inc. | successfactors | https://jobs.cargill.com | 215 | 16 | 13 | 1918 | FR 48, NL 42, BE 32 |
| 428 | Telespazio Belgium | breezy | https://telespazio-be.breezy.hr | 18 | 16 | 13 | 19 | NL 15, BE 2, LU 1 |
| 429 | Bolton | successfactors | https://jobs.boltongroup.net | 48 | 16 | 12 | 52 | IT 22, NL 9, DE 8 |
| 430 | Fortnox AB | teamtailor | https://fortnoxab.teamtailor.com | 23 | 16 | 12 | 23 | SE 23 |
| 431 | OP-Palvelut Oy | successfactors | https://op-careers.fi | 49 | 16 | 12 | 49 | FI 49 |
| 432 | HatchD1 | successfactors | https://jobs.hatch.com | 29 | 16 | 11 | 584 | PL 23, DE 6 |
| 433 | Jensen Hughes | greenhouse | https://job-boards.greenhouse.io/jensenhughes | 31 | 16 | 10 | 136 | IE 15, IT 6, BE 5 |
| 434 | OEBB | cornerstone | https://oebb.csod.com/ux/ats/careersite/1/home?c=oebb | 213 | 16 | 10 | 213 | AT 213 |
| 435 | Actual Talent | teamtailor | https://winid.teamtailor.com | 80 | 16 | 10 | 85 | ES 73, DE 4, PT 2 |
| 436 | Boehringer Ingelheim | successfactors | https://career5.successfactors.eu/career?company=BoehringerPRD | 83 | 16 | 9 | 470 | PL 29, DE 28, ES 7 |
| 437 | Warner Bros Discovery | phenom | https://careers.wbd.com | 51 | 16 | 9 | 332 | PL 30, NL 7, FR 6 |
| 438 | Swiss Reinsurance Company Ltd. | successfactors | https://careers.swissre.com | 54 | 16 | 9 | 309 | ES 29, CH 15, DE 5 |
| 439 | Smadex SLU | jazzhr | https://smadexslu.applytojob.com | 24 | 16 | 8 | 42 | ES 23, DE 1 |
| 440 | Dolby Laboratories, Inc. | successfactors | https://careers.dolby.com | 23 | 16 | 7 | 110 | IE 9, PL 8, DE 4 |
| 441 | Hovione | icims | https://pt-careers-hovione.icims.com | 44 | 16 | 6 | 48 | PT 37, IE 7 |
| 442 | Scopely | greenhouse | https://job-boards.greenhouse.io/scopely | 79 | 16 | 6 | 179 | ES 72, IE 6, CH 1 |
| 443 | Sportradar | smartrecruiters | https://careers.smartrecruiters.com/sportradar | 30 | 16 | 5 | 80 | AT 13, PL 7, DE 5 |
| 444 | Wolt - English | greenhouse | https://job-boards.greenhouse.io/wolt | 101 | 16 | 4 | 239 | DE 48, DK 16, SE 10 |
| 445 | Arabian Construction Company | darwinbox | https://accpeoplehr.darwinbox.com/ms/candidate/careers | 24 | 15 | 15 | 24 | AE 24 |
| 446 | BMW AG | successfactors | https://jobs.bmwgroup.com | 57 | 15 | 15 | 895 | AT 16, FR 11, CH 6 |
| 447 | Gsk | workday | https://gsk.wd5.myworkdayjobs.com/gskcareers | 77 | 15 | 15 | 677 | BE 47, FR 22, IT 4 |
| 448 | Normec Healthcare BE | recruitee | https://normechcbe.recruitee.com | 25 | 15 | 15 | 25 | BE 22, NL 3 |
| 449 | Disney (Disneycareerdc) | workday | https://disney.wd5.myworkdayjobs.com/disneycareerdc | 106 | 15 | 14 | 643 | FR 91, DE 4, CH 2 |
| 450 | Hdix | oracle | https://hdix.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 126 | 15 | 14 | 126 | IT 126 |
| 451 | Sonepar | successfactors | https://career.sonepar.com | 31 | 15 | 14 | 941 | IT 27, DE 2, FR 1 |
| 452 | Trench Group | cornerstone | https://trench.csod.com/ux/ats/careersite/1/home?c=trench | 67 | 15 | 14 | 105 | DE 35, FR 19, AT 8 |
| 453 | data4 | bamboohr | https://data4.bamboohr.com/careers | 45 | 15 | 13 | 46 | FR 23, ES 7, IT 5 |
| 454 | Stäubli | smartrecruiters | https://careers.smartrecruiters.com/staubligroup | 97 | 15 | 13 | 124 | FR 44, DE 34, ES 7 |
| 455 | Accelerate | teamtailor | https://accelerate.teamtailor.com | 19 | 15 | 12 | 19 | SE 19 |
| 456 | ALPADIA Language Schools SA | smartrecruiters | https://careers.smartrecruiters.com/AlpadiaLanguageSchoolsSA | 68 | 15 | 12 | 68 | DE 29, FR 20, ES 11 |
| 457 | Eedu | oracle | https://eedu.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1003 | 119 | 15 | 12 | 581 | FR 29, PT 20, ES 15 |
| 458 | AIXTRON SE | softgarden | https://aixtron.career.softgarden.de/ | 35 | 15 | 11 | 41 | DE 35 |
| 459 | Continental | smartrecruiters | https://careers.smartrecruiters.com/continental | 274 | 15 | 11 | 865 | DE 158, AT 72, PT 26 |
| 460 | expleo-jobs-be-en | icims | https://expleo-jobs-be-en.icims.com | 24 | 15 | 11 | 24 | BE 24 |
| 461 | adesso Belgium | recruitee | https://adesso1.recruitee.com | 34 | 15 | 10 | 35 | BE 34 |
| 462 | Iahtgs | oracle | https://iahtgs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 24 | 15 | 9 | 25 | ES 24 |
| 463 | OPTIVEUM sp. z o.o. | teamtailor | https://optiveumspzoo.teamtailor.com | 28 | 15 | 8 | 28 | PL 28 |
| 464 | Pertemps ERP | join_com | https://join.com/companies/pertempserp | 87 | 15 | 8 | 99 | DE 60, PL 14, AT 7 |
| 465 | Collaboration Betters The World GmbH | join_com | https://join.com/companies/positivethinkingcompanytech | 18 | 15 | 7 | 18 | DE 18 |
| 466 | Bridgestone Europe NV/SA | successfactors | https://careers.bridgestone-emea.com | 149 | 14 | 14 | 172 | FR 44, PL 42, ES 34 |
| 467 | Foodora Italia | recruiterbox | https://foodoraitalia.recruiterbox.com | 22 | 14 | 14 | 22 | IT 22 |
| 468 | HSH Management Services Limited | successfactors | https://careers.hshgroup.com/ThePeninsula | 43 | 14 | 14 | 260 | FR 43 |
| 469 | Solvay Jobs | successfactors | https://careers.solvay.com | 38 | 14 | 14 | 71 | FR 13, PT 9, BE 6 |
| 470 | XIAO Beteiligungsgesellschaft mbH | recruitee | https://xiaorestaurant.recruitee.com | 139 | 14 | 14 | 139 | DE 139 |
| 471 | XIAO Beteiligungsgesellschaft mbH | join_com | https://join.com/companies/xiao-restaurant | 91 | 14 | 14 | 91 | DE 91 |
| 472 | ALEWIJNSE | recruitee | https://alewijnse.recruitee.com | 28 | 14 | 13 | 41 | NL 27, FR 1 |
| 473 | Artelia | smartrecruiters | https://careers.smartrecruiters.com/Artelia | 350 | 14 | 13 | 495 | FR 340, BE 8, CH 2 |
| 474 | BayWa AG | smartrecruiters | https://careers.smartrecruiters.com/BayWaAG | 968 | 14 | 13 | 968 | DE 968 |
| 475 | Canon | oracle | https://ejqe.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 66 | 14 | 13 | 114 | NL 13, DE 12, IT 10 |
| 476 | iCapital | greenhouse | https://job-boards.greenhouse.io/icapitalnetwork | 24 | 14 | 13 | 208 | PT 22, CH 2 |
| 477 | Strukton Nederland | smartrecruiters | https://careers.smartrecruiters.com/StruktonNederland | 116 | 14 | 13 | 116 | NL 105, BE 11 |
| 478 | Fa Eosd Saasfaprod1 | oracle | https://fa-eosd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001 | 70 | 14 | 12 | 70 | DK 70 |
| 479 | Micron | workday | https://micron.wd1.myworkdayjobs.com/external | 19 | 14 | 12 | 2877 | IT 12, DE 7 |
| 480 | Catawiki | greenhouse | https://job-boards.greenhouse.io/catawiki | 40 | 14 | 11 | 46 | NL 15, PT 10, DE 7 |
| 481 | Generac | workday | https://generac.wd5.myworkdayjobs.com/external | 25 | 14 | 11 | 479 | IT 19, ES 4, FR 1 |
| 482 | Intertek | workable | https://apply.workable.com/intertek | 122 | 14 | 11 | 149 | IT 53, ES 29, PL 11 |
| 483 | T.EN Career Site | oracle | https://hcxg.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 36 | 14 | 11 | 153 | FR 26, ES 6, IT 3 |
| 484 | VALEURIAD | recruitee | https://valeuriad.recruitee.com | 22 | 14 | 11 | 22 | FR 22 |
| 485 | Circle K | phenom | https://workwithus.circlek.com | 612 | 14 | 11 | 9900 | NL 230, DK 162, PL 52 |
| 486 | Feedzai | greenhouse | https://job-boards.greenhouse.io/feedzai | 27 | 14 | 10 | 34 | PT 23, DE 2, NL 1 |
| 487 | Iaings | oracle | https://iaings.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/SinchCareer | 23 | 14 | 10 | 65 | SE 16, ES 5, DE 2 |
| 488 | Umicore | successfactors | https://careers.umicore.com | 148 | 14 | 10 | 202 | BE 78, DE 39, PL 15 |
| 489 | DKB Code Factory | greenhouse | https://job-boards.greenhouse.io/dkbcodefactory | 23 | 14 | 9 | 26 | ES 14, DE 9 |
| 490 | clearspace | bamboohr | https://clearspace.bamboohr.com/careers | 27 | 14 | 8 | 43 | LU 15, CH 10, FR 2 |
| 491 | Copaco Nederland BV | recruitee | https://nl.werkenbijcopaco.com | 30 | 14 | 8 | 30 | NL 26, BE 4 |
| 492 | Evolution | smartrecruiters | https://careers.smartrecruiters.com/evolution | 54 | 14 | 8 | 299 | SE 13, PL 10, ES 8 |
| 493 | NVIDIA | workday | https://nvidia.wd5.myworkdayjobs.com/nvidiaexternalcareersite | 22 | 14 | 7 | 2607 | DK 6, CH 5, FR 3 |
| 494 | SUEZ | cornerstone | https://hris-suez.csod.com/ux/ats/careersite/1/home?c=hris-suez | 590 | 14 | 7 | 639 | FR 581, PL 3, BE 3 |
| 495 | Thought Machine | ashby | https://jobs.ashbyhq.com/thought-machine | 15 | 14 | 7 | 40 | PT 15 |
| 496 | Dynatrace | successfactors | https://career41.sapsf.com/career?company=dynatracel | 28 | 14 | 6 | 109 | AT 14, ES 4, DE 3 |
| 497 | Globaldev Group | workable | https://apply.workable.com/globaldevgroup | 14 | 14 | 6 | 16 | PL 11, ES 1, PT 1 |
| 498 | Tripadvisor | greenhouse | https://job-boards.greenhouse.io/tripadvisor | 19 | 14 | 4 | 96 | PL 15, ES 2, IE 2 |
| 499 | ASM | greenhouse | https://job-boards.greenhouse.io/asm | 21 | 14 | 3 | 448 | NL 8, BE 5, DE 3 |
| 500 | MACOM Technology Solutions | cornerstone | https://macomtech.csod.com/ux/ats/careersite/1/home?c=macomtech | 23 | 14 | 3 | 222 | FR 11, IE 7, CH 4 |
| 501 | Reasonable Accommodation Sabre | workday | https://sabre.wd1.myworkdayjobs.com/sabrejobs | 29 | 14 | 3 | 124 | PL 28, ES 1 |
| 502 | ascent | jazzhr | https://ascent.applytojob.com | 15 | 14 | 2 | 19 | PT 9, DE 3, AT 3 |
| 503 | Fundraise Up | greenhouse | https://job-boards.greenhouse.io/fundraiseup | 41 | 14 | 2 | 114 | PL 15, ES 14, PT 12 |
| 504 | Trading212 | ashby | https://jobs.ashbyhq.com/trading212 | 25 | 14 | 0 | 75 | PL 10, ES 10, DE 4 |
| 505 | Ag | workday | https://ag.wd3.myworkdayjobs.com/airbus | 47 | 13 | 13 | 2548 | PT 45, FR 2 |
| 506 | Clarios | workday | https://clarios.wd5.myworkdayjobs.com/clarioscareers | 37 | 13 | 13 | 215 | DE 36, ES 1 |
| 507 | EPSA | smartrecruiters | https://careers.smartrecruiters.com/EPSA | 101 | 13 | 13 | 102 | FR 101 |
| 508 | Nttlimited (Internal Employees Not On Workday) | workday | https://nttlimited.wd3.myworkdayjobs.com/internal_employees_not_on_workday | 45 | 13 | 13 | 210 | CZ 10, DE 10, FR 6 |
| 509 | Schroders Referral | oracle | https://ekbq.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001 | 16 | 13 | 13 | 97 | FR 8, DE 4, IT 2 |
| 510 | Amcor Group GmbH | successfactors | https://jobs-sf.amcor.com | 102 | 13 | 12 | 178 | DE 35, FR 19, CH 10 |
| 511 | Arthrex, Inc. | successfactors | https://careers.arthrex.com/Germany | 84 | 13 | 12 | 485 | DE 60, FR 18, CH 6 |
| 512 | Ascom | teamtailor | https://ascom.teamtailor.com | 29 | 13 | 12 | 41 | NL 9, DE 7, SE 4 |
| 513 | Caixa Mágica Software | recruiterbox | https://caixamagica.recruiterbox.com | 18 | 13 | 12 | 21 | PT 18 |
| 514 | Kronospan | smartrecruiters | https://careers.smartrecruiters.com/Kronospan | 217 | 13 | 12 | 335 | PL 138, DE 34, AT 16 |
| 515 | Myhr | workday | https://myhr.wd3.myworkdayjobs.com/planseegroup_career | 110 | 13 | 12 | 205 | AT 37, DE 37, LU 21 |
| 516 | ScioTeq BV | recruitee | https://scioteq.recruitee.com | 28 | 13 | 12 | 33 | BE 28 |
| 517 | SOCOTEC | smartrecruiters | https://careers.smartrecruiters.com/Socotec | 551 | 13 | 12 | 728 | FR 485, NL 59, BE 4 |
| 518 | SPECTRUM AG | join_com | https://join.com/companies/spectrum-ag | 19 | 13 | 12 | 19 | DE 19 |
| 519 | AGFA NV | successfactors | https://careers.agfa.com/HealthCare | 35 | 13 | 11 | 85 | BE 22, PL 4, AT 3 |
| 520 | Astek Sweden AB | teamtailor | https://astekswedenab.teamtailor.com | 41 | 13 | 11 | 41 | SE 41 |
| 521 | Coloplast A/S | successfactors | https://careers.coloplast.com | 130 | 13 | 11 | 296 | DE 29, DK 28, FR 24 |
| 522 | HELLA | cornerstone | https://hella.csod.com/ux/ats/careersite/1/home?c=hella | 85 | 13 | 11 | 488 | DE 80, FR 3, AT 1 |
| 523 | Keenfinity | smartrecruiters | https://careers.smartrecruiters.com/keenfinity | 34 | 13 | 11 | 83 | PT 20, NL 8, DE 5 |
| 524 | Newperkinelmer | workday | https://newperkinelmer.wd1.myworkdayjobs.com/external | 27 | 13 | 11 | 252 | PL 8, FR 7, CH 5 |
| 525 | Skydio | ashby | https://jobs.ashbyhq.com/skydio | 13 | 13 | 11 | 131 | CH 11, FI 2 |
| 526 | ASSYSTEM | smartrecruiters | https://careers.smartrecruiters.com/assystem | 412 | 13 | 10 | 931 | FR 411, IE 1 |
| 527 | expleo-jobs-de-de | icims | https://expleo-jobs-de-de.icims.com | 27 | 13 | 10 | 27 | DE 27 |
| 528 | KPN | smartrecruiters | https://careers.smartrecruiters.com/kpn | 46 | 13 | 10 | 46 | NL 46 |
| 529 | Viatris | workday | https://viatris.wd5.myworkdayjobs.com/external | 98 | 13 | 10 | 390 | IE 55, DE 15, FR 10 |
| 530 | WSP Central Europe | teamtailor | https://wspcentraleurope.teamtailor.com | 98 | 13 | 9 | 100 | CH 39, ES 21, FR 18 |
| 531 | Lilly | phenom | https://careers.lilly.com | 65 | 13 | 7 | 658 | DE 21, NL 17, IE 11 |
| 532 | edcengineering | bamboohr | https://edcengineering.bamboohr.com/careers | 20 | 13 | 7 | 23 | IE 20 |
| 533 | Octopus Energy Group | lever | https://jobs.lever.co/octoenergy | 72 | 13 | 7 | 142 | DE 31, IT 17, FR 10 |
| 534 | Ed:Za | teamtailor | https://edzagroup.teamtailor.com | 49 | 13 | 6 | 49 | SE 49 |
| 535 | Ehzq | oracle | https://ehzq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4 | 74 | 13 | 6 | 390 | IE 74 |
| 536 | Envista | workday | https://envista.wd1.myworkdayjobs.com/envistacareers | 57 | 13 | 6 | 266 | CZ 23, BE 8, ES 6 |
| 537 | Guidewire | workday | https://guidewire.wd5.myworkdayjobs.com/external | 18 | 13 | 6 | 138 | IE 7, PL 5, DE 4 |
| 538 | Haufe Group | smartrecruiters | https://careers.smartrecruiters.com/HaufeGroup | 34 | 13 | 6 | 34 | DE 34 |
| 539 | Autodesk | workday | https://autodesk.wd1.myworkdayjobs.com/ext | 33 | 13 | 5 | 418 | PL 8, NO 8, ES 5 |
| 540 | Xecuro GmbH | softgarden | _(unknown — not in companies.csv)_ | 35 | 13 | 5 | 35 | DE 35 |
| 541 | Bankdata | teamtailor | https://bankdata.teamtailor.com | 22 | 13 | 3 | 22 | DK 22 |
| 542 | IFS | smartrecruiters | https://careers.smartrecruiters.com/ifs1 | 57 | 13 | 3 | 262 | NL 12, DE 11, SE 8 |
| 543 | payabl. | workable | https://apply.workable.com/payabl | 15 | 13 | 3 | 26 | PT 8, PL 4, DE 3 |
| 544 | Wavestone Germany AG | softgarden | https://wavestone.career.softgarden.de/ | 91 | 13 | 3 | 93 | DE 81, CH 9, AT 1 |
| 545 | GitLab | greenhouse | https://job-boards.greenhouse.io/gitlab | 36 | 13 | 2 | 227 | DE 15, PL 13, NL 3 |
| 546 | VML MAP | greenhouse | https://job-boards.greenhouse.io/map | 30 | 13 | 1 | 63 | DK 15, ES 15 |
| 547 | CEiiA | smartrecruiters | https://careers.smartrecruiters.com/CEiiA | 34 | 12 | 12 | 34 | PT 34 |
| 548 | EV Group GmbH | softgarden | https://evgroup.career.softgarden.de/ | 47 | 12 | 12 | 47 | AT 47 |
| 549 | Fa Ewwx Saasfaprod1 | oracle | https://fa-ewwx-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 76 | 12 | 12 | 80 | PL 27, DE 22, DK 7 |
| 550 | Intel | workday | https://intel.wd1.myworkdayjobs.com/external | 13 | 12 | 12 | 568 | IE 11, PL 1, DE 1 |
| 551 | Lever Implementation Training Environment | lever | https://jobs.lever.co/leverdemo-8 | 45 | 12 | 12 | 429 | DE 31, ES 7, IE 3 |
| 552 | Max Mara Fashion Group | smartrecruiters | https://careers.smartrecruiters.com/maxmarafashiongroup | 172 | 12 | 12 | 189 | IT 169, FR 2, BE 1 |
| 553 | MSX International | smartrecruiters | https://careers.smartrecruiters.com/msxinternational | 136 | 12 | 12 | 369 | DE 41, IT 28, FR 18 |
| 554 | Oberalp Careers | successfactors | https://jobs.oberalp.com | 85 | 12 | 12 | 88 | IT 54, DE 24, AT 4 |
| 555 | SMA Solar Technology AG | successfactors | https://sma.jobs | 47 | 12 | 12 | 118 | DE 35, PL 10, FR 2 |
| 556 | CPU Consulting & Software GmbH | join_com | https://join.com/companies/cpu-ag | 18 | 12 | 11 | 18 | DE 18 |
| 557 | DP World | oracle | https://ehpv.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 152 | 12 | 11 | 495 | DE 57, BE 23, PL 23 |
| 558 | Fa Ertb Saasfaprod1 | oracle | https://fa-ertb-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2 | 15 | 12 | 11 | 994 | IE 12, FR 3 |
| 559 | AVISIA | recruitee | https://avisia.recruitee.com | 12 | 12 | 10 | 12 | FR 12 |
| 560 | Fa Evax Saasfaprod1 | oracle | https://fa-evax-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001 | 131 | 12 | 10 | 2512 | CH 31, DE 24, ES 22 |
| 561 | Hp (Exteu Ac Careersite) | workday | https://hp.wd5.myworkdayjobs.com/exteu-ac-careersite | 97 | 12 | 10 | 1181 | ES 68, DE 10, IT 5 |
| 562 | Intercon Solutions GmbH | join_com | https://join.com/companies/intercon-solutions1 | 29 | 12 | 10 | 29 | DE 29 |
| 563 | Biibhr | workday | https://biibhr.wd3.myworkdayjobs.com/external | 42 | 12 | 9 | 230 | PL 16, CH 13, ES 7 |
| 564 | Konecranes | smartrecruiters | https://careers.smartrecruiters.com/Konecranes | 113 | 12 | 9 | 406 | DE 41, PL 14, ES 12 |
| 565 | Addepar | greenhouse | https://job-boards.greenhouse.io/addepar1 | 14 | 12 | 8 | 112 | PL 12, CH 2 |
| 566 | Adobe | phenom | https://careers.adobe.com | 36 | 12 | 8 | 645 | CH 12, DE 10, IE 5 |
| 567 | Coface | smartrecruiters | https://careers.smartrecruiters.com/coface | 88 | 12 | 8 | 219 | FR 34, DE 22, PL 8 |
| 568 | Diebold Nixdorf | oracle | https://eeug.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX | 43 | 12 | 8 | 150 | PL 21, DE 11, IT 3 |
| 569 | DOF | workable | https://apply.workable.com/dof | 25 | 12 | 8 | 85 | NO 22, DK 3 |
| 570 | Securitas | teamtailor | https://securitas.teamtailor.com | 29 | 12 | 8 | 36 | SE 13, IE 6, PL 4 |
| 571 | TeamViewer Germany GmbH | teamtailor | https://teamviewer.teamtailor.com | 37 | 12 | 8 | 100 | PT 22, DE 12, AT 2 |
| 572 | WIIT S.p.A. | softgarden | _(unknown — not in companies.csv)_ | 18 | 12 | 8 | 18 | IT 18 |
| 573 | wizinc | greenhouse | https://job-boards.greenhouse.io/wizinc | 16 | 12 | 8 | 127 | DE 4, IE 4, ES 3 |
| 574 | Machine Learning Reply | recruitee | https://machinelearningreply.recruitee.com | 13 | 12 | 7 | 13 | DE 8, AT 5 |
| 575 | Meilleurtaux | smartrecruiters | https://careers.smartrecruiters.com/Meilleurtaux | 36 | 12 | 7 | 36 | FR 36 |
| 576 | Live Reply GmbH | join_com | https://join.com/companies/reply3 | 19 | 12 | 7 | 19 | DE 19 |
| 577 | SAP Fioneer | workable | https://apply.workable.com/fioneer | 51 | 12 | 7 | 105 | DE 51 |
| 578 | SD Solutions | breezy | https://sd-solutions.breezy.hr | 17 | 12 | 7 | 45 | PL 16, ES 1 |
| 579 | Syngenta Group | smartrecruiters | https://careers.smartrecruiters.com/SyngentaGroup | 99 | 12 | 7 | 506 | CH 32, FR 18, NL 16 |
| 580 | TOWA - the digital growth company | join_com | https://join.com/companies/towa | 28 | 12 | 7 | 28 | DE 14, AT 12, PL 2 |
| 581 | globalcareers-pepsico | icims | https://globalcareers-pepsico.icims.com | 32 | 12 | 5 | 825 | PL 17, ES 15 |
| 582 | rtbhouse | greenhouse | https://job-boards.greenhouse.io/rtbhouse | 43 | 12 | 5 | 68 | PL 34, IT 3, DE 3 |
| 583 | 8am | greenhouse | https://job-boards.greenhouse.io/affinipay1 | 16 | 12 | 4 | 30 | CZ 16 |
| 584 | Hiire | teamtailor | https://hiire.teamtailor.com | 26 | 12 | 4 | 27 | PT 25, NL 1 |
| 585 | The Quality Group | greenhouse | https://job-boards.greenhouse.io/thequalitygroupgmbh2 | 36 | 12 | 4 | 89 | DE 33, FR 1, NL 1 |
| 586 | Accuris | dayforce | https://jobs.dayforcehcm.com/accuris/CANDIDATEPORTAL | 15 | 12 | 3 | 28 | PL 15 |
| 587 | Flutterbe | workday | https://flutterbe.wd3.myworkdayjobs.com/Blip_External | 18 | 12 | 3 | 20 | PT 18 |
| 588 | The Quality Group GmbH | greenhouse | https://job-boards.greenhouse.io/thequalitygroupgmbh1 | 36 | 12 | 3 | 101 | DE 32, NL 2, FR 1 |
| 589 | Acuity Inc. | successfactors | https://careers.acuitybrands.com | 15 | 12 | 2 | 205 | IE 11, CH 3, DE 1 |
| 590 | Eeho | oracle | https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001 | 21 | 12 | 2 | 2283 | IE 8, SE 4, NL 3 |
| 591 | staffbase | greenhouse | https://job-boards.greenhouse.io/staffbase | 26 | 12 | 0 | 33 | DE 26 |
| 592 | Swile | lever | https://jobs.lever.co/swile | 15 | 12 | 0 | 27 | FR 15 |
| 593 | Adient (Broadbean External) | workday | https://adient.wd3.myworkdayjobs.com/broadbean_external | 14 | 11 | 11 | 31 | PL 10, ES 3, FR 1 |
| 594 | Bayerische Landesbank AöR | successfactors | https://jobs.bayernlb.de | 28 | 11 | 11 | 28 | DE 26, IT 2 |
| 595 | Bernard Krone Holding SE & Co. KG | successfactors | https://jobs.krone.group | 195 | 11 | 11 | 240 | DE 195 |
| 596 | Board International | jazzhr | https://boardinternationalsa.applytojob.com | 19 | 11 | 11 | 30 | ES 12, CH 4, FR 2 |
| 597 | EverAI | ashby | https://jobs.ashbyhq.com/everai | 27 | 11 | 11 | 53 | IT 5, DE 4, ES 4 |
| 598 | Fa Eocc Saasfaprod1 | oracle | https://fa-eocc-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1007 | 171 | 11 | 11 | 361 | FR 127, DE 16, PL 11 |
| 599 | GRAMMER | cornerstone | https://grammer-performance-management.csod.com/ux/ats/careersite/1/home?c=grammer-performance-management | 42 | 11 | 11 | 115 | DE 33, CZ 6, PL 3 |
| 600 | NTS | recruitee | https://nts.recruitee.com | 48 | 11 | 11 | 63 | NL 48 |
| 601 | Cencora | phenom | https://careers.cencora.com | 221 | 11 | 10 | 938 | NL 74, FR 70, NO 23 |
| 602 | Cato Networks | greenhouse | https://job-boards.greenhouse.io/catonetworks | 22 | 11 | 10 | 100 | CZ 7, BE 3, DE 3 |
| 603 | Disney | workday | https://disney.wd5.myworkdayjobs.com/disneycareer | 52 | 11 | 10 | 626 | FR 35, DE 4, PT 2 |
| 604 | Esri | greenhouse | https://job-boards.greenhouse.io/esri | 13 | 11 | 10 | 450 | DE 6, NL 3, FR 2 |
| 605 | GECKO | breezy | https://gecko.breezy.hr | 26 | 11 | 10 | 26 | DE 26 |
| 606 | Groupe Roullier | cornerstone | https://roullier.csod.com/ux/ats/careersite/1/home?c=roullier | 144 | 11 | 10 | 220 | FR 132, IE 5, DE 3 |
| 607 | Hesperia World | teamtailor | https://hesperiaworld.teamtailor.com | 78 | 11 | 10 | 79 | ES 78 |
| 608 | Intuitive | smartrecruiters | https://careers.smartrecruiters.com/intuitive | 96 | 11 | 10 | 684 | DE 43, FR 13, CH 8 |
| 609 | SOGECLAIR | workable | https://apply.workable.com/sogeclair | 59 | 11 | 10 | 136 | FR 44, ES 12, DE 2 |
| 610 | CEVA | cornerstone | https://ceva.csod.com/ux/ats/careersite/1/home?c=ceva | 46 | 11 | 9 | 111 | FR 33, DE 6, IT 2 |
| 611 | Embark Studios | teamtailor | https://embarkstudios.teamtailor.com | 18 | 11 | 9 | 18 | SE 18 |
| 612 | Frenckengroup | workday | https://frenckengroup.wd103.myworkdayjobs.com/External | 27 | 11 | 9 | 110 | NL 27 |
| 613 | Planet | greenhouse | https://job-boards.greenhouse.io/planetlabs | 32 | 11 | 9 | 126 | DE 20, NL 6, FR 2 |
| 614 | Transdev | cornerstone | https://transdev.csod.com/ux/ats/careersite/1/home?c=transdev | 452 | 11 | 9 | 457 | FR 448, DE 3, IE 1 |
| 615 | Anthropic | greenhouse | https://job-boards.greenhouse.io/anthropic | 29 | 11 | 8 | 595 | IE 9, DE 8, FR 7 |
| 616 | Axians Infoma GmbH | softgarden | _(unknown — not in companies.csv)_ | 30 | 11 | 8 | 30 | DE 28, AT 2 |
| 617 | ESA | successfactors | https://jobs.esa.int | 20 | 11 | 8 | 20 | NL 10, DE 3, ES 2 |
| 618 | Fa Eugp Saasfaprod1 | oracle | https://fa-eugp-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/career-synlab | 209 | 11 | 7 | 210 | DE 178, IT 23, ES 6 |
| 619 | Genpt | workday | https://genpt.wd1.myworkdayjobs.com/careers | 123 | 11 | 7 | 2585 | DE 100, PL 14, IE 8 |
| 620 | Fifty-Five | workable | https://apply.workable.com/fifty-five | 21 | 11 | 6 | 29 | FR 18, CH 3 |
| 621 | Coopers Group AG | join_com | https://join.com/companies/iet | 34 | 11 | 6 | 34 | CH 34 |
| 622 | in2 | bamboohr | https://in2.bamboohr.com/careers | 17 | 11 | 6 | 17 | IE 10, DE 7 |
| 623 | Post Luxembourg | successfactors | https://careers.postgroup.lu | 32 | 11 | 6 | 32 | LU 32 |
| 624 | 10x Team | ashby | https://jobs.ashbyhq.com/10xteam | 101 | 11 | 5 | 133 | ES 19, FR 16, NL 14 |
| 625 | Delta Electronics | smartrecruiters | https://careers.smartrecruiters.com/DeltaElectronics | 48 | 11 | 5 | 55 | NL 22, DE 17, AT 5 |
| 626 | OMNIVISION | jobvite | https://jobs.jobvite.com/ovt | 14 | 11 | 5 | 107 | NO 9, BE 4, DE 1 |
| 627 | Welcome to the Jungle | greenhouse | https://job-boards.greenhouse.io/artefactjobs | 18 | 11 | 4 | 30 | FR 18 |
| 628 | MAPFRE | successfactors | https://jobs.mapfre.com | 89 | 11 | 4 | 169 | ES 89 |
| 629 | McDermott External Career Site | oracle | https://edsv.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 46 | 11 | 4 | 477 | SE 21, NL 15, IT 9 |
| 630 | Yellowtail Conclusion | recruitee | https://yellowtail.recruitee.com | 36 | 11 | 4 | 36 | NL 36 |
| 631 | Bentley Systems | successfactors | https://jobs.bentley.com | 23 | 11 | 3 | 98 | IE 15, NL 2, ES 2 |
| 632 | zetaglobal | greenhouse | https://job-boards.greenhouse.io/zetaglobal | 12 | 11 | 3 | 142 | DE 5, CZ 4, DK 3 |
| 633 | ompexternaljobboards | greenhouse | https://job-boards.greenhouse.io/ompexternaljobboards | 19 | 11 | 2 | 37 | BE 18, NL 1 |
| 634 | parser | bamboohr | https://parser.bamboohr.com/careers | 12 | 11 | 2 | 30 | ES 12 |
| 635 | ADB SAFEGATE Careers | successfactors | https://careers.adbsafegate.com | 24 | 10 | 10 | 63 | AT 9, DE 6, BE 6 |
| 636 | De Brauw Blackstone Westbroek | smartrecruiters | https://careers.smartrecruiters.com/DeBrauwBlackstoneWestbroek1 | 71 | 10 | 10 | 71 | NL 71 |
| 637 | E-FARM | join_com | https://join.com/companies/e-farm | 11 | 10 | 10 | 11 | DE 11 |
| 638 | Fa Espx Saasfaprod1 | oracle | https://fa-espx-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 30 | 10 | 10 | 1044 | DE 5, IT 5, FR 4 |
| 639 | IFF | workday | https://iff.wd5.myworkdayjobs.com/iff_careers | 58 | 10 | 10 | 383 | NL 14, ES 12, DK 10 |
| 640 | JACOBS DOUWE EGBERTS | smartrecruiters | https://careers.smartrecruiters.com/JACOBSDOUWEEGBERTS | 53 | 10 | 10 | 130 | PL 17, DE 13, NL 11 |
| 641 | Joveo Sandbox | smartrecruiters | https://careers.smartrecruiters.com/joveosandbox | 34 | 10 | 10 | 63 | DE 9, FR 9, FI 6 |
| 642 | LOTUS BAKERIES CORPORATE NV | successfactors | https://jobslotusbakeries.com | 79 | 10 | 10 | 123 | BE 57, NL 10, FR 7 |
| 643 | Moog | workday | https://moog.wd5.myworkdayjobs.com/moog_external_career_site | 43 | 10 | 10 | 456 | DE 17, IE 13, NL 7 |
| 644 | Pragmatic Coders | recruitee | https://pragmaticcoders.recruitee.com | 11 | 10 | 10 | 12 | PL 11 |
| 645 | Tenneco Automotive | successfactors | https://jobs.tenneco.com | 38 | 10 | 10 | 222 | DE 25, PL 5, FR 4 |
| 646 | Valentino | successfactors | https://jobs.valentino.com | 28 | 10 | 10 | 58 | IT 22, AT 2, FR 1 |
| 647 | Volga Partners | workable | https://apply.workable.com/volga-partners | 16 | 10 | 10 | 69 | FR 3, ES 3, DE 3 |
| 648 | Werken bij Profource | oracle | https://eccs.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/werkenbij | 29 | 10 | 10 | 29 | NL 29 |
| 649 | AGILITA AG | softgarden | https://agilitaschweiz.career.softgarden.de/ | 29 | 10 | 9 | 29 | CH 26, DE 3 |
| 650 | BRÜGGEN ENGINEERING GmbH | join_com | https://join.com/companies/brueggen-engineering | 76 | 10 | 9 | 76 | DE 76 |
| 651 | HashtagTalent | smartrecruiters | https://careers.smartrecruiters.com/hashtagtalent | 12 | 10 | 9 | 29 | CZ 9, DE 3 |
| 652 | Innogy SE | successfactors | https://karriere.suewag.com | 34 | 10 | 9 | 34 | DE 34 |
| 653 | Maurer Electronics GmbH | softgarden | _(unknown — not in companies.csv)_ | 13 | 10 | 9 | 13 | DE 13 |
| 654 | VusionGroup SA | smartrecruiters | https://careers.smartrecruiters.com/vusiongroupsa | 38 | 10 | 9 | 53 | FR 19, DE 7, AT 5 |
| 655 | Zoku | recruitee | https://livezoku.recruitee.com | 23 | 10 | 9 | 23 | NL 12, DK 5, AT 5 |
| 656 | Actemium Controlmatic West GmbH | softgarden | _(unknown — not in companies.csv)_ | 62 | 10 | 8 | 62 | DE 62 |
| 657 | Alithya | oracle | https://careers.alithya.com/hcmUI/CandidateExperience/en/sites/AlithyaCareersCarrieres | 16 | 10 | 8 | 186 | FR 16 |
| 658 | BNY External Career Site | oracle | https://eofe.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 121 | 10 | 8 | 1382 | IE 48, PL 41, DE 13 |
| 659 | Boskalis | smartrecruiters | https://careers.smartrecruiters.com/boskalis | 86 | 10 | 8 | 106 | NL 81, DE 5 |
| 660 | Dexcom | workday | https://dexcom.wd1.myworkdayjobs.com/dexcom | 54 | 10 | 8 | 301 | IE 35, DE 9, ES 8 |
| 661 | Fortive Careers | oracle | https://ejta.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 32 | 10 | 8 | 298 | NL 15, PL 10, IT 2 |
| 662 | IQM Quantum Computers | teamtailor | https://iqm.teamtailor.com | 12 | 10 | 8 | 19 | FI 10, DE 2 |
| 663 | Mapbox | ashby | https://jobs.ashbyhq.com/mapbox | 10 | 10 | 8 | 52 | DE 8, PL 2 |
| 664 | Universiteitleiden | successfactors | https://careers.universiteitleiden.nl | 64 | 10 | 8 | 64 | NL 64 |
| 665 | Centric | recruitee | https://centric.recruitee.com | 64 | 10 | 7 | 65 | NL 64 |
| 666 | Cohesity | workday | https://cohesity.wd5.myworkdayjobs.com/cohesity_careers | 30 | 10 | 7 | 163 | DE 6, FR 5, NL 4 |
| 667 | Coop | successfactors | https://jobs.coop.ch/Transgourmet_Prodega | 3152 | 10 | 7 | 3162 | CH 3152 |
| 668 | Copeland | workday | https://copeland.wd5.myworkdayjobs.com/copeland_external_careers_page | 23 | 10 | 7 | 400 | DE 14, IT 8, ES 1 |
| 669 | cyberunity AG | join_com | https://join.com/companies/cyberunity | 16 | 10 | 7 | 16 | CH 16 |
| 670 | hellomateo | join_com | https://join.com/companies/mateoestate | 11 | 10 | 7 | 11 | DE 11 |
| 671 | OpenAI | ashby | https://jobs.ashbyhq.com/openai | 30 | 10 | 7 | 781 | IE 15, DE 6, FR 4 |
| 672 | Sword Technologies N.V./S.A. | recruitee | https://swordtechnologies.recruitee.com | 15 | 10 | 7 | 15 | LU 14, BE 1 |
| 673 | Teamwork Corporate | smartrecruiters | https://careers.smartrecruiters.com/TeamworkCorporate | 67 | 10 | 7 | 79 | FR 63, CH 2, LU 1 |
| 674 | Westernacher Solutions GmbH | jazzhr | https://westernachersolutions.applytojob.com | 21 | 10 | 7 | 22 | DE 20, AT 1 |
| 675 | eigenblue | breezy | https://eigenblue.breezy.hr | 18 | 10 | 6 | 22 | DE 18 |
| 676 | Expedia | workday | https://expedia.wd108.myworkdayjobs.com/search | 27 | 10 | 6 | 163 | ES 11, CZ 11, IT 3 |
| 677 | Hyundai Motorsport GmbH | softgarden | https://motorsporthyundai.career.softgarden.de/ | 18 | 10 | 6 | 18 | DE 14, FR 4 |
| 678 | IQ Plus AG | join_com | https://join.com/companies/iqplus | 16 | 10 | 6 | 16 | CH 16 |
| 679 | Lifted, an Upwork Company™ | smartrecruiters | https://careers.smartrecruiters.com/liftedanupworkcompany | 37 | 10 | 6 | 319 | ES 11, IT 7, FR 7 |
| 680 | Wavestone Germany AG | join_com | https://join.com/companies/q-perior | 18 | 10 | 6 | 18 | CH 16, DE 2 |
| 681 | WPP | greenhouse | https://job-boards.greenhouse.io/wpp | 17 | 10 | 6 | 218 | DK 6, ES 3, PL 3 |
| 682 | 1KOMMA5˚ | join_com | https://join.com/companies/1komma5grad | 100 | 10 | 5 | 100 | DE 100 |
| 683 | Dashlane | greenhouse | https://job-boards.greenhouse.io/dashlane | 13 | 10 | 5 | 15 | FR 8, PT 5 |
| 684 | Fa Euxc Saasfaprod1 | oracle | https://fa-euxc-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 80 | 10 | 5 | 470 | IE 60, LU 14, NL 3 |
| 685 | Faktion BV | recruitee | https://faktionbv1.recruitee.com | 14 | 10 | 5 | 14 | BE 14 |
| 686 | FREENOW | greenhouse | https://job-boards.greenhouse.io/freenow | 46 | 10 | 5 | 52 | DE 28, ES 13, IE 5 |
| 687 | TechBiz Global GmbH | recruitee | https://techbizglobal.recruitee.com | 28 | 10 | 5 | 197 | DE 15, NL 3, ES 3 |
| 688 | BAS Group | recruitee | https://basgroup.recruitee.com | 80 | 10 | 4 | 82 | NL 80 |
| 689 | DoiT | greenhouse | https://job-boards.greenhouse.io/doitintl | 28 | 10 | 4 | 84 | IE 13, SE 7, NL 6 |
| 690 | noris network AG | join_com | https://join.com/companies/noris | 18 | 10 | 4 | 18 | DE 18 |
| 691 | WIIT AG | softgarden | https://wiit.career.softgarden.de/ | 28 | 10 | 4 | 28 | DE 28 |
| 692 | Aviva Investors (External) | workday | https://aviva.wd1.myworkdayjobs.com/External | 22 | 10 | 3 | 150 | PL 20, LU 2 |
| 693 | NNE | cornerstone | https://nne.csod.com/ux/ats/careersite/1/home?c=nne | 10 | 10 | 3 | 23 | DK 10 |
| 694 | Dowjones | workday | https://dowjones.wd1.myworkdayjobs.com/dow_jones_career | 19 | 10 | 2 | 173 | ES 17, DK 1, NL 1 |
| 695 | Fa Ewmy Saasfaprod1 | oracle | https://fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 27 | 10 | 2 | 154 | DE 14, PL 10, ES 2 |
| 696 | Netwrix Corporation | rippling | https://ats.rippling.com/netwrix-corporation/jobs | 18 | 10 | 2 | 25 | PL 18 |
| 697 | SBM Offshore Group | successfactors | https://careers.sbmoffshore.com | 25 | 10 | 2 | 285 | NL 25 |
| 698 | SOFTSWISS | teamtailor | https://softswiss.teamtailor.com | 38 | 10 | 2 | 53 | PL 38 |
| 699 | Accso – Accelerated Solutions GmbH | recruitee | https://accso.recruitee.com | 15 | 10 | 1 | 18 | DE 15 |
| 700 | Alexander Thamm GmbH | join_com | https://join.com/companies/alexanderthamm | 13 | 10 | 1 | 13 | DE 12, CH 1 |
| 701 | AlphaSense | greenhouse | https://job-boards.greenhouse.io/alphasense | 16 | 10 | 0 | 222 | FI 12, IE 4 |
| 702 | Alantra | workday | https://alantra.wd3.myworkdayjobs.com/alantra | 27 | 9 | 9 | 46 | ES 17, DE 6, SE 1 |
| 703 | Artemed SE | smartrecruiters | https://careers.smartrecruiters.com/ArtemedSE | 441 | 9 | 9 | 441 | DE 441 |
| 704 | Barrywehmiller | workday | https://barrywehmiller.wd1.myworkdayjobs.com/bwcareers | 30 | 9 | 9 | 392 | DE 21, IT 3, NL 3 |
| 705 | BENSAUDE, S.A. | successfactors | https://carreiras.grupobensaude.pt | 58 | 9 | 9 | 58 | PT 58 |
| 706 | BEW Berliner Energie und Wärme GmbH | smartrecruiters | https://careers.smartrecruiters.com/bew | 41 | 9 | 9 | 41 | DE 41 |
| 707 | Cuatrecasas | cornerstone | https://cuatrecasas.csod.com/ux/ats/careersite/1/home?c=cuatrecasas | 32 | 9 | 9 | 41 | ES 28, PT 4 |
| 708 | CVX Ventures | greenhouse | https://job-boards.greenhouse.io/cvx | 40 | 9 | 9 | 43 | DK 25, SE 6, FI 3 |
| 709 | Fast Retailing Group (graduates eu Uniqlo) | workday | https://fastretailing.wd3.myworkdayjobs.com/graduates_eu_Uniqlo | 9 | 9 | 9 | 11 | FR 1, DE 1, BE 1 |
| 710 | Gerresheimer | smartrecruiters | https://careers.smartrecruiters.com/gerresheimer | 105 | 9 | 9 | 156 | DE 74, PL 22, IT 7 |
| 711 | Groz-Beckert KG | successfactors | https://jobs.groz-beckert.com | 41 | 9 | 9 | 43 | DE 35, CZ 5, PT 1 |
| 712 | HumanSignal | greenhouse | https://job-boards.greenhouse.io/humansignal | 9 | 9 | 9 | 49 | FI 1, IT 1, NL 1 |
| 713 | Myhrabc | workday | https://myhrabc.wd5.myworkdayjobs.com/global | 151 | 9 | 9 | 939 | NL 68, NO 23, ES 21 |
| 714 | ONLY Stores Germany | softgarden | https://onlystores.career.softgarden.de/ | 369 | 9 | 9 | 369 | DE 361, CZ 5, AT 2 |
| 715 | Palantir Technologies | lever | https://jobs.lever.co/palantir | 13 | 9 | 9 | 309 | FR 3, NL 2, NO 2 |
| 716 | Proximus | cornerstone | https://proximus.csod.com/ux/ats/careersite/10/home?c=proximus | 54 | 9 | 9 | 120 | BE 51, ES 2, DE 1 |
| 717 | Sacmi | cornerstone | https://sacmi.csod.com/ux/ats/careersite/4/home?c=sacmi | 55 | 9 | 9 | 55 | IT 55 |
| 718 | Tenaris | successfactors | https://recruitment.tenaris.com | 24 | 9 | 9 | 215 | IT 24 |
| 719 | Applied | workday | https://flowserve.wd1.myworkdayjobs.com/applied | 51 | 9 | 8 | 418 | NL 19, ES 11, FR 7 |
| 720 | Big Dutchman AG | successfactors | https://jobs.bigdutchman.com | 41 | 9 | 8 | 46 | DE 39, ES 2 |
| 721 | Ejhp | oracle | https://ejhp.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2 | 89 | 9 | 8 | 2198 | DE 19, FR 15, PL 15 |
| 722 | expleo-jobs-ch-en | icims | https://expleo-jobs-ch-en.icims.com | 30 | 9 | 8 | 30 | CH 30 |
| 723 | SD Worx Group | teamtailor | https://sdworxgroup.teamtailor.com | 84 | 9 | 8 | 100 | ES 21, DE 14, NL 12 |
| 724 | SupportYourApp | workable | https://apply.workable.com/supportyourapp | 18 | 9 | 8 | 57 | ES 7, PL 4, PT 2 |
| 725 | TTi Group | workday | https://ttiemea.wd3.myworkdayjobs.com/TTI | 69 | 9 | 8 | 112 | DE 22, PL 16, NL 8 |
| 726 | Vanderlande | workday | https://vanderlande.wd3.myworkdayjobs.com/careers | 15 | 9 | 8 | 180 | NL 15 |
| 727 | Action Service Distributie BV | successfactors | https://apply.action.com | 3890 | 9 | 7 | 3927 | NL 1645, DE 822, FR 508 |
| 728 | Bloq.it | teamtailor | https://bloqit.teamtailor.com | 27 | 9 | 7 | 28 | PT 21, DE 3, IT 1 |
| 729 | ComparaJá | greenhouse | https://job-boards.greenhouse.io/comparaja | 54 | 9 | 7 | 81 | PT 54 |
| 730 | Hp | workday | https://hp.wd5.myworkdayjobs.com/externalcareersite | 69 | 9 | 7 | 848 | ES 48, DE 6, IT 4 |
| 731 | Huntsman Corporation | workday | https://huntsman.wd1.myworkdayjobs.com/huntsman | 30 | 9 | 7 | 167 | PL 16, CH 5, DE 4 |
| 732 | Koninklijk Nederlands Lucht- en Ruimtevaartcentrum | recruitee | https://werkenbijnlr.recruitee.com | 32 | 9 | 7 | 32 | NL 32 |
| 733 | Leyton | teamtailor | https://leytonglobal-germany.teamtailor.com | 29 | 9 | 7 | 29 | DE 29 |
| 734 | Mksinst (MKSCareersEMEA) | workday | https://mksinst.wd1.myworkdayjobs.com/MKSCareersEMEA | 50 | 9 | 7 | 116 | DE 21, FR 14, PL 9 |
| 735 | PESTANA MANAGEMENT - SERVIÇOS | successfactors | https://careers.pestanagroup.com | 143 | 9 | 7 | 146 | PT 133, ES 9, NL 1 |
| 736 | Proalpha Group | greenhouse | https://job-boards.greenhouse.io/proalphagroup | 34 | 9 | 7 | 37 | DE 29, CH 3, PL 1 |
| 737 | Sandvik (Sandvik Jobs) | workday | https://sandvik.wd3.myworkdayjobs.com/sandvik-jobs | 60 | 9 | 7 | 406 | SE 17, DE 11, FI 8 |
| 738 | Xylem | join_com | https://join.com/companies/xylem | 22 | 9 | 7 | 22 | DE 18, CH 4 |
| 739 | Barco NV | successfactors | https://jobs.barco.com | 15 | 9 | 6 | 51 | BE 15 |
| 740 | Boeing | workday | https://boeing.wd1.myworkdayjobs.com/external_careers | 15 | 9 | 6 | 746 | DE 11, ES 2, PL 1 |
| 741 | CBRE Global Workplace Solutions / Data Center Solutions | join_com | https://join.com/companies/cbre | 16 | 9 | 6 | 16 | DE 12, SE 2, NO 2 |
| 742 | Focused | ashby | https://jobs.ashbyhq.com/focused | 25 | 9 | 6 | 30 | DE 25 |
| 743 | Ictech | teamtailor | https://ictech.teamtailor.com | 16 | 9 | 6 | 16 | SE 16 |
| 744 | Kreiosspace | personio | https://kreiosspace.jobs.personio.com | 16 | 9 | 6 | 16 | ES 16 |
| 745 | Leaf Space | teamtailor | https://leafspace.teamtailor.com | 10 | 9 | 6 | 11 | IT 10 |
| 746 | Paradox Interactive | teamtailor | https://paradox-interactive.teamtailor.com | 18 | 9 | 6 | 18 | SE 11, FI 5, ES 2 |
| 747 | Veneficus B.V. | recruitee | https://veneficusbv.recruitee.com | 9 | 9 | 6 | 9 | NL 9 |
| 748 | Ziff Davis | jobvite | https://jobs.jobvite.com/ziffdavis | 17 | 9 | 6 | 77 | ES 12, IE 4, FI 1 |
| 749 | Zinkworks | teamtailor | https://zinkworks.teamtailor.com | 15 | 9 | 6 | 15 | IE 15 |
| 750 | Accenture | join_com | https://join.com/companies/accenture | 28 | 9 | 5 | 28 | DE 27, AT 1 |
| 751 | Chain IQ Group AG | successfactors | https://careers.chainiq.com | 18 | 9 | 5 | 69 | PT 14, CH 4 |
| 752 | Coduct Solutions GmbH | join_com | https://join.com/companies/coduct | 9 | 9 | 5 | 9 | DE 9 |
| 753 | Deezer | teamtailor | https://deezer.teamtailor.com | 16 | 9 | 5 | 16 | FR 16 |
| 754 | Devexperts | smartrecruiters | https://careers.smartrecruiters.com/Devexperts | 9 | 9 | 5 | 33 | PT 9 |
| 755 | ICON | workday | https://icon.wd3.myworkdayjobs.com/broadbean_external | 131 | 9 | 5 | 894 | IE 33, PL 26, FR 22 |
| 756 | MEDIAPLUS Gruppe | softgarden | _(unknown — not in companies.csv)_ | 21 | 9 | 5 | 21 | DE 21 |
| 757 | Fa Ewjt Saasfaprod1 | oracle | https://fa-ewjt-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2 | 16 | 9 | 4 | 3309 | IE 16 |
| 758 | MBRYONICS | rippling | https://ats.rippling.com/mbryonics/jobs | 13 | 9 | 4 | 13 | IE 13 |
| 759 | Awin | greenhouse | https://job-boards.greenhouse.io/awin | 29 | 9 | 3 | 54 | DE 16, PL 5, NL 3 |
| 760 | CD PROJEKT RED | smartrecruiters | https://careers.smartrecruiters.com/cdprojektred | 34 | 9 | 3 | 43 | PL 34 |
| 761 | E. Breuninger GmbH & Co. | smartrecruiters | https://careers.smartrecruiters.com/EBreuningerGmbHCo | 155 | 9 | 3 | 155 | DE 152, LU 3 |
| 762 | Henryschein | workday | https://henryschein.wd1.myworkdayjobs.com/external_careers | 99 | 9 | 3 | 257 | DE 43, ES 20, FR 15 |
| 763 | CVS Health | phenom | https://jobs.cvshealth.com | 15 | 9 | 3 | 9900 | IE 13, NL 2 |
| 764 | Kong | ashby | https://jobs.ashbyhq.com/kong | 15 | 9 | 3 | 71 | IT 9, FR 4, NL 1 |
| 765 | Zscaler | greenhouse | https://job-boards.greenhouse.io/zscaler | 29 | 9 | 3 | 360 | DE 8, SE 5, NL 5 |
| 766 | CSC | oracle | https://hczw.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 39 | 9 | 2 | 178 | LU 13, NL 7, IE 6 |
| 767 | Bristol Myers Squibb | eightfold | https://bms.eightfold.ai/careers | 84 | 9 | 1 | 597 | PL 37, NL 28, IE 8 |
| 768 | Swift | workday | https://swift.wd3.myworkdayjobs.com/join-swift | 15 | 9 | 1 | 102 | BE 7, NL 7, IT 1 |
| 769 | HubSpot | greenhouse | https://job-boards.greenhouse.io/hubspotjobs | 62 | 9 | 0 | 147 | IE 40, DE 12, FR 9 |
| 770 | Aikido Security | recruitee | https://aikidosecurity.recruitee.com | 28 | 8 | 8 | 73 | BE 28 |
| 771 | Alloheim Senioren-Residenzen SE | softgarden | https://alloheim.career.softgarden.de/ | 128 | 8 | 8 | 128 | DE 128 |
| 772 | APM Terminals | workday | https://maersk.wd3.myworkdayjobs.com/apmt_careers | 40 | 8 | 8 | 134 | NL 11, DE 10, SE 8 |
| 773 | Ayming | smartrecruiters | https://careers.smartrecruiters.com/Ayming | 91 | 8 | 8 | 105 | FR 42, PT 20, ES 7 |
| 774 | BE Terna | cornerstone | https://be-terna.csod.com/ux/ats/careersite/5/home?c=be-terna | 32 | 8 | 8 | 42 | AT 15, DE 13, NO 2 |
| 775 | Campus | workday | https://hl.wd1.myworkdayjobs.com/campus | 8 | 8 | 8 | 27 | DE 4, NL 2, IT 1 |
| 776 | Samlino Group | greenhouse | https://job-boards.greenhouse.io/ceg | 34 | 8 | 8 | 34 | PT 34 |
| 777 | Cold Culture | teamtailor | https://coldculture.teamtailor.com | 30 | 8 | 8 | 32 | ES 23, NL 4, IT 2 |
| 778 | EF / Hult | oracle | https://fa-evad-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/ef | 54 | 8 | 8 | 175 | FR 11, DE 9, IT 8 |
| 779 | Efet | oracle | https://efet.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 118 | 8 | 8 | 4438 | DE 33, PT 25, IT 16 |
| 780 | European Medicines Agency | successfactors | https://careers.ema.europa.eu | 10 | 8 | 8 | 10 | NL 10 |
| 781 | Flexion Robotics | workable | https://apply.workable.com/flexion-robotics | 11 | 8 | 8 | 12 | CH 11 |
| 782 | Flir | workday | https://flir.wd1.myworkdayjobs.com/flircareers | 44 | 8 | 8 | 705 | FR 23, SE 7, ES 5 |
| 783 | GreenPocket GmbH | join_com | https://join.com/companies/greenpocket | 13 | 8 | 8 | 13 | DE 13 |
| 784 | HAVER & BOECKER | cornerstone | https://haverboecker.csod.com/ux/ats/careersite/1/home?c=haverboecker | 24 | 8 | 8 | 24 | DE 24 |
| 785 | Hdeh | oracle | https://hdeh.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 52 | 8 | 8 | 52 | IT 52 |
| 786 | Iabdgs | oracle | https://iabdgs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Finland | 8 | 8 | 8 | 8 | FI 8 |
| 787 | KULT \| OLYMP & HADES | recruitee | _(unknown — not in companies.csv)_ | 75 | 8 | 8 | 75 | DE 62, AT 12, LU 1 |
| 788 | Lear Corporation | successfactors | https://jobs.lear.com | 13 | 8 | 8 | 319 | CZ 13 |
| 789 | mainova-karriere | successfactors | https://mainova-karriere.de | 72 | 8 | 8 | 72 | DE 72 |
| 790 | Job Opportunities | successfactors | https://careers.mondigroup.com | 184 | 8 | 8 | 226 | DE 106, PL 31, AT 29 |
| 791 | OTTO FUCHS KG | smartrecruiters | https://careers.smartrecruiters.com/OTTOFUCHSKG | 63 | 8 | 8 | 63 | DE 63 |
| 792 | Our jobs - CANAL+ Group | successfactors | https://joinus.canalplus.com | 43 | 8 | 8 | 60 | FR 32, PL 10, LU 1 |
| 793 | Pierre Fabre Laboratories | workday | https://pierrefabre.wd3.myworkdayjobs.com/external_career_site | 36 | 8 | 8 | 249 | BE 26, DE 4, ES 3 |
| 794 | TELUS Digital | join_com | https://join.com/companies/telusinternational | 15 | 8 | 8 | 27 | CH 3, AT 3, DE 2 |
| 795 | The Great-West Life Assurance Company | successfactors | https://life-careers.com/canadalifedeutschland | 18 | 8 | 8 | 22 | IE 11, DE 7 |
| 796 | WBS TRAINING Trainer:in Honorar | softgarden | _(unknown — not in companies.csv)_ | 33 | 8 | 8 | 33 | DE 33 |
| 797 | American Tower Global | oracle | https://hdsn.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 13 | 8 | 7 | 63 | DE 6, FR 4, NL 2 |
| 798 | AniCura Group | teamtailor | https://anicuraglobal.teamtailor.com | 98 | 8 | 7 | 100 | DE 18, FR 17, SE 14 |
| 799 | Avery Dennison | smartrecruiters | https://careers.smartrecruiters.com/averydennison | 69 | 8 | 7 | 443 | NL 18, DE 11, IE 9 |
| 800 | dexter health | join_com | https://join.com/companies/dexter-health | 8 | 8 | 7 | 8 | DE 8 |
| 801 | Diamond Foundry | lever | https://jobs.lever.co/diamondfoundry | 17 | 8 | 7 | 44 | ES 16, DE 1 |
| 802 | DKMS gemeinnützige GmbH | successfactors | https://jobs.dkms.de | 31 | 8 | 7 | 31 | DE 31 |
| 803 | EDP Energias de Portugal S.A. | successfactors | https://jobs.edp.com | 29 | 8 | 7 | 75 | PT 18, ES 7, IT 3 |
| 804 | Fugro | workday | https://fugro.wd3.myworkdayjobs.com/careers | 30 | 8 | 7 | 137 | NL 21, DE 7, BE 2 |
| 805 | Rolex SA | successfactors | https://www.carrieres-rolex.com | 209 | 8 | 7 | 209 | CH 209 |
| 806 | Teleperformance Spain | workable | https://apply.workable.com/teleperformance-spain | 123 | 8 | 7 | 123 | ES 123 |
| 807 | Wayve | greenhouse | https://job-boards.greenhouse.io/wayve | 14 | 8 | 7 | 167 | DE 14 |
| 808 | Actana Consulting Services GmbH | join_com | https://join.com/companies/actana-consulting | 13 | 8 | 6 | 13 | DE 13 |
| 809 | Akzo Nobel Sourcing B.V. | successfactors | https://careers.akzonobel.com | 77 | 8 | 6 | 263 | FR 23, DE 14, NL 12 |
| 810 | ATLAS | smartrecruiters | https://careers.smartrecruiters.com/ATLAS4 | 26 | 8 | 6 | 26 | DE 26 |
| 811 | Avantor | phenom | https://careers.avantorsciences.com | 79 | 8 | 6 | 251 | NL 25, DE 15, BE 13 |
| 812 | Data Intellect | smartrecruiters | https://careers.smartrecruiters.com/dataintellect | 13 | 8 | 6 | 33 | IE 13 |
| 813 | DKB Das kann Bank | successfactors | https://jobs.dkb.de | 18 | 8 | 6 | 119 | DE 18 |
| 814 | DNB | successfactors | https://jobb.dnb.no | 12 | 8 | 6 | 12 | NO 12 |
| 815 | gambit | personio | https://gambit.jobs.personio.com | 68 | 8 | 6 | 71 | DE 44, AT 15, CH 9 |
| 816 | Holzland Becker | recruitee | https://holzlandbecker.recruitee.com | 26 | 8 | 6 | 28 | DE 26 |
| 817 | Medialine EuroTrade AG | join_com | https://join.com/companies/medialine | 47 | 8 | 6 | 47 | DE 46, AT 1 |
| 818 | PA Consulting | smartrecruiters | https://careers.smartrecruiters.com/paconsulting | 46 | 8 | 6 | 189 | SE 13, DK 12, IE 10 |
| 819 | Pinely | workable | https://apply.workable.com/pinely | 17 | 8 | 6 | 17 | NL 17 |
| 820 | Sbdinc | workday | https://sbdinc.wd1.myworkdayjobs.com/stanley_black_decker_career_site | 118 | 8 | 6 | 621 | DE 63, PL 14, FR 13 |
| 821 | Schibsted | teamtailor | https://aftonbladet.teamtailor.com | 32 | 8 | 6 | 32 | SE 18, NO 14 |
| 822 | TechSeed | teamtailor | https://techseed.teamtailor.com | 24 | 8 | 6 | 24 | SE 24 |
| 823 | ACT-ON | smartrecruiters | https://careers.smartrecruiters.com/ACT-ON | 36 | 8 | 5 | 36 | FR 29, BE 3, DE 3 |
| 824 | AEG Power Solutions | softgarden | https://aegps.career.softgarden.de/ | 24 | 8 | 5 | 35 | DE 17, ES 6, NL 1 |
| 825 | Avnet | workday | https://avnet.wd1.myworkdayjobs.com/external | 52 | 8 | 5 | 275 | DE 35, FR 4, IT 3 |
| 826 | Brainlab | smartrecruiters | https://careers.smartrecruiters.com/brainlab | 26 | 8 | 5 | 43 | DE 22, ES 2, CH 1 |
| 827 | Cognex | workday | https://cognex.wd1.myworkdayjobs.com/external_career_site | 12 | 8 | 5 | 86 | DE 6, IE 4, IT 1 |
| 828 | Creditplus Bank AG | softgarden | https://creditplus.career.softgarden.de/ | 57 | 8 | 5 | 57 | DE 57 |
| 829 | G MASS | workable | https://apply.workable.com/g-mass | 16 | 8 | 5 | 101 | IE 11, NL 2, ES 1 |
| 830 | hubside - Die Recruitingwerkstatt | join_com | https://join.com/companies/hubside | 40 | 8 | 5 | 40 | DE 40 |
| 831 | INFUSE | greenhouse | https://job-boards.greenhouse.io/infuse | 41 | 8 | 5 | 415 | PL 19, PT 12, IT 8 |
| 832 | In The Pocket | greenhouse | https://job-boards.greenhouse.io/inthepocket | 17 | 8 | 5 | 19 | BE 17 |
| 833 | Organon | phenom | https://jobs.organon.com | 47 | 8 | 5 | 144 | NL 23, BE 9, PT 8 |
| 834 | Viaplay Group | teamtailor | https://nent.teamtailor.com | 18 | 8 | 5 | 19 | SE 16, ES 2 |
| 835 | Proofpoint | workday | https://proofpoint.wd5.myworkdayjobs.com/proofpointcareers | 18 | 8 | 5 | 145 | IE 10, FR 3, PL 2 |
| 836 | Solactive AG | join_com | https://join.com/companies/solactive | 10 | 8 | 5 | 10 | DE 10 |
| 837 | Sumitomo Mitsui Banking Corporation | successfactors | https://careers.smbcgroup.com/smbc | 8 | 8 | 5 | 598 | IE 8 |
| 838 | Tencent | workday | https://tencent.wd1.myworkdayjobs.com/tencent_careers | 13 | 8 | 5 | 301 | FR 4, NL 4, DE 3 |
| 839 | Twilio | greenhouse | https://job-boards.greenhouse.io/twilio | 10 | 8 | 5 | 153 | IE 7, ES 3 |
| 840 | ALAIKA Advisory | greenhouse | https://job-boards.greenhouse.io/alaika | 12 | 8 | 4 | 12 | DE 12 |
| 841 | Bausparkasse Schwäbisch Hall AG | successfactors | https://jobs.schwaebisch-hall.de | 40 | 8 | 4 | 41 | DE 40 |
| 842 | Callista Group AG | recruitee | https://callista.recruitee.com | 18 | 8 | 4 | 19 | CH 18 |
| 843 | envelio | personio | https://envelio.jobs.personio.com | 10 | 8 | 4 | 11 | DE 10 |
| 844 | NVISO | join_com | https://join.com/companies/nviso | 20 | 8 | 4 | 26 | BE 13, DE 5, AT 2 |
| 845 | West Pharmaceutical Services | successfactors | https://careers.westpharma.com | 69 | 8 | 4 | 260 | DE 41, IE 13, DK 11 |
| 846 | GE Aerospace | phenom | https://careers.geaerospace.com | 29 | 8 | 3 | 520 | PL 18, IT 9, SE 1 |
| 847 | King | phenom | https://careers.king.com | 12 | 8 | 3 | 15 | ES 9, SE 3 |
| 848 | Joom | breezy | https://joom-group.breezy.hr | 13 | 8 | 3 | 16 | PT 11, DE 2 |
| 849 | novonord | successfactors | https://careers.novonordisk.com | 88 | 8 | 3 | 352 | DK 60, FR 19, PL 4 |
| 850 | onetrust | greenhouse | https://job-boards.greenhouse.io/onetrust | 21 | 8 | 3 | 92 | ES 15, NL 4, DE 2 |
| 851 | Septeo | smartrecruiters | https://careers.smartrecruiters.com/Septeo | 56 | 8 | 3 | 58 | FR 44, ES 8, BE 4 |
| 852 | AutoScout24 | greenhouse | https://job-boards.greenhouse.io/autoscout24 | 36 | 8 | 2 | 42 | DE 25, IT 8, NL 2 |
| 853 | Fiserv | phenom | https://careers.fiserv.com | 52 | 8 | 2 | 402 | IE 18, DE 17, IT 14 |
| 854 | Cast AI | greenhouse | https://job-boards.greenhouse.io/castaigroupinc | 11 | 8 | 2 | 29 | PL 8, DE 2, NL 1 |
| 855 | Crusoe | ashby | https://jobs.ashbyhq.com/crusoe | 12 | 8 | 2 | 373 | IE 12 |
| 856 | Docebo | ashby | https://jobs.ashbyhq.com/docebo | 15 | 8 | 2 | 44 | IT 13, DE 2 |
| 857 | Edwards Lifesciences | workday | https://edwards.wd5.myworkdayjobs.com/edwardscareers | 31 | 8 | 2 | 398 | CZ 9, IE 6, FR 5 |
| 858 | Mirakl - Labs | greenhouse | https://job-boards.greenhouse.io/mirakllabs | 16 | 8 | 2 | 16 | FR 16 |
| 859 | Nearform | greenhouse | https://job-boards.greenhouse.io/nearform | 10 | 8 | 2 | 32 | IE 4, IT 4, PL 2 |
| 860 | Skylo Technologies | ashby | https://jobs.ashbyhq.com/skylo | 12 | 8 | 2 | 55 | FI 12 |
| 861 | Stillfront | teamtailor | https://stillfrontgroup.teamtailor.com | 25 | 8 | 2 | 39 | DE 24, SE 1 |
| 862 | CYBRET AI | ashby | https://jobs.ashbyhq.com/cybret | 11 | 8 | 1 | 12 | NO 11 |
| 863 | OrderYOYO | teamtailor | https://orderyoyo.teamtailor.com | 71 | 8 | 1 | 100 | DE 47, NL 13, DK 9 |
| 864 | Sony Music Global Job Board | greenhouse | https://job-boards.greenhouse.io/sonymusicentertainment | 36 | 8 | 1 | 155 | IE 16, FR 8, PL 7 |
| 865 | Clarivate | workday | https://clarivate.wd3.myworkdayjobs.com/clarivate_careers | 9 | 8 | 0 | 175 | ES 9 |
| 866 | Fivetran | greenhouse | https://job-boards.greenhouse.io/fivetran | 14 | 8 | 0 | 209 | IE 11, DE 3 |
| 867 | Lighthouse | greenhouse | https://job-boards.greenhouse.io/lighthouse | 24 | 8 | 0 | 55 | ES 16, BE 8 |
| 868 | Alfasigma jobs | successfactors | https://jobs.alfasigma.com | 97 | 7 | 7 | 112 | DE 47, PL 21, IT 20 |
| 869 | ATP architekten ingenieure | smartrecruiters | https://careers.smartrecruiters.com/ATParchitekteningenieure | 105 | 7 | 7 | 110 | DE 52, CH 28, AT 25 |
| 870 | Austro Holding | smartrecruiters | https://careers.smartrecruiters.com/AustroHolding | 58 | 7 | 7 | 59 | AT 48, DE 8, FR 2 |
| 871 | Autoriteit Financiele Markten | recruitee | https://werkenbijdeafm.recruitee.com | 20 | 7 | 7 | 20 | NL 20 |
| 872 | C.A.R.E. SE | softgarden | _(unknown — not in companies.csv)_ | 78 | 7 | 7 | 78 | DE 78 |
| 873 | Hugo Boss | phenom | https://careers.hugoboss.com | 142 | 7 | 7 | 790 | DE 48, NL 24, FR 16 |
| 874 | Centific | workday | https://centific.wd1.myworkdayjobs.com/centific_global | 8 | 7 | 7 | 148 | ES 8 |
| 875 | Cosine | personio | https://cosine.jobs.personio.com | 14 | 7 | 7 | 14 | NL 11, IT 2, DE 1 |
| 876 | Dcubed | teamtailor | https://dcubed.teamtailor.com | 15 | 7 | 7 | 19 | DE 15 |
| 877 | EIGHT ADVISORY SAS | recruitee | https://8advisory.recruitee.com | 75 | 7 | 7 | 77 | FR 39, DE 16, NL 9 |
| 878 | EIT RawMaterials | teamtailor | https://eitrawmaterialsgmbh.teamtailor.com | 12 | 7 | 7 | 16 | DE 10, BE 2 |
| 879 | Hcib | oracle | https://hcib.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001 | 27 | 7 | 7 | 284 | ES 15, IT 9, DE 1 |
| 880 | Hotel De L'Europe B.V. | recruitee | https://deleuropeamsterdam.recruitee.com | 25 | 7 | 7 | 25 | NL 25 |
| 881 | Incoretex GmbH | join_com | https://join.com/companies/incoretex | 8 | 7 | 7 | 8 | DE 8 |
| 882 | Ingeteam | teamtailor | https://ingeteam.teamtailor.com | 62 | 7 | 7 | 74 | ES 60, IT 2 |
| 883 | Kirey | oracle | https://iabigs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/kirey-career-site | 18 | 7 | 7 | 18 | IT 18 |
| 884 | MSC | cornerstone | https://msc.csod.com/ux/ats/careersite/1/home?c=msc | 80 | 7 | 7 | 185 | DE 30, CH 15, BE 12 |
| 885 | Ncr (Ext Non Us) | workday | https://ncr.wd1.myworkdayjobs.com/ext_non_us | 8 | 7 | 7 | 67 | IT 4, NL 1, FR 1 |
| 886 | OTB Careers | successfactors | https://careers.otb.net | 75 | 7 | 7 | 151 | IT 39, FR 17, DE 9 |
| 887 | Pels Rijcken | recruitee | https://pelsrijcken.recruitee.com | 16 | 7 | 7 | 16 | NL 16 |
| 888 | Peter Schmidt Group GmbH | softgarden | https://peter-schmidt-group.career.softgarden.de/ | 10 | 7 | 7 | 10 | DE 10 |
| 889 | Presidents Institute | greenhouse | https://job-boards.greenhouse.io/presidents | 11 | 7 | 7 | 11 | DK 11 |
| 890 | presidentssummit | greenhouse | https://job-boards.greenhouse.io/presidentssummit | 16 | 7 | 7 | 90 | DK 14, SE 2 |
| 891 | Prevas | teamtailor | https://prevas.teamtailor.com | 38 | 7 | 7 | 39 | SE 35, DK 3 |
| 892 | Private Equity Insights | greenhouse | https://job-boards.greenhouse.io/privateequityinsights | 129 | 7 | 7 | 1157 | DK 33, DE 20, NL 20 |
| 893 | Rocket Internet | smartrecruiters | https://careers.smartrecruiters.com/rocketinternet | 10 | 7 | 7 | 17 | DE 10 |
| 894 | smartbox | bamboohr | https://smartbox.bamboohr.com/careers | 24 | 7 | 7 | 29 | IE 8, IT 6, PT 5 |
| 895 | Starface GmbH | join_com | https://join.com/companies/starface | 13 | 7 | 7 | 13 | DE 12, AT 1 |
| 896 | Trina Solar | personio | https://trina-solar-1.jobs.personio.com | 16 | 7 | 7 | 28 | DE 8, IT 2, PL 2 |
| 897 | Ulrichmedical | cornerstone | https://ulrichmedical.csod.com/ux/ats/careersite/1/home?c=ulrichmedical | 34 | 7 | 7 | 34 | DE 34 |
| 898 | Unit4 | smartrecruiters | https://careers.smartrecruiters.com/Unit44 | 28 | 7 | 7 | 38 | PL 9, PT 8, ES 4 |
| 899 | VDI Technologiezentrum GmbH | softgarden | https://vdijobs.career.softgarden.de/ | 19 | 7 | 7 | 19 | DE 19 |
| 900 | Virtu Financial | greenhouse | https://job-boards.greenhouse.io/virtu | 11 | 7 | 7 | 51 | IE 11 |
| 901 | Wincent | ashby | https://jobs.ashbyhq.com/wincent | 7 | 7 | 7 | 16 | CZ 6, PL 1 |
| 902 | Yousign | teamtailor | https://yousign.teamtailor.com | 15 | 7 | 7 | 15 | FR 8, ES 7 |
| 903 | ADBAKER | join_com | https://join.com/companies/adbaker | 24 | 7 | 6 | 24 | DE 24 |
| 904 | Athora Netherlands | recruitee | https://athora.recruitee.com | 22 | 7 | 6 | 22 | NL 22 |
| 905 | Blackstone | workday | https://blackstone.wd1.myworkdayjobs.com/blackstone_campus_careers | 9 | 7 | 6 | 334 | LU 7, IE 2 |
| 906 | Craftzing | recruitee | https://craftzing.recruitee.com | 16 | 7 | 6 | 16 | BE 16 |
| 907 | CrowdStrike | workday | https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers | 37 | 7 | 6 | 417 | ES 15, DE 10, DK 3 |
| 908 | Exoticca | workable | https://apply.workable.com/exoticca | 31 | 7 | 6 | 40 | ES 31 |
| 909 | Hczf | oracle | https://hczf.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 20 | 7 | 6 | 20 | IT 20 |
| 910 | Lseg | workday | https://lseg.wd3.myworkdayjobs.com/careers | 10 | 7 | 6 | 739 | FR 10 |
| 911 | Luxair | cornerstone | https://luxair.csod.com/ux/ats/careersite/25/home?c=luxair | 50 | 7 | 6 | 50 | LU 50 |
| 912 | MET Group | smartrecruiters | https://careers.smartrecruiters.com/metgroup | 39 | 7 | 6 | 67 | ES 10, CH 10, IT 8 |
| 913 | Motorolasolutions | workday | https://motorolasolutions.wd5.myworkdayjobs.com/careers | 24 | 7 | 6 | 167 | PL 4, DE 4, IT 3 |
| 914 | NinjaOne | jobvite | https://jobs.jobvite.com/ninjaone | 27 | 7 | 6 | 106 | DE 27 |
| 915 | TAPTAP Digital | jazzhr | https://taptapnetworks.applytojob.com | 9 | 7 | 6 | 9 | ES 9 |
| 916 | UNIQA IT Services GmbH | successfactors | https://careers.uniqagroup.com | 63 | 7 | 6 | 63 | AT 63 |
| 917 | winning | jazzhr | https://winning.applytojob.com | 45 | 7 | 6 | 45 | PT 24, ES 18, DE 2 |
| 918 | 12Build | recruitee | https://12build.recruitee.com | 12 | 7 | 5 | 16 | NL 10, BE 2 |
| 919 | Ballast Nedam | recruitee | https://ballastnedam.recruitee.com | 138 | 7 | 5 | 140 | NL 138 |
| 920 | Zimmer Biomet | phenom | https://careers.zimmerbiomet.com | 57 | 7 | 5 | 407 | DE 13, FR 13, PL 11 |
| 921 | conxai | bamboohr | https://conxai.bamboohr.com/careers | 10 | 7 | 5 | 16 | DE 10 |
| 922 | FIS Gruppe | successfactors | https://jobs.fisgruppe.de | 23 | 7 | 5 | 36 | DE 23 |
| 923 | Haeger Consulting GmbH | join_com | https://join.com/companies/haeger-consulting | 7 | 7 | 5 | 7 | DE 7 |
| 924 | Hostinger | ashby | https://jobs.ashbyhq.com/hostinger | 21 | 7 | 5 | 77 | PL 16, ES 3, PT 1 |
| 925 | Hot ITem Groep | join_com | https://join.com/companies/hotitemgroep | 10 | 7 | 5 | 10 | NL 10 |
| 926 | KoRo Handels GmbH | join_com | https://join.com/companies/korodrogerie | 25 | 7 | 5 | 25 | DE 20, FR 4, IT 1 |
| 927 | LIGENTEC | teamtailor | https://ligentec.teamtailor.com | 8 | 7 | 5 | 8 | CH 6, FR 2 |
| 928 | Logitech | workday | https://logitech.wd5.myworkdayjobs.com/logitech | 30 | 7 | 5 | 204 | IE 13, CH 4, DE 3 |
| 929 | MAIT GmbH | softgarden | https://austria-mait.career.softgarden.de/ | 15 | 7 | 5 | 15 | DE 10, AT 5 |
| 930 | MaxAccelerate | workable | https://apply.workable.com/max-accelerate | 25 | 7 | 5 | 49 | PT 7, IE 7, NL 4 |
| 931 | Polestar | teamtailor | https://polestar.teamtailor.com | 21 | 7 | 5 | 28 | SE 15, DE 2, BE 1 |
| 932 | Prelude | ashby | https://jobs.ashbyhq.com/prelude | 16 | 7 | 5 | 17 | FR 16 |
| 933 | sunday | teamtailor | https://sunday.teamtailor.com | 38 | 7 | 5 | 62 | FR 38 |
| 934 | Tenstorrent | greenhouse | https://job-boards.greenhouse.io/tenstorrent | 11 | 7 | 5 | 131 | DE 6, PL 2, ES 2 |
| 935 | WhyHireWrong? | teamtailor | https://whyhirewrong.teamtailor.com | 16 | 7 | 5 | 21 | PL 9, DE 4, CH 2 |
| 936 | A.S. Watson Europe | cornerstone | https://aswatsoneurope.csod.com/ux/ats/careersite/1/home?c=aswatsoneurope | 116 | 7 | 4 | 124 | FR 67, NL 31, IT 18 |
| 937 | Camlin | rippling | https://ats.rippling.com/camlin-careers/jobs | 9 | 7 | 4 | 21 | PL 3, IT 3, ES 3 |
| 938 | Crown Gabelstapler GmbH & Co. KG | softgarden | https://crown.career.softgarden.de/ | 64 | 7 | 4 | 64 | DE 63, CZ 1 |
| 939 | Deutsches Promotionszentrum | recruitee | https://deutschespromotionszentrum.recruitee.com | 46 | 7 | 4 | 63 | DE 46 |
| 940 | Dun & Bradstreet | lever | https://jobs.lever.co/dnb | 29 | 7 | 4 | 124 | PL 9, IE 9, DE 5 |
| 941 | Gcore | smartrecruiters | https://careers.smartrecruiters.com/gcore | 10 | 7 | 4 | 13 | PL 6, DE 3, LU 1 |
| 942 | Laminar Projects | lever | https://jobs.lever.co/laminarprojects | 11 | 7 | 4 | 31 | NO 4, PL 2, PT 2 |
| 943 | Leidos | workday | https://leidos.wd5.myworkdayjobs.com/external | 23 | 7 | 4 | 2167 | DE 10, IT 6, NL 3 |
| 944 | PGIM | workday | https://pru.wd5.myworkdayjobs.com/pgim_careers | 29 | 7 | 4 | 83 | IE 26, DE 2, LU 1 |
| 945 | Pru | workday | https://pru.wd5.myworkdayjobs.com/careers | 29 | 7 | 4 | 164 | IE 26, DE 2, LU 1 |
| 946 | questel | bamboohr | https://questel.bamboohr.com/careers | 17 | 7 | 4 | 49 | FR 11, BE 4, DE 1 |
| 947 | Specialized | workday | https://specialized.wd5.myworkdayjobs.com/specialized_external_career_site | 15 | 7 | 4 | 125 | CH 8, DE 4, NL 2 |
| 948 | StepStone Group | smartrecruiters | https://careers.smartrecruiters.com/StepStoneGroup | 59 | 7 | 4 | 93 | DE 50, IE 4, PL 3 |
| 949 | Telnyx | greenhouse | https://job-boards.greenhouse.io/telnyx54 | 10 | 7 | 4 | 25 | NL 6, IE 3, DE 1 |
| 950 | Urw | workday | https://urw.wd3.myworkdayjobs.com/urw_career_site | 41 | 7 | 4 | 61 | FR 26, NL 4, DE 4 |
| 951 | Visteon | darwinbox | https://visteon-panorama.darwinbox.com/ms/candidate/careers | 13 | 7 | 4 | 102 | PT 11, DE 2 |
| 952 | Iterable | greenhouse | https://job-boards.greenhouse.io/iterable | 8 | 7 | 3 | 19 | PT 8 |
| 953 | Jobandtalent | bamboohr | https://jobandtalent.bamboohr.com/careers | 15 | 7 | 3 | 16 | DE 6, ES 6, PT 1 |
| 954 | NewCold | cornerstone | https://newcold.csod.com/ux/ats/careersite/5/home?c=newcold | 57 | 7 | 3 | 111 | DE 24, NL 13, PL 11 |
| 955 | Signicat | teamtailor | https://signicat.teamtailor.com | 8 | 7 | 3 | 13 | ES 3, NL 3, PT 2 |
| 956 | Blueorigin | workday | https://blueorigin.wd5.myworkdayjobs.com/blueorigin | 7 | 7 | 2 | 1581 | LU 5, PL 2 |
| 957 | Hoxhunt | ashby | https://jobs.ashbyhq.com/hoxhunt | 10 | 7 | 2 | 17 | FI 10 |
| 958 | Ntrs | workday | https://ntrs.wd1.myworkdayjobs.com/northerntrust | 95 | 7 | 2 | 671 | IE 79, LU 12, NL 2 |
| 959 | There's a reason why Fiserv | workday | https://fiserv.wd5.myworkdayjobs.com/ext | 40 | 7 | 2 | 402 | IE 16, IT 13, DE 8 |
| 960 | Vitens | successfactors | https://werkenbijvitens.nl | 28 | 7 | 2 | 28 | NL 28 |
| 961 | Bristolmyerssquibb | workday | https://bristolmyerssquibb.wd5.myworkdayjobs.com/bms | 67 | 7 | 1 | 607 | NL 28, PL 24, IE 8 |
| 962 | Fidelity | workday | https://fmr.wd1.myworkdayjobs.com/fidelitycareers | 13 | 7 | 1 | 576 | IE 10, DE 3 |
| 963 | Huawei Ireland Research Centre | teamtailor | https://huaweiireland.teamtailor.com | 12 | 7 | 1 | 14 | IE 12 |
| 964 | relex | greenhouse | https://job-boards.greenhouse.io/relex | 21 | 7 | 1 | 50 | SE 4, DE 4, ES 3 |
| 965 | Salesfive GmbH | join_com | https://join.com/companies/salesfive | 17 | 7 | 1 | 17 | DE 16, AT 1 |
| 966 | sproutsocial | greenhouse | https://job-boards.greenhouse.io/sproutsocial | 9 | 7 | 1 | 26 | PL 8, IE 1 |
| 967 | Cochlear | workday | https://cochlear.wd3.myworkdayjobs.com/cochlear_careers | 11 | 7 | 0 | 79 | BE 9, DE 1, SE 1 |
| 968 | Dailymotion | smartrecruiters | https://careers.smartrecruiters.com/dailymotion | 12 | 7 | 0 | 13 | FR 12 |
| 969 | EcoVadis | smartrecruiters | https://careers.smartrecruiters.com/ecovadis | 16 | 7 | 0 | 34 | ES 9, PL 6, FR 1 |
| 970 | Sonatus | greenhouse | https://job-boards.greenhouse.io/sonatus | 9 | 7 | 0 | 26 | IE 6, PL 2, DE 1 |
| 971 | SPD Technology | workable | https://apply.workable.com/spd-technology | 9 | 7 | 0 | 13 | ES 5, DE 4 |
| 972 | AB SKF | successfactors | https://career.skf.com | 51 | 6 | 6 | 195 | DE 26, FR 11, PL 4 |
| 973 | ACI Worldwide Job Opportunities | oracle | https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX | 11 | 6 | 6 | 112 | IE 8, FR 1, DE 1 |
| 974 | Alterric Deutschland GmbH | softgarden | https://alterric.career.softgarden.de/ | 18 | 6 | 6 | 18 | DE 18 |
| 975 | Amoria Group | greenhouse | https://job-boards.greenhouse.io/amoriabond | 25 | 6 | 6 | 41 | NL 17, DE 8 |
| 976 | Astemo | workday | https://astemo.wd102.myworkdayjobs.com/global_career_site | 25 | 6 | 6 | 320 | FR 15, DE 9, PL 1 |
| 977 | BarentsKrans | recruitee | https://barentskrans.recruitee.com | 23 | 6 | 6 | 23 | NL 23 |
| 978 | BENTELER | successfactors | https://career.benteler.jobs | 70 | 6 | 6 | 132 | DE 61, PT 6, ES 2 |
| 979 | Cargolux Luxembourg Pilots | oracle | https://cargolux-iajigs.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cargoluxpilots | 32 | 6 | 6 | 47 | LU 27, DE 2, NL 2 |
| 980 | Cw | workday | https://cw.wd1.myworkdayjobs.com/external | 44 | 6 | 6 | 2271 | ES 13, CZ 7, NL 6 |
| 981 | Dentons Group B.V. | successfactors | https://careers.dentons.com | 47 | 6 | 6 | 146 | DE 17, NL 11, IT 10 |
| 982 | EBARA Precision Machinery Europe GmbH | softgarden | https://jobs-ebara-pm.career.softgarden.de/ | 14 | 6 | 6 | 16 | DE 8, IE 6 |
| 983 | ESAB | workday | https://esab.wd5.myworkdayjobs.com/esabcareers | 19 | 6 | 6 | 116 | SE 7, FR 4, IT 3 |
| 984 | Experian | smartrecruiters | https://careers.smartrecruiters.com/experian | 21 | 6 | 6 | 451 | DE 12, IT 5, NO 2 |
| 985 | fair parken GmbH | softgarden | https://fairparken.career.softgarden.de/ | 44 | 6 | 6 | 44 | DE 44 |
| 986 | Flink | smartrecruiters | https://careers.smartrecruiters.com/Flink3 | 113 | 6 | 6 | 113 | NL 73, DE 40 |
| 987 | Gen Re | cornerstone | https://genre.csod.com/ux/ats/careersite/1/home?c=genre | 11 | 6 | 6 | 33 | DE 9, FR 1, DK 1 |
| 988 | Industria de Turbo Propulsores S.A.U. | successfactors | https://careers.itpaero.com | 21 | 6 | 6 | 58 | IT 11, ES 10 |
| 989 | Kapten & Son | recruitee | https://karriere.kapten-son.com | 26 | 6 | 6 | 27 | DE 23, NL 2, AT 1 |
| 990 | Kempinski Hotels | pinpoint | https://kempinski.pinpointhq.com | 124 | 6 | 6 | 304 | DE 78, CH 35, AT 11 |
| 991 | Klépierre | oracle | https://fa-ewfm-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/klepierre | 41 | 6 | 6 | 41 | FR 36, CZ 2, ES 2 |
| 992 | Large Space Structures GmbH | join_com | https://join.com/companies/largespace | 7 | 6 | 6 | 7 | DE 7 |
| 993 | L.E.A.SE. S.A. | join_com | https://join.com/companies/lease | 43 | 6 | 6 | 43 | BE 29, LU 14 |
| 994 | LILT (Production) | ashby | https://jobs.ashbyhq.com/lilt-production | 56 | 6 | 6 | 408 | DE 14, FR 12, IT 8 |
| 995 | Munters | workday | https://munters.wd3.myworkdayjobs.com/external_careers | 13 | 6 | 6 | 117 | NL 3, SE 3, DE 2 |
| 996 | Novaspace | lever | https://jobs.lever.co/novaspace | 24 | 6 | 6 | 28 | FR 17, BE 5, DE 2 |
| 997 | Öresund IT | teamtailor | https://oresundit.teamtailor.com | 7 | 6 | 6 | 7 | SE 7 |
| 998 | recrute1-carrefour | icims | https://recrute1-carrefour.icims.com | 938 | 6 | 6 | 938 | FR 937, ES 1 |
| 999 | Remmert GmbH | join_com | https://join.com/companies/remmert | 79 | 6 | 6 | 79 | DE 73, PL 6 |
| 1000 | SANTA LUCIA, S.A. | successfactors | https://trabajo.santalucia.es/Santalucia | 134 | 6 | 6 | 134 | ES 134 |
| 1001 | starface | personio | https://starface.jobs.personio.com | 8 | 6 | 6 | 8 | DE 8 |
| 1002 | strategie:p personalberatung | join_com | https://join.com/companies/strategie-p | 19 | 6 | 6 | 19 | DE 19 |
| 1003 | Street Child | workable | https://apply.workable.com/streetchildcareers | 10 | 6 | 6 | 33 | ES 6, DE 3, FR 1 |
| 1004 | Swissquote | smartrecruiters | https://careers.smartrecruiters.com/swissquote | 30 | 6 | 6 | 35 | CH 28, LU 2 |
| 1005 | Trimble | workday | https://trimble.wd1.myworkdayjobs.com/trimblecareers | 18 | 6 | 6 | 221 | DE 8, FR 3, IE 2 |
| 1006 | UPCnet | cornerstone | https://upcnet.csod.com/ux/ats/careersite/1/home?c=upcnet | 9 | 6 | 6 | 9 | ES 9 |
| 1007 | Veliu | ashby | https://jobs.ashbyhq.com/veliu | 8 | 6 | 6 | 9 | IT 8 |
| 1008 | VO2 Group | lever | https://jobs.lever.co/vo2-group | 20 | 6 | 6 | 38 | FR 20 |
| 1009 | Westfalen AG & Co. KG | successfactors | https://karriere.westfalen.com/westfalenag | 70 | 6 | 6 | 70 | DE 70 |
| 1010 | AGILITA DE | softgarden | _(unknown — not in companies.csv)_ | 15 | 6 | 5 | 15 | DE 15 |
| 1011 | AMD | successfactors | https://performancemanager4.successfactors.com/career?company=AMD | 7 | 6 | 5 | 351 | IE 6, FR 1 |
| 1012 | andrena objects AG | softgarden | https://andrena.career.softgarden.de/ | 9 | 6 | 5 | 9 | DE 9 |
| 1013 | Andres Industries AG | join_com | https://join.com/companies/andres-industries | 23 | 6 | 5 | 23 | DE 23 |
| 1014 | Aquablu B.V | recruitee | https://aquablu.recruitee.com | 17 | 6 | 5 | 18 | NL 17 |
| 1015 | ASI | smartrecruiters | https://careers.smartrecruiters.com/ASIFR | 49 | 6 | 5 | 49 | FR 49 |
| 1016 | Atecna | teamtailor | https://atecna.teamtailor.com | 14 | 6 | 5 | 14 | FR 14 |
| 1017 | BizAway | jazzhr | https://bizaway.applytojob.com | 31 | 6 | 5 | 37 | ES 22, IT 9 |
| 1018 | BlaBlaCar | lever | https://jobs.lever.co/blablacar | 10 | 6 | 5 | 10 | FR 10 |
| 1019 | Bosch-HomeComfort | smartrecruiters | https://careers.smartrecruiters.com/bosch-homecomfort | 14 | 6 | 5 | 177 | ES 8, FR 6 |
| 1020 | BTC | cornerstone | https://btc.csod.com/ux/ats/careersite/1/home?c=btc | 179 | 6 | 5 | 191 | DE 176, SE 2, CH 1 |
| 1021 | Carlsberg Global Business Services A/S | successfactors | https://careers.britvic.com/BritvicIreland | 58 | 6 | 5 | 119 | IE 11, CH 10, PL 10 |
| 1022 | Click&Boat | teamtailor | https://clickboat.teamtailor.com | 16 | 6 | 5 | 16 | ES 16 |
| 1023 | contentguru | bamboohr | https://contentguru.bamboohr.com/careers | 11 | 6 | 5 | 39 | DE 5, PT 4, NL 2 |
| 1024 | cronoseuropa | bamboohr | https://cronoseuropa.bamboohr.com/careers | 21 | 6 | 5 | 25 | BE 16, LU 4, FR 1 |
| 1025 | DataDome | greenhouse | https://job-boards.greenhouse.io/ddome | 11 | 6 | 5 | 17 | FR 11 |
| 1026 | Electricity Supply Board | successfactors | https://careers.esb.ie | 21 | 6 | 5 | 21 | IE 21 |
| 1027 | Envalior | workable | https://apply.workable.com/envalior | 26 | 6 | 5 | 52 | DE 13, BE 8, NL 4 |
| 1028 | Extreme Networks | lever | https://jobs.lever.co/extremenetworks | 20 | 6 | 5 | 142 | DE 9, ES 3, NL 2 |
| 1029 | Forterro | pinpoint | https://forterro.pinpointhq.com | 28 | 6 | 5 | 51 | DE 11, FR 5, SE 3 |
| 1030 | FreshMinds | recruitee | https://freshminds.recruitee.com | 13 | 6 | 5 | 13 | NL 13 |
| 1031 | Gisa | successfactors | https://karriere.gisa.de | 23 | 6 | 5 | 23 | DE 23 |
| 1032 | Grünenthal GmbH | successfactors | https://careers.grunenthal.com | 40 | 6 | 5 | 47 | DE 27, IT 7, PT 4 |
| 1033 | Huawei Sweden | teamtailor | https://huawei.teamtailor.com | 9 | 6 | 5 | 9 | FI 3, NO 3, DK 2 |
| 1034 | IATA | cornerstone | https://iata.csod.com/ux/ats/careersite/1/home?c=iata | 20 | 6 | 5 | 43 | CH 15, ES 3, BE 2 |
| 1035 | passport Business Engineering GmbH | join_com | https://join.com/companies/passport-gmbh | 22 | 6 | 5 | 22 | DE 22 |
| 1036 | Pfizer | workday | https://pfizer.wd1.myworkdayjobs.com/pfizercareers | 29 | 6 | 5 | 532 | DE 13, BE 7, IE 2 |
| 1037 | PRIOjet GmbH | join_com | https://join.com/companies/priojet | 6 | 6 | 5 | 6 | DE 6 |
| 1038 | profine GmbH | softgarden | https://profine-group.career.softgarden.de/ | 57 | 6 | 5 | 59 | DE 53, NL 2, IT 1 |
| 1039 | Pyyne Digital | teamtailor | https://pyynedigital.teamtailor.com | 6 | 6 | 5 | 6 | PT 3, SE 3 |
| 1040 | Recorded Future | greenhouse | https://job-boards.greenhouse.io/recordedfuture | 6 | 6 | 5 | 43 | SE 6 |
| 1041 | redwirespaceeurope | greenhouse | https://job-boards.greenhouse.io/redwirespaceeurope | 12 | 6 | 5 | 12 | BE 8, LU 2, PL 2 |
| 1042 | Sec | workday | https://sec.wd3.myworkdayjobs.com/samsung_careers | 25 | 6 | 5 | 691 | DE 8, FR 7, NL 3 |
| 1043 | Selectra | jazzhr | https://selectra.applytojob.com | 35 | 6 | 5 | 40 | ES 23, FR 9, BE 2 |
| 1044 | SERVICEPLAN Gruppe | softgarden | https://serviceplan.career.softgarden.de/ | 60 | 6 | 5 | 60 | DE 60 |
| 1045 | Shift Technology | greenhouse | https://job-boards.greenhouse.io/shifttechnology | 9 | 6 | 5 | 23 | FR 7, ES 1, DE 1 |
| 1046 | SpiraTec AG | softgarden | https://spiratec.career.softgarden.de/ | 126 | 6 | 5 | 141 | DE 106, IT 8, FR 6 |
| 1047 | SPX FLOW | successfactors | https://career8.successfactors.com/career?company=spxflowP | 31 | 6 | 5 | 95 | PL 17, ES 4, DK 4 |
| 1048 | Statkraft | smartrecruiters | https://careers.smartrecruiters.com/statkraft1 | 42 | 6 | 5 | 98 | NO 11, DE 11, IE 8 |
| 1049 | Telenet | oracle | https://ebza.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cx_1001 | 58 | 6 | 5 | 58 | BE 58 |
| 1050 | thor | bamboohr | https://thor.bamboohr.com/careers | 15 | 6 | 5 | 15 | FI 15 |
| 1051 | Umdasch Group | smartrecruiters | https://careers.smartrecruiters.com/umdaschgroup | 102 | 6 | 5 | 168 | DE 48, AT 33, PL 5 |
| 1052 | USZ Jobs & Karriere | successfactors | https://job.usz.ch | 256 | 6 | 5 | 256 | CH 256 |
| 1053 | Abtrace | workable | https://apply.workable.com/abtrace | 6 | 6 | 4 | 13 | PT 6 |
| 1054 | Accelleron | workday | https://accelleron.wd3.myworkdayjobs.com/accelleron | 30 | 6 | 4 | 111 | CH 22, PL 3, NL 2 |
| 1055 | Alter Domus Participations SARL | successfactors | https://jobs.alterdomus.com | 39 | 6 | 4 | 200 | LU 19, ES 7, IE 5 |
| 1056 | Aneo | recruitee | https://aneo.recruitee.com | 8 | 6 | 4 | 8 | FR 8 |
| 1057 | ANGEHEUERT GmbH | recruitee | https://deintraumjobwartet.recruitee.com | 126 | 6 | 4 | 190 | AT 69, DE 56, IT 1 |
| 1058 | binderholz | join_com | https://join.com/companies/binderholz | 95 | 6 | 4 | 98 | AT 54, DE 40, FR 1 |
| 1059 | U.S. Bank | phenom | https://careers.usbank.com | 43 | 6 | 4 | 1361 | PL 26, IE 15, LU 2 |
| 1060 | Clariant International Ltd. | successfactors | https://careers.clariant.com | 37 | 6 | 4 | 105 | DE 24, FR 4, ES 4 |
| 1061 | Collabera | smartrecruiters | https://careers.smartrecruiters.com/collabera2 | 9 | 6 | 4 | 1683 | PL 9 |
| 1062 | Db | workday | https://db.wd3.myworkdayjobs.com/dbwebsite | 14 | 6 | 4 | 1146 | LU 14 |
| 1063 | DLL Group | oracle | https://iabcbn.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 25 | 6 | 4 | 42 | NL 15, FR 4, DE 3 |
| 1064 | Dufry International AG | successfactors | https://careers.avoltaworld.com | 215 | 6 | 4 | 1339 | CH 112, NL 76, BE 10 |
| 1065 | Fa Exhj Saasfaprod1 | oracle | https://fa-exhj-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 17 | 6 | 4 | 95 | FR 10, IE 3, CZ 2 |
| 1066 | Flextronics (Anord Mardix Careers) | workday | https://flextronics.wd1.myworkdayjobs.com/Anord_Mardix_Careers | 17 | 6 | 4 | 121 | IE 13, PL 4 |
| 1067 | IKA Werke | softgarden | https://trainee-ikajobs.career.softgarden.de/ | 21 | 6 | 4 | 29 | DE 20, BE 1 |
| 1068 | IONOS SE | join_com | https://join.com/companies/ionos | 22 | 6 | 4 | 22 | DE 22 |
| 1069 | LACROIX | smartrecruiters | https://careers.smartrecruiters.com/LACROIX1 | 38 | 6 | 4 | 45 | FR 15, DE 12, PL 9 |
| 1070 | Radius Limited | smartrecruiters | https://careers.smartrecruiters.com/radiuslimited | 68 | 6 | 4 | 160 | IE 32, DE 14, BE 9 |
| 1071 | SICPA | successfactors | https://jobs.sicpa.com | 22 | 6 | 4 | 50 | CH 17, ES 5 |
| 1072 | thunes | greenhouse | https://job-boards.greenhouse.io/thunes | 14 | 6 | 4 | 53 | ES 11, FR 3 |
| 1073 | Transparent Hiring | breezy | https://transparent-hiring.breezy.hr | 11 | 6 | 4 | 18 | DE 11 |
| 1074 | Uber | uber | ats: uber | 55 | 6 | 4 | 521 | DE 17, FR 9, NL 6 |
| 1075 | Unisys | workday | https://unisys.wd5.myworkdayjobs.com/external | 13 | 6 | 4 | 416 | PL 5, NL 3, CH 2 |
| 1076 | Uppstuk | teamtailor | https://uppstuk.teamtailor.com | 23 | 6 | 4 | 23 | SE 23 |
| 1077 | Zattoo | join_com | https://join.com/companies/zattoo | 10 | 6 | 4 | 10 | DE 10 |
| 1078 | AirHelp | workable | https://apply.workable.com/airhelp | 11 | 6 | 3 | 11 | PL 6, DE 4, PT 1 |
| 1079 | ANYbotics | lever | https://jobs.lever.co/anybotics | 8 | 6 | 3 | 17 | CH 8 |
| 1080 | AXI Holding Services | recruitee | https://axi.recruitee.com | 18 | 6 | 3 | 18 | BE 18 |
| 1081 | Bitpanda | greenhouse | https://job-boards.greenhouse.io/bitpanda | 24 | 6 | 3 | 24 | AT 18, ES 3, DE 3 |
| 1082 | DuPont | phenom | https://careers.dupont.com | 45 | 6 | 3 | 203 | DE 14, BE 9, CH 7 |
| 1083 | Circu Li-Ion | personio | https://circu-li-ion.jobs.personio.com | 14 | 6 | 3 | 15 | LU 14 |
| 1084 | coni+partner AG | join_com | https://join.com/companies/coni-partner | 20 | 6 | 3 | 20 | CH 20 |
| 1085 | CORSAIR | oracle | https://edix.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 15 | 6 | 3 | 94 | DE 15 |
| 1086 | Endava | smartrecruiters | https://careers.smartrecruiters.com/endava | 8 | 6 | 3 | 187 | PL 6, DE 2 |
| 1087 | EQT Group | greenhouse | https://job-boards.greenhouse.io/eqtpartners | 14 | 6 | 3 | 23 | SE 7, PL 4, NL 2 |
| 1088 | Example Corp | greenhouse | https://job-boards.greenhouse.io/examplecorpsandbox | 13 | 6 | 3 | 221 | DE 4, ES 2, FR 2 |
| 1089 | Five9 | greenhouse | https://job-boards.greenhouse.io/five9 | 10 | 6 | 3 | 118 | PT 9, ES 1 |
| 1090 | Focal Systems | greenhouse | https://job-boards.greenhouse.io/focalsystems | 6 | 6 | 3 | 34 | PL 5, PT 1 |
| 1091 | Groupement Mousquetaires | smartrecruiters | https://careers.smartrecruiters.com/GroupementMousquetaires3 | 403 | 6 | 3 | 403 | FR 403 |
| 1092 | Guardsquare | greenhouse | https://job-boards.greenhouse.io/guardsquare | 10 | 6 | 3 | 16 | BE 9, DE 1 |
| 1093 | klaviyo | greenhouse | https://job-boards.greenhouse.io/klaviyo | 16 | 6 | 3 | 137 | IE 15, FR 1 |
| 1094 | Mendo.ai | teamtailor | https://mendoai-1730204279.teamtailor.com | 12 | 6 | 3 | 12 | FR 12 |
| 1095 | project44 | greenhouse | https://job-boards.greenhouse.io/project44 | 6 | 6 | 3 | 32 | PL 2, DE 2, NL 2 |
| 1096 | StackAI | ashby | https://jobs.ashbyhq.com/stack-ai | 6 | 6 | 3 | 20 | PL 4, ES 2 |
| 1097 | Too Good To Go | greenhouse | https://job-boards.greenhouse.io/toogoodtogo | 35 | 6 | 3 | 54 | DK 8, DE 7, NL 5 |
| 1098 | Cloudera | workday | https://cloudera.wd5.myworkdayjobs.com/external_career | 14 | 6 | 2 | 70 | CZ 7, CH 2, IE 1 |
| 1099 | EMIL Group GmbH | join_com | https://join.com/companies/emil | 12 | 6 | 2 | 12 | DE 11, CH 1 |
| 1100 | emil-group-gmbh | personio | https://emil-group-gmbh.jobs.personio.com | 10 | 6 | 2 | 10 | DE 9, PL 1 |
| 1101 | Ig | workday | https://ig.wd103.myworkdayjobs.com/ext_ig | 18 | 6 | 2 | 57 | PL 18 |
| 1102 | Mozilla | greenhouse | https://job-boards.greenhouse.io/mozilla | 22 | 6 | 2 | 69 | DE 8, FR 4, SE 2 |
| 1103 | Mtb | workday | https://mtb.wd5.myworkdayjobs.com/campus | 56 | 6 | 2 | 909 | DE 54, IE 2 |
| 1104 | NICE | greenhouse | https://job-boards.greenhouse.io/nice | 13 | 6 | 2 | 185 | DE 9, SE 2, NL 1 |
| 1105 | NNIT | cornerstone | https://nnit.csod.com/ux/ats/careersite/1/home?c=nnit | 22 | 6 | 2 | 25 | IE 9, IT 6, CH 3 |
| 1106 | Octapharma AG | successfactors | https://careers.octapharma.com | 58 | 6 | 2 | 58 | DE 22, FR 15, AT 14 |
| 1107 | Pipedrive | lever | https://jobs.lever.co/pipedrive | 8 | 6 | 2 | 16 | IE 3, DE 3, PT 2 |
| 1108 | Remitly | workday | https://remitly.wd5.myworkdayjobs.com/remitly_careers | 7 | 6 | 2 | 171 | PL 6, IE 1 |
| 1109 | Talkdesk | greenhouse | https://job-boards.greenhouse.io/talkdesk2 | 8 | 6 | 2 | 47 | PT 7, DE 1 |
| 1110 | Windhoff Group | join_com | https://join.com/companies/windhoff-group | 6 | 6 | 2 | 6 | DE 6 |
| 1111 | Attio | ashby | https://jobs.ashbyhq.com/attio | 7 | 6 | 1 | 48 | PL 6, PT 1 |
| 1112 | BearingPoint Netherlands | teamtailor | https://bearingpointnetherlands.teamtailor.com | 19 | 6 | 1 | 19 | NL 19 |
| 1113 | Collibra | greenhouse | https://job-boards.greenhouse.io/collibra | 11 | 6 | 1 | 38 | BE 9, CZ 1, PL 1 |
| 1114 | Deloitte | smartrecruiters | https://careers.smartrecruiters.com/DeloitteAT | 108 | 6 | 1 | 108 | AT 108 |
| 1115 | Falk Defence GmbH | join_com | https://join.com/companies/falkdefencecom | 9 | 6 | 1 | 9 | DE 9 |
| 1116 | Hccz | oracle | https://hccz.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2 | 13 | 6 | 1 | 374 | PL 9, ES 4 |
| 1117 | waymo | greenhouse | https://job-boards.greenhouse.io/waymo | 7 | 6 | 1 | 341 | PL 7 |
| 1118 | ABC Financial Services | workday | https://abcfinancial.wd5.myworkdayjobs.com/ABCFinancialServices | 9 | 6 | 0 | 31 | PL 9 |
| 1119 | commercetools | greenhouse | https://job-boards.greenhouse.io/commercetools | 11 | 6 | 0 | 22 | DE 9, ES 2 |
| 1120 | Digital Turbine | workday | https://digitalturbine.wd501.myworkdayjobs.com/digital_turbine_external_careers | 6 | 6 | 0 | 41 | DE 3, PL 3 |
| 1121 | eRecht24 GmbH & Co. KG | join_com | https://join.com/companies/e-recht24 | 11 | 6 | 0 | 11 | DE 11 |
| 1122 | expleo-jobs-ie-en | icims | https://expleo-jobs-ie-en.icims.com | 17 | 6 | 0 | 17 | IE 17 |
| 1123 | OutSystems | workday | https://outsystems.wd503.myworkdayjobs.com/outsystems | 25 | 6 | 0 | 85 | PT 17, NL 4, DE 3 |
| 1124 | Pigment | lever | https://jobs.lever.co/pigment | 10 | 6 | 0 | 120 | FR 9, CH 1 |
| 1125 | Rebtel | teamtailor | https://rebtel.teamtailor.com | 6 | 6 | 0 | 6 | SE 6 |
| 1126 | Advokatfirman Delphi | teamtailor | https://advokatfirmandelphi.teamtailor.com | 9 | 5 | 5 | 10 | SE 9 |
| 1127 | Appsilon | teamtailor | https://appsilon-1739358905.teamtailor.com | 10 | 5 | 5 | 12 | PL 10 |
| 1128 | Assecor GmbH | join_com | https://join.com/companies/assecor | 10 | 5 | 5 | 10 | DE 10 |
| 1129 | AXSOL GmbH | join_com | https://join.com/companies/axsol | 10 | 5 | 5 | 10 | DE 10 |
| 1130 | Blackrock | workday | https://blackrock.wd1.myworkdayjobs.com/blackrock_professional | 18 | 5 | 5 | 284 | DE 16, LU 1, IE 1 |
| 1131 | Burson | greenhouse | https://job-boards.greenhouse.io/bursonglobalcareers | 37 | 5 | 5 | 191 | FR 11, ES 7, BE 5 |
| 1132 | c.cure - Geschäftsbereich der Megamaris GmbH | join_com | https://join.com/companies/ccure | 24 | 5 | 5 | 24 | DE 24 |
| 1133 | Doctario GmbH | join_com | https://join.com/companies/doctariode | 5 | 5 | 5 | 5 | DE 5 |
| 1134 | EnCharge AI | greenhouse | https://job-boards.greenhouse.io/enchargeai36 | 5 | 5 | 5 | 26 | DE 5 |
| 1135 | Enzazaden | workday | https://enzazaden.wd103.myworkdayjobs.com/enza-careers | 42 | 5 | 5 | 69 | NL 29, ES 10, DE 2 |
| 1136 | Erm | workday | https://erm.wd3.myworkdayjobs.com/erm_careers | 22 | 5 | 5 | 527 | IT 8, NL 4, IE 2 |
| 1137 | evroc | teamtailor | https://evrocab-1692891239.teamtailor.com | 9 | 5 | 5 | 10 | SE 8, FR 1 |
| 1138 | Excellent Go4 | join_com | https://join.com/companies/excellent1 | 100 | 5 | 5 | 100 | CH 100 |
| 1139 | inca | join_com | https://join.com/companies/get-incacom | 8 | 5 | 5 | 8 | DE 8 |
| 1140 | gospooky | bamboohr | https://gospooky.bamboohr.com/careers | 18 | 5 | 5 | 18 | NL 18 |
| 1141 | greenventory GmbH | join_com | https://join.com/companies/greenventory | 17 | 5 | 5 | 17 | DE 17 |
| 1142 | Hallo Welt! GmbH | join_com | https://join.com/companies/hallowelt | 6 | 5 | 5 | 6 | DE 6 |
| 1143 | Hcqt | oracle | https://hcqt.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Reale-Group | 25 | 5 | 5 | 25 | IT 25 |
| 1144 | Heppner Group | cornerstone | https://heppner-group.csod.com/ux/ats/careersite/4/home?c=heppner-group | 70 | 5 | 5 | 70 | FR 48, DE 18, NL 4 |
| 1145 | Hived NV | recruitee | https://hived.recruitee.com | 8 | 5 | 5 | 8 | BE 8 |
| 1146 | Hôpitaux Universitaires de Genève | smartrecruiters | https://careers.smartrecruiters.com/HUG | 44 | 5 | 5 | 44 | CH 44 |
| 1147 | IRIUM - Spain | jazzhr | https://iriumspain.applytojob.com | 15 | 5 | 5 | 32 | ES 15 |
| 1148 | JERÓNIMO MARTINS, S.G.P.S., S.A. | successfactors | https://careers.jeronimomartins.com/Biedronka | 1665 | 5 | 5 | 1960 | PL 1381, PT 284 |
| 1149 | kandou | bamboohr | https://kandou.bamboohr.com/careers | 8 | 5 | 5 | 27 | CH 8 |
| 1150 | Kenvue | workday | https://kenvue.wd5.myworkdayjobs.com/kenvue | 20 | 5 | 5 | 168 | ES 6, FR 5, SE 3 |
| 1151 | Koenig & Bauer Aktiengesellschaft | successfactors | https://jobs.koenig-bauer.com | 48 | 5 | 5 | 48 | DE 46, AT 2 |
| 1152 | LavazzaLuigi | successfactors | https://jobs.lavazza.com | 17 | 5 | 5 | 22 | IT 11, DE 4, AT 1 |
| 1153 | LinoPro GmbH | join_com | https://join.com/companies/linopro | 5 | 5 | 5 | 5 | DE 5 |
| 1154 | Lnds | personio | https://lnds.jobs.personio.com | 12 | 5 | 5 | 12 | LU 12 |
| 1155 | Lombardodier | workday | https://lombardodier.wd3.myworkdayjobs.com/lombard_odier_careers | 7 | 5 | 5 | 40 | LU 7 |
| 1156 | Marvell | workday | https://marvell.wd1.myworkdayjobs.com/marvellcareers | 5 | 5 | 5 | 135 | IT 5 |
| 1157 | Meet5 GmbH | join_com | https://join.com/companies/meet5 | 7 | 5 | 5 | 7 | DE 7 |
| 1158 | Minebea Intec GmbH | successfactors | https://jobs.minebea-intec.com | 12 | 5 | 5 | 12 | DE 10, CH 1, ES 1 |
| 1159 | PCS Professional Clinical Software GmbH | join_com | https://join.com/companies/pcs | 7 | 5 | 5 | 7 | AT 7 |
| 1160 | bigawin ag | join_com | https://join.com/companies/permwin | 98 | 5 | 5 | 98 | CH 98 |
| 1161 | PIA Group Nederland | recruitee | _(unknown — not in companies.csv)_ | 133 | 5 | 5 | 136 | NL 133 |
| 1162 | Silex | teamtailor | https://silex.teamtailor.com | 8 | 5 | 5 | 8 | SE 8 |
| 1163 | SKYTREE | recruitee | https://skytree.recruitee.com | 7 | 5 | 5 | 7 | NL 4, PL 3 |
| 1164 | subduxion | rippling | https://ats.rippling.com/subduxion/jobs | 6 | 5 | 5 | 6 | NL 6 |
| 1165 | Tmhcc | workday | https://tmhcc.wd108.myworkdayjobs.com/external | 13 | 5 | 5 | 140 | ES 10, FR 1, LU 1 |
| 1166 | TUI InfoTec GmbH | successfactors | https://jobs.tuigroup.com | 24 | 5 | 5 | 507 | BE 13, AT 7, PL 2 |
| 1167 | TWK | join_com | https://join.com/companies/twk | 13 | 5 | 5 | 13 | DE 13 |
| 1168 | Vanta | ashby | https://jobs.ashbyhq.com/vanta | 12 | 5 | 5 | 112 | IE 12 |
| 1169 | Vitestro | recruitee | https://vitestro.recruitee.com | 6 | 5 | 5 | 6 | NL 6 |
| 1170 | Walter | workday | https://sandvik.wd3.myworkdayjobs.com/walter-jobs | 12 | 5 | 5 | 25 | DE 7, AT 2, FR 1 |
| 1171 | WBS SCHULEN | softgarden | https://wbs-gruppe.career.softgarden.de/ | 27 | 5 | 5 | 27 | DE 27 |
| 1172 | Hudson River Trading | greenhouse | https://job-boards.greenhouse.io/wehrtyou | 5 | 5 | 5 | 75 | NO 3, IE 2 |
| 1173 | Wienerberger | cornerstone | https://wienerberger.csod.com/ux/ats/careersite/1/home?c=wienerberger | 149 | 5 | 5 | 165 | FR 77, DE 48, AT 24 |
| 1174 | ZF Friedrichshafen AG | successfactors | https://jobs.zf.com | 13 | 5 | 5 | 820 | DE 5, PL 4, CZ 2 |
| 1175 | adidas | successfactors | https://jobs.adidas-group.com | 252 | 5 | 4 | 1070 | DE 114, FR 55, NL 24 |
| 1176 | Adjust | recruitee | https://adjust.recruitee.com | 29 | 5 | 4 | 29 | NL 29 |
| 1177 | Air Space Intelligence | ashby | https://jobs.ashbyhq.com/airspace-intelligence.com | 5 | 5 | 4 | 28 | PL 5 |
| 1178 | Allstate | workday | https://allstate.wd5.myworkdayjobs.com/allstate_careers | 10 | 5 | 4 | 461 | IT 10 |
| 1179 | AST SpaceMobile | greenhouse | https://job-boards.greenhouse.io/astspacemobile | 13 | 5 | 4 | 229 | ES 13 |
| 1180 | atlantic.vc | recruitee | _(unknown — not in companies.csv)_ | 10 | 5 | 4 | 11 | DE 9, FR 1 |
| 1181 | Bundl | recruitee | https://bundl.recruitee.com | 6 | 5 | 4 | 6 | BE 6 |
| 1182 | careersen-itt-inc | icims | https://careersen-itt-inc.icims.com | 13 | 5 | 4 | 34 | NL 8, IT 3, DE 2 |
| 1183 | Coca-Cola Company | workday | https://coke.wd1.myworkdayjobs.com/coca-cola-careers | 14 | 5 | 4 | 202 | DE 5, IE 5, FR 2 |
| 1184 | CommScope Inc., of North Carolina | successfactors | https://jobs.commscope.com | 7 | 5 | 4 | 80 | BE 2, FR 1, DK 1 |
| 1185 | COWMANAGER B.V. | recruitee | https://cowmanager.recruitee.com | 10 | 5 | 4 | 12 | NL 10 |
| 1186 | Crypto Finance AG | workable | https://apply.workable.com/crypto-finance | 10 | 5 | 4 | 10 | CH 8, DE 2 |
| 1187 | dywidag | bamboohr | https://dywidag.bamboohr.com/careers | 27 | 5 | 4 | 43 | DE 12, PL 9, FR 3 |
| 1188 | Einhell Germany AG | softgarden | https://einhell.career.softgarden.de/ | 29 | 5 | 4 | 29 | DE 28, AT 1 |
| 1189 | ELMI Power GmbH | join_com | https://join.com/companies/elmipowerde | 10 | 5 | 4 | 10 | DE 10 |
| 1190 | Energie und Wasser Potsdam GmbH | softgarden | _(unknown — not in companies.csv)_ | 14 | 5 | 4 | 14 | DE 14 |
| 1191 | External Site | oracle | https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1 | 29 | 5 | 4 | 39 | FR 28, LU 1 |
| 1192 | Huawei Finland R&D | teamtailor | https://huaweifinlandrnd.teamtailor.com | 10 | 5 | 4 | 10 | FI 10 |
| 1193 | INP Schweiz AG | join_com | https://join.com/companies/inp-e | 8 | 5 | 4 | 8 | CH 8 |
| 1194 | Integer | workday | https://integer.wd1.myworkdayjobs.com/external | 15 | 5 | 4 | 256 | IE 15 |
| 1195 | intrinsicrobotics | greenhouse | https://job-boards.greenhouse.io/intrinsicrobotics | 7 | 5 | 4 | 23 | DE 7 |
| 1196 | Jobs for Humanity | smartrecruiters | https://careers.smartrecruiters.com/jobsforhumanity | 72 | 5 | 4 | 3211 | NL 24, PL 15, FR 10 |
| 1197 | Kombo | smartrecruiters | https://careers.smartrecruiters.com/kombo | 35 | 5 | 4 | 155 | DE 16, SE 10, NL 2 |
| 1198 | Mhs | workday | https://mhs.wd1.myworkdayjobs.com/careers | 7 | 5 | 4 | 58 | NL 5, IT 2 |
| 1199 | Millennium | cornerstone | https://millenniumelearning.csod.com/ux/ats/careersite/2/home?c=millenniumelearning | 180 | 5 | 4 | 180 | PL 180 |
| 1200 | Mister Spex | workday | https://misterspex.wd103.myworkdayjobs.com/Mister_Spex_Careers | 73 | 5 | 4 | 85 | DE 73 |
| 1201 | mrge - commerce advertising | join_com | https://join.com/companies/mrge | 5 | 5 | 4 | 6 | DE 5 |
| 1202 | murphygs | bamboohr | https://murphygs.bamboohr.com/careers | 16 | 5 | 4 | 20 | IE 11, DE 5 |
| 1203 | Mynt | teamtailor | https://mynt.teamtailor.com | 8 | 5 | 4 | 8 | SE 5, FI 1, NO 1 |
| 1204 | NETZSCH Group | smartrecruiters | https://careers.smartrecruiters.com/NETZSCHGroup | 43 | 5 | 4 | 54 | DE 37, FR 3, BE 2 |
| 1205 | Norbert Health | breezy | https://norbert-health.breezy.hr | 5 | 5 | 4 | 13 | FR 5 |
| 1206 | Nozomi Networks | greenhouse | https://job-boards.greenhouse.io/nozominetworks | 7 | 5 | 4 | 18 | IT 3, NL 2, CH 1 |
| 1207 | OLAM INTERNATIONAL LTD | successfactors | https://careers.ofi.com | 10 | 5 | 4 | 30 | NL 9, ES 1 |
| 1208 | Pae | workday | https://pae.wd1.myworkdayjobs.com/amentum_careers | 74 | 5 | 4 | 2749 | DE 41, PL 27, ES 5 |
| 1209 | Pavago | workable | https://apply.workable.com/pavago | 10 | 5 | 4 | 827 | PT 4, DE 2, IE 2 |
| 1210 | piano | bamboohr | https://piano.bamboohr.com/careers | 13 | 5 | 4 | 18 | FR 8, PL 3, NL 1 |
| 1211 | PTC | workday | https://ptc.wd1.myworkdayjobs.com/ptc | 24 | 5 | 4 | 167 | ES 14, DE 9, IT 1 |
| 1212 | PUC | cornerstone | https://puc.csod.com/ux/ats/careersite/1/home?c=puc | 317 | 5 | 4 | 347 | DE 256, PL 19, AT 16 |
| 1213 | Rituals | smartrecruiters | https://careers.smartrecruiters.com/Rituals1 | 588 | 5 | 4 | 615 | FR 246, NL 83, ES 64 |
| 1214 | Sephora | successfactors | https://jobs.sephora.com | 412 | 5 | 4 | 2105 | IT 164, FR 160, DE 34 |
| 1215 | SICK | successfactors | https://jobs.sick.com | 6 | 5 | 4 | 80 | DE 6 |
| 1216 | Simvia | recruitee | https://simvia.recruitee.com | 10 | 5 | 4 | 10 | NL 10 |
| 1217 | Stadt Zürich | successfactors | https://jobs.stadt-zuerich.ch | 566 | 5 | 4 | 566 | CH 566 |
| 1218 | U.S. Bank | workday | https://usbank.wd1.myworkdayjobs.com/us_bank_careers | 35 | 5 | 4 | 1337 | PL 21, IE 12, LU 2 |
| 1219 | velio | personio | https://velio.jobs.personio.com | 8 | 5 | 4 | 10 | DE 8 |
| 1220 | Visa | workday | https://visa.wd5.myworkdayjobs.com/visa | 50 | 5 | 4 | 762 | PL 18, DE 13, ES 4 |
| 1221 | Vitesco Technologies GmbH | successfactors | https://jobs.vitesco-technologies.com | 26 | 5 | 4 | 181 | CZ 15, DE 6, FR 5 |
| 1222 | VML Enterprise Solutions | greenhouse | https://job-boards.greenhouse.io/vmlenterprisesolutions | 35 | 5 | 4 | 158 | PL 13, PT 12, FR 9 |
| 1223 | Volkswagen AG | successfactors | https://jobs.scania.com | 239 | 5 | 4 | 1823 | DE 196, CZ 11, DK 10 |
| 1224 | ANGEHEUERT GmbH Personalberatung | join_com | https://join.com/companies/angeheuert | 39 | 5 | 3 | 39 | AT 23, DE 16 |
| 1225 | BearingPoint Austria | teamtailor | https://bearingpointgmbh.teamtailor.com | 12 | 5 | 3 | 12 | AT 12 |
| 1226 | Boxtop AG | join_com | https://join.com/companies/boxtop | 10 | 5 | 3 | 10 | CH 10 |
| 1227 | BRISA - AUTO ESTRADAS DE | successfactors | https://recrutamento.brisa.pt/A-to-Be | 31 | 5 | 3 | 31 | PT 31 |
| 1228 | Budget Thuis | recruitee | https://werkenbijbudgetthuis.nl | 11 | 5 | 3 | 11 | NL 11 |
| 1229 | byte-code spa | recruitee | https://bytecode.recruitee.com | 5 | 5 | 3 | 7 | IT 4, CH 1 |
| 1230 | ComeOn Group | workable | https://apply.workable.com/comeon-group | 7 | 5 | 3 | 31 | SE 3, PL 3, AT 1 |
| 1231 | Company Background Planet | workday | https://planet.wd3.myworkdayjobs.com/planet | 44 | 5 | 3 | 77 | PL 21, ES 9, PT 4 |
| 1232 | Decathlon Digital EN | greenhouse | https://job-boards.greenhouse.io/decathlontechnologyen | 13 | 5 | 3 | 13 | FR 13 |
| 1233 | EBZ Group | successfactors | https://careers.ebz-group.com | 33 | 5 | 3 | 33 | DE 33 |
| 1234 | Elatec GmbH | smartrecruiters | https://careers.smartrecruiters.com/ElatecGmbH | 10 | 5 | 3 | 10 | DE 10 |
| 1235 | eleQtron GmbH | recruitee | https://eleqtron.recruitee.com | 8 | 5 | 3 | 8 | DE 8 |
| 1236 | EnduroSat | jazzhr | https://endurosat.applytojob.com | 7 | 5 | 3 | 55 | IT 4, DE 3 |
| 1237 | Entrust | workday | https://entrust.wd1.myworkdayjobs.com/entrustcareers | 8 | 5 | 3 | 71 | PT 3, NL 2, ES 2 |
| 1238 | Flower | teamtailor | https://flower.teamtailor.com | 9 | 5 | 3 | 9 | SE 9 |
| 1239 | Flywire | smartrecruiters | https://careers.smartrecruiters.com/flywire1 | 6 | 5 | 3 | 61 | ES 6 |
| 1240 | FormFactor | workday | https://formfactor.wd1.myworkdayjobs.com/ffi-careers | 9 | 5 | 3 | 185 | DE 9 |
| 1241 | FutureWhiz | recruitee | https://futurewhiz.recruitee.com | 7 | 5 | 3 | 8 | NL 7 |
| 1242 | ITM Power | join_com | https://join.com/companies/itm-power | 12 | 5 | 3 | 12 | DE 12 |
| 1243 | LEONI | smartrecruiters | https://careers.smartrecruiters.com/LEONI1 | 26 | 5 | 3 | 293 | DE 24, CZ 1, FR 1 |
| 1244 | Merz Pharma GmbH Co. KGaA | softgarden | https://merz-pharma.career.softgarden.de/ | 24 | 5 | 3 | 24 | DE 24 |
| 1245 | Miebach Consulting GmbH | recruitee | https://miebachconsulting.recruitee.com | 11 | 5 | 3 | 11 | DE 10, BE 1 |
| 1246 | NECT GmbH | softgarden | https://nect.career.softgarden.de/ | 14 | 5 | 3 | 14 | DE 14 |
| 1247 | Olo | lever | https://jobs.lever.co/olo | 5 | 5 | 3 | 7 | IE 5 |
| 1248 | polishcareers-pepsico | icims | https://polishcareers-pepsico.icims.com | 11 | 5 | 3 | 11 | PL 11 |
| 1249 | FourByte GmbH | join_com | https://join.com/companies/profitpath | 9 | 5 | 3 | 9 | DE 9 |
| 1250 | Qutwo | teamtailor | https://qutwo.teamtailor.com | 11 | 5 | 3 | 11 | FI 11 |
| 1251 | suitsupply | greenhouse | https://job-boards.greenhouse.io/suitsupply | 57 | 5 | 3 | 170 | NL 30, DE 13, BE 4 |
| 1252 | think-cell | greenhouse | https://job-boards.greenhouse.io/think-cell | 14 | 5 | 3 | 36 | DE 12, NL 1, PL 1 |
| 1253 | Verkor | lever | https://jobs.lever.co/verkor | 30 | 5 | 3 | 48 | FR 30 |
| 1254 | ABRIO GmbH | recruitee | https://abriogmbh.recruitee.com | 20 | 5 | 2 | 20 | DE 20 |
| 1255 | Aveniq AG | recruitee | https://aveniq.recruitee.com | 13 | 5 | 2 | 13 | CH 13 |
| 1256 | Breitling | successfactors | https://careers.breitling.com | 21 | 5 | 2 | 50 | NL 6, DE 6, PL 4 |
| 1257 | Codest Ltd. Company No. 12590542, VAT number: GB363431020 | recruitee | https://thecodest.recruitee.com | 5 | 5 | 2 | 19 | PL 5 |
| 1258 | emea-cookmedical | icims | https://emea-cookmedical.icims.com | 21 | 5 | 2 | 25 | IE 12, DK 5, AT 1 |
| 1259 | Fastned | recruitee | https://fastned.recruitee.com | 24 | 5 | 2 | 26 | NL 12, DK 5, FR 3 |
| 1260 | Genesys | workday | https://genesys.wd1.myworkdayjobs.com/genesys | 23 | 5 | 2 | 187 | IE 7, FR 4, SE 3 |
| 1261 | Groupe Bel | successfactors | https://jobs.groupe-bel.com | 66 | 5 | 2 | 282 | FR 64, BE 1, DE 1 |
| 1262 | Innovamat | teamtailor | https://innovamat.teamtailor.com | 21 | 5 | 2 | 47 | ES 20, IT 1 |
| 1263 | Kolecto | teamtailor | https://kolecto-1739351560.teamtailor.com | 11 | 5 | 2 | 11 | FR 11 |
| 1264 | Loka, Inc | greenhouse | https://job-boards.greenhouse.io/lokainc | 6 | 5 | 2 | 11 | PT 6 |
| 1265 | NTT DATA, Inc. | successfactors | https://careers-inc.nttdata.com | 23 | 5 | 2 | 1464 | IE 19, NL 2, DE 1 |
| 1266 | Online Payment Platform | recruitee | https://jobs.onlinepaymentplatform.com | 7 | 5 | 2 | 7 | NL 7 |
| 1267 | Pennylane SAS | ashby | https://jobs.ashbyhq.com/pennylane | 37 | 5 | 2 | 141 | FR 23, DE 14 |
| 1268 | Pvh | workday | https://pvh.wd1.myworkdayjobs.com/pvh_careers | 25 | 5 | 2 | 1538 | NL 22, DE 2, LU 1 |
| 1269 | SBK | cornerstone | https://sbk.csod.com/ux/ats/careersite/17/home?c=sbk | 13 | 5 | 2 | 13 | DE 13 |
| 1270 | Soda Data | workable | https://apply.workable.com/soda-data-nv | 5 | 5 | 2 | 11 | DE 5 |
| 1271 | Solvinity | recruitee | https://solvinity.recruitee.com | 8 | 5 | 2 | 8 | NL 8 |
| 1272 | Sophos | lever | https://jobs.lever.co/sophos | 13 | 5 | 2 | 107 | DE 8, IT 2, FR 2 |
| 1273 | SwingDev—a hippo company | lever | https://jobs.lever.co/swingdev | 8 | 5 | 2 | 9 | PL 8 |
| 1274 | Timestamp | teamtailor | https://timestamp.teamtailor.com | 18 | 5 | 2 | 19 | PT 18 |
| 1275 | Volksbank Wien | softgarden | https://aerzte-apotheker-volksbank-at.career.softgarden.de/ | 23 | 5 | 2 | 23 | AT 23 |
| 1276 | Ørsted Services A/S | successfactors | https://applyforjob.orsted.com | 22 | 5 | 2 | 39 | DK 12, PL 8, DE 2 |
| 1277 | Algolia | greenhouse | https://job-boards.greenhouse.io/algolia | 10 | 5 | 1 | 32 | FR 8, DE 2 |
| 1278 | Anthesis Group | pinpoint | https://anthesisgroup.pinpointhq.com | 28 | 5 | 1 | 77 | ES 14, NL 5, DE 4 |
| 1279 | Auterion | greenhouse | https://job-boards.greenhouse.io/auterion | 11 | 5 | 1 | 19 | DE 8, CH 3 |
| 1280 | Cint | smartrecruiters | https://careers.smartrecruiters.com/cint | 8 | 5 | 1 | 19 | ES 5, CZ 2, DE 1 |
| 1281 | CLEVR | greenhouse | https://job-boards.greenhouse.io/clevr | 11 | 5 | 1 | 14 | NL 6, NO 3, DE 1 |
| 1282 | easybill GmbH | join_com | https://join.com/companies/easybill | 5 | 5 | 1 | 5 | DE 5 |
| 1283 | Evidentiq | personio | https://evidentiq.jobs.personio.com | 6 | 5 | 1 | 6 | DE 6 |
| 1284 | Filigran | ashby | https://jobs.ashbyhq.com/filigran | 9 | 5 | 1 | 17 | FR 8, DE 1 |
| 1285 | Hyland | icims | https://careers-hyland.icims.com | 9 | 5 | 1 | 66 | PL 7, IT 1, DE 1 |
| 1286 | iits | personio | https://iits.jobs.personio.com | 6 | 5 | 1 | 6 | DE 6 |
| 1287 | INDIE Solutions GmbH | join_com | https://join.com/companies/indie-solutions | 11 | 5 | 1 | 11 | DE 11 |
| 1288 | Interstellar Lab | greenhouse | https://job-boards.greenhouse.io/interstellarlab | 7 | 5 | 1 | 9 | FR 7 |
| 1289 | INVENSENSE, INC. | taleo | https://phh.tbe.taleo.net/phh04/ats/careers/v2/searchResults?org=INVENSENSE&cws=1 | 5 | 5 | 1 | 10 | FR 4, IT 1 |
| 1290 | Kraken | ashby | https://jobs.ashbyhq.com/kraken.com | 8 | 5 | 1 | 72 | PL 5, IE 3 |
| 1291 | Labelium | smartrecruiters | https://careers.smartrecruiters.com/Labelium | 24 | 5 | 1 | 24 | FR 24 |
| 1292 | Linkup | ashby | https://jobs.ashbyhq.com/Linkup | 7 | 5 | 1 | 10 | FR 7 |
| 1293 | Matera | recruitee | https://matera.recruitee.com | 23 | 5 | 1 | 26 | FR 15, DE 8 |
| 1294 | MAZARS | smartrecruiters | https://careers.smartrecruiters.com/MAZARS | 180 | 5 | 1 | 193 | FR 180 |
| 1295 | Nitor | workable | https://apply.workable.com/nitor | 10 | 5 | 1 | 11 | FI 10 |
| 1296 | QualityMinds GmbH | join_com | https://join.com/companies/qualityminds | 6 | 5 | 1 | 6 | DE 6 |
| 1297 | Resmed | workday | https://resmed.wd3.myworkdayjobs.com/resmed_external_careers | 18 | 5 | 1 | 232 | DE 10, FR 5, IE 3 |
| 1298 | Sprinklr | workday | https://sprinklr.wd1.myworkdayjobs.com/careers | 11 | 5 | 1 | 91 | IE 5, IT 2, FR 1 |
| 1299 | Techland S.A. | smartrecruiters | https://careers.smartrecruiters.com/TechlandSA | 30 | 5 | 1 | 30 | PL 30 |
| 1300 | tripactions | greenhouse | https://job-boards.greenhouse.io/tripactions | 36 | 5 | 1 | 202 | DE 19, PT 10, FR 5 |
| 1301 | airSlate | lever | https://jobs.lever.co/airslate | 10 | 5 | 0 | 14 | PL 8, ES 1, CZ 1 |
| 1302 | Believe | smartrecruiters | https://careers.smartrecruiters.com/Believe | 7 | 5 | 0 | 18 | FR 5, LU 1, DE 1 |
| 1303 | CesiumAstro | lever | https://jobs.lever.co/CesiumAstro | 12 | 5 | 0 | 263 | DE 12 |
| 1304 | Flora Food Group | greenhouse | https://job-boards.greenhouse.io/florafoodgroup | 14 | 5 | 0 | 24 | DE 7, PL 4, PT 2 |
| 1305 | Fusion Consulting | smartrecruiters | https://careers.smartrecruiters.com/fusionconsulting | 56 | 5 | 0 | 87 | DE 25, ES 15, PT 8 |
| 1306 | Keyfactor, Inc. | greenhouse | https://job-boards.greenhouse.io/keyfactorinc | 9 | 5 | 0 | 26 | SE 4, ES 4, NL 1 |
| 1307 | nitrosoftwareinc | greenhouse | https://job-boards.greenhouse.io/nitrosoftwareinc | 13 | 5 | 0 | 17 | IE 8, PT 3, BE 2 |
| 1308 | Sedgman | oracle | https://elgl.fa.ap1.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2004 | 5 | 5 | 0 | 60 | DE 5 |
| 1309 | Socialpoint | greenhouse | https://job-boards.greenhouse.io/spcareers | 9 | 5 | 0 | 9 | ES 9 |
| 1310 | Sysdig | lever | https://jobs.lever.co/sysdig | 7 | 5 | 0 | 14 | IT 4, ES 3 |
| 1311 | Verve | greenhouse | https://job-boards.greenhouse.io/verve | 16 | 5 | 0 | 62 | IE 8, DE 7, ES 1 |
| 1312 | Workato | greenhouse | https://job-boards.greenhouse.io/workato | 12 | 5 | 0 | 101 | DE 5, ES 3, NL 2 |
