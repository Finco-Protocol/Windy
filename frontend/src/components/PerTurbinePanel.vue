<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { SpatialForecast, SpatialModel } from "@/api/windy";

const props = defineProps<{
  forecast: SpatialForecast;
  unit: "mwh" | "gwh";
  viewMode: "baseline" | "adjusted";
}>();

const selectedId = ref("ecmwf_ifs");
const active = computed<SpatialModel | undefined>(() =>
  props.forecast.models.find((m) => m.model_id === selectedId.value) ??
  props.forecast.models.find((m) => m.status === "OK"),
);
watch(
  () => props.forecast.site_id,
  () => { selectedId.value = "ecmwf_ifs"; },
);
const allDates = computed(() =>
  props.forecast.models.find((m) => m.daily.length > 0)?.daily.map((r) => r.date) ?? [],
);
const usable = computed(() => props.forecast.models.filter((m) => m.status === "OK" && !m.met_only));
const chosenTotal = (m: SpatialModel, date: string): number | null => {
  const day = m.daily.find((r) => r.date === date);
  return props.viewMode === "adjusted" ? (day?.adjusted_mwh ?? null) : (day?.base_mwh ?? null);
};
const chosenUnit = (value: { base_mwh: number | null; adjusted_mwh_pre_export_cap: number | null }): number | null =>
  props.viewMode === "adjusted" ? value.adjusted_mwh_pre_export_cap : value.base_mwh;
const formatEnergy = (mwh: number | null | undefined): string => {
  if (mwh == null || !Number.isFinite(mwh)) return "—";
  return props.unit === "gwh" ? (mwh / 1000).toFixed(4) : mwh.toFixed(1);
};
</script>

<template>
  <section aria-label="Prognoza po turbini" class="space-y-3">
    <div class="border-sodium-400/40 bg-ink-900 rounded border p-3">
      <h2 class="text-paper-50 mb-1 font-mono text-base">Per turbine — 26 pojedinačnih lokacija</h2>
      <p class="text-paper-300 font-mono text-[11px]">
        Prognozirani vjetar na koordinati svake turbine, pojedinačna krivulja snage i trenutačna ON/OFF/cap ograničenja.
        {{ forecast.note }}
      </p>
      <p v-if="forecast.location_info.provisional_mapping" class="text-heat-300 mt-2 font-mono text-[11px]">
        ⚠ WTG mapiranje na 26 koordinata vlasničkog XLSX-a nije neovisno potvrđeno. Rezultati su referentni.
      </p>
      <p v-if="forecast.location_info.virtual_inventory" class="text-heat-300 mt-1 font-mono text-[11px]">
        ⚠ Inventar je virtualan za prikaz; provjerite i učitajte 26 stvarnih turbina prije operativne uporabe.
      </p>
    </div>

    <div v-if="usable.length === 0" class="border-ink-700 text-heat-300 rounded border p-3 font-mono text-sm">
      Nema arhivirane potpune per-turbine prognoze. Pritisnite „Osvježi” za kontrolirani dohvat svih 26 koordinata.
      Podaci iz „1 spot” načina NE koriste se kao zamjena.
    </div>

    <div v-if="allDates.length" class="overflow-x-auto">
      <h3 class="text-paper-100 mb-2 font-mono text-sm">Usporedba ukupne proizvodnje po modelima ({{ unit.toUpperCase() }})</h3>
      <table class="min-w-max w-full font-mono text-[11px]">
        <thead><tr class="border-ink-700 border-b">
          <th class="p-2 text-left">Model</th>
          <th v-for="d in allDates" :key="d" class="p-2 text-right">{{ d }}</th>
          <th class="p-2 text-left">NWP ćelija</th>
        </tr></thead>
        <tbody>
          <tr v-for="model in forecast.models.filter((m) => !m.met_only)" :key="model.model_id" class="border-ink-800 border-b">
            <td class="text-paper-100 p-2">{{ model.label }}</td>
            <td v-for="d in allDates" :key="d" class="text-sodium-200 p-2 text-right"
              :title="model.daily.find((row) => row.date === d)?.partial ? 'Djelomičan dan, ne prikazuje procjenu za sve sate' : ''">
              {{ formatEnergy(chosenTotal(model, d)) }}
              <span v-if="model.daily.find((row) => row.date === d)?.partial" class="text-heat-300">⚠</span>
            </td>
            <td class="text-paper-400 p-2">{{ model.grid_cells ?? "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p class="text-paper-400 mt-1 font-mono text-[10px]">
        Vrijednosti po modelima nisu prosječene niti umjetno popunjene. NWP ćelija ≠ broj turbina.
        ⚠ označava djelomičnu pokrivenost.
      </p>
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <label for="windy-per-turbine-model" class="text-paper-300 font-mono text-[11px]">Model za pregled turbina</label>
      <select id="windy-per-turbine-model" v-model="selectedId"
        class="bg-ink-900 border-ink-600 text-paper-50 rounded border px-3 py-1 font-mono text-xs">
        <option v-for="m in forecast.models.filter((x) => !x.met_only)" :key="m.model_id" :value="m.model_id">
          {{ m.label }} — {{ m.status }}
        </option>
      </select>
    </div>
    <p v-if="active" class="text-paper-400 font-mono text-[11px]">
      {{ active.label }} · run {{ active.issue_utc ?? "nepoznato" }} · {{ active.status }} ·
      {{ active.grid_cells ?? "nepoznato" }} različitih NWP ćelija.
      {{ active.warning }}
    </p>

    <div v-if="active && active.turbines.length" class="overflow-x-auto">
      <h3 class="text-paper-100 mb-2 font-mono text-sm">Proizvodnja i raspoloživost svake turbine ({{ unit.toUpperCase() }})</h3>
      <table class="min-w-max w-full font-mono text-[11px]">
        <thead><tr class="border-ink-700 border-b">
          <th class="p-2 text-left">WTG</th>
          <th v-for="day in active.daily" :key="day.date" class="p-2 text-right">{{ day.date }}</th>
          <th class="p-2 text-right">Visina terena</th>
        </tr></thead>
        <tbody>
          <tr v-for="t in active.turbines" :key="t.unit_id" class="border-ink-800 border-b">
            <td class="text-paper-100 p-2" :title="`${t.latitude}, ${t.longitude}`">{{ t.unit_id }}</td>
            <td v-for="d in t.daily" :key="d.date" class="text-sodium-200 p-2 text-right"
              :title="`Vjetar prosjek ${d.wind_mean_mps ?? '—'} m/s; ${d.hours} modeliranih sati`">
              {{ formatEnergy(chosenUnit(d)) }}
            </td>
            <td class="text-paper-400 p-2 text-right">{{ t.elevation_m == null ? "—" : Math.round(t.elevation_m) + " m" }}</td>
          </tr>
        </tbody>
      </table>
      <p class="text-paper-400 mt-1 font-mono text-[10px]">
        U Prilagođeno: pojedinačni MWh su nakon ON/OFF i MW/% limita, ali prije zajedničkog export capa.
        Zbroj po turbinama zato se može razlikovati od ograničenog ukupnog izlaza elektrane.
      </p>
    </div>
  </section>
</template>
