# Decompozicija razlika MWh između modela — 2026-11-18 (Addendum B §4)

Vlasnički brojevi (dnevni MWh, 18.11.): ECMWF 1230,9553 · ARPEGE 1370,445 · ICON Global 672,7605 · ICON EU 726,7862 · GFS 300,1433.

Faktori (bez tvrdnje koji je „točan"):
1. **different-wind**: svaki model ima vlastito polje vjetra; GFS daje znatno niže brzine tog dana (sintetički dani na dalekom horizontu imaju još niže).
2. **different-time**: runovi su različitih izdanja (ECMWF/GFS/ICON ok; GFS ok-issue-unknown) — isti UTC sat nije isti lead time.
3. **different-coverage**: ARPEGE ~4 dana, ICON EU 5, ICON Global 8, GFS 16 — dnevni zbroji pokrivaju različite podskupove dana; 18.11. svi pokrivaju, ali satno coverage varira.
4. **power-curve**: zajednička REFERENCE krivulja (skalirana ge103-2750) — nelinearnost pojačava razlike brzina (npr. 8→12 m/s je ~dvostruka snaga).
5. **hub metoda**: ECMWF 100 m proxy (+10 m), GFS 80/100 log-interp, ICON 80/120 log-interp, ARPEGE 80/100 log-interp — sustavne pomake ne treba tumačiti kao kvalitetu modela.
6. **data-issue**: GEM je bio missing-height (100 m NULL upstream) do fixa 80/120 — isključen iz te usporedbe.

Preporuka: uspoređivati **common-hour intersection** ( postojeći /api/forecast intersection total) umjesto dnevnih zbroja različitih pokrivenosti; Average stalno označava N contributora.
