# Windy frontend

Vue 3 + TypeScript + Vite application, adapted from
[Flowm/meteocompare](https://github.com/Flowm/meteocompare) (MIT, upstream commit
`9d6bb7bf7ad33c416229d8afc02447d43b540250`). The upstream project's own README and
design notes (ADR documents, domain glossary) live in its repository.

## Commands

```bash
corepack pnpm install     # locked install
corepack pnpm dev         # dev server (proxies /api to the local backend)
corepack pnpm test        # vitest
corepack pnpm type-check  # vue-tsc
corepack pnpm build       # production build + PWA
corepack pnpm lint        # oxlint + oxfmt + vue-tsc
```

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0).
