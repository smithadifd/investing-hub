# Stooq daily-history fixtures

These files are synthetic. On 2026-10-08 the daily CSV endpoint
`https://stooq.com/q/d/l/?s=<symbol>&i=d` answered with a browser check
instead of a CSV, so the series were generated in Stooq's column layout
(`Date,Open,High,Low,Close,Volume`, one session per weekday) rather than
recorded from the site.

`spy.us`, `xlk.us`, `xlf.us` and `xle.us` are broad public listings, not
taken from a views file. Each file has 260 sessions ending 2026-10-07, enough
for a 252-session window, and the closes are not constant. Scoring uses the
`Close` column only. Tests inject a fetcher that reads these files and never
call the network.
