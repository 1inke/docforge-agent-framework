# Document Engine Assets

## `gb-t-7714-2015-numeric.csl`

- **Source**: Citation Style Language (`citation-style-language/styles`), `china-national-standard-gb-t-7714-2015-numeric.csl`
- **URL**: https://github.com/citation-style-language/styles/blob/master/china-national-standard-gb-t-7714-2015-numeric.csl
- **Authors**: 牛耕田, Zeping Lee
- **License**: Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) — https://creativecommons.org/licenses/by-sa/3.0/

## `crossref.lua`

- Two-pass Pandoc Lua filter (licensed with DAF):
  - Pass 1 registers `Figure`, `Table` (including `Div` wrapper `{#tbl:...}`), `Span` (`{#eq:...}`), and `Header` (`{#sec:...}`) labels and attaches numbered prefixes.
  - Pass 2 removes `[@hypothesis]` markers, resolves cross-references (`@fig:*`, `@tbl:*`, `@eq:*`, `@sec:*`), and rewrites evidence citations (`@ev_*`) to deduplicated `source_id` references via `daf-evidence-map` metadata before `--citeproc`.
