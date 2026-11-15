# Upstream sources and attribution

Windy is built on top of the following open-source projects (all MIT-licensed):

| Project | Used for | Upstream commit (cloned 2026-11-15) |
| --- | --- | --- |
| [Flowm/meteocompare](https://github.com/Flowm/meteocompare) | Vue 3 + TypeScript frontend basis (Open-Meteo multi-model client, model registry, hourly charts, Single Runs adapter) | `9d6bb7bf7ad33c416229d8afc02447d43b540250` |
| [Devs-Hunting/enercast](https://github.com/Devs-Hunting/enercast) | Selective reference for backend data-sync / actual-vs-predicted patterns | `695e24835208430314f24af6bae45c6d590bc534` |
| [wind-python/windpowerlib](https://github.com/wind-python/windpowerlib) | Power-curve / turbine modelling library (used as a dependency where it adds value) | `191d8ca894d6d830997630fa3599f048d6e8c7b4` |

Where upstream code is copied or adapted, the original MIT license notices are
preserved. See the feature-branch README for a detailed reuse inventory.
