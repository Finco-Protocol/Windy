# GFS dijagnoza nula: 2026-11-29 do 2026-12-02 (Addendum B §3)

**Verdikt: STVARNE NULE (valjane), NIJE missing→zero bug.**

Dokazi (produkcijska arhiva run 14, fetched 2026-11-17T20:12Z, days=16, read-only):
- 120 točaka u prozoru ≥ 2026-11-28: **wind non-null 120/120**, statusi `{ok: 120}` — nijedan missing-height, nijedan izrezan sat.
- **103/120 sati ima vjetar < 3 m/s** (ispod cut-in referentne krivulje) → modeled_power_mw = 0, energy = 0 — fizički ispravno.
- Uzorak posljednjih sati: 2026-12-02T21:00Z w=1.5233 → 0.0 MW; 22:00Z w=1.7133 → 0.0 MW.
- **Live raw API usporedba (2026-11-18, isti model/endpoint):** max vjetar po danu — 28.11: 17,13; 29.11: 12,07; 30.11: 8,13; **1.12: 2,73; 2.12: 2,61 m/s** (10 sati < 1 m/s 1.12.) — slaže se s arhivom; daleki horizont GFS smoothing daje vrlo niske brzine.

Klasifikacija po danu: 29.11 djelomično nisko (max 12,07 ali večeri nisko); 30.11 nisko; 1.12 i 2.12 potpuno ispod cut-in → 0 MWh dani s pokrivenošću 24/24.

Zaključak: 0 MWh su stvarna modelirana nula s punom pokrivenošću (`hours_counted=24`, `partial=false` u dnevnoj logici) — razlika od missing (—) očuvana u UI i exportu.
