**Live app: http://149.165.159.230:8000/** (testing window Nov 1–11, 2026; if it is unreachable, file one issue saying so)

**Who is the user?** An analyst in supply chain finance, vendor risk or compliance who joins or deduplicates company data from several sources, where one company appears as "Wal-Mart Stores Inc.", "Walmart" and "沃尔玛". They want each name mapped to one canonical company, with its parent and headquarters, and a clear signal when the tool is unsure.

**Where to go.** App: http://149.165.159.230:8000/ (API docs at `/docs`). README: https://github.com/normangw/entity-resolver#readme

**How to start.** (1) Type a name on the home page, such as "Lyft", "Wal-Mart Stores Inc." or "Shanghai Airlines", and try other regions. (2) Try a typo ("Mircosoft"), a non-English name, and a made-up one. (3) Upload an .xlsx or .csv of company names, pick the column, resolve it, and download the result. Look for wrong matches, a confident answer for something it should not know, and anything confusing.

**What it can and cannot do.** It resolves about 14,000 listed companies and their aliases, and returns "unresolved" instead of guessing. Subsidiaries (such as YouTube) use a Wikidata lookup that can be slow or rate-limited. It cannot resolve brands that are not listed, most typos, or private companies, and the web UI is bare.

**Feedback sought.** Is the unresolved message clear? Would you trust the matches? Is the upload flow easy? Which names should work but do not?
