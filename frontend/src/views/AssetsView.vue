<script setup lang="ts">
import { computed, reactive, ref } from "vue";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import StateBlock from "@/components/StateBlock.vue";
import { windyApi, type AssetCreate, type AssetSummary, type ConfigRevisionsResponse, type LocationsResponse, type PowerCurveSummary } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";

// Sites — list and create. Configuration that shapes derived MWh (hub height,
// capacity, turbines, curve, losses) is fixed at creation for this phase: there
// is deliberately no edit path, so archived forecast snapshots can never be
// silently relabelled under changed settings.

const res = useAbortableResource<{ assets: AssetSummary[] }>((signal) => windyApi.assets(signal), () => []);
const { data: assetsData, loading, error, refresh } = res;
const assets = computed(() => assetsData.value?.assets ?? []);
const curvesRes = useAbortableResource<{ curves: PowerCurveSummary[] }>((signal) => windyApi.powerCurves(signal), () => []);
const curves = computed(() => curvesRes.data.value?.curves ?? []);

const busy = ref(false);
const createError = ref<string | null>(null);
const createdNote = ref<string | null>(null);
const form = reactive<AssetCreate>({
  id: undefined,
  label: "",
  country: "",
  latitude: 45.0,
  longitude: 16.0,
  capacity_mw: 10,
  turbine_count: 5,
  turbine_rated_mw: 2,
  hub_height_m: 90,
  loss_factor: 0,
  timezone: "Europe/Podgorica",
  power_curve_id: "",
  notes: "",
});

async function create(): Promise<void> {
  busy.value = true;
  createError.value = null;
  createdNote.value = null;
  try {
    const site = await windyApi.assetCreate({ ...form });
    createdNote.value = `created site "${site.id}" — configuration is fixed for this phase; the selected curve is ${
      curves.value.find((c) => c.id === site.power_curve_id)?.site_specific ? "site-specific" : "a GENERIC reference curve (modelled MWh are unverified)"
    }`;
    form.label = "";
    form.country = "";
    form.id = undefined;
    cloneNote.value = null;
    await refresh();
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

// Addendum A — non-destructive configuration versioning: a new site id is
// created from the current one's values; the original site and ALL its
// archives stay untouched (never an in-place rewrite).
const cloneNote = ref<string | null>(null);

function startClone(a: AssetSummary): void {
  cloneNote.value = `Obrazac „Add a site" ispod je ispunjen iz „${a.id}" — provjeri vrijednosti, unesi NOVI id i label, pa spremi. Original „${a.id}" i svi njegovi arhivirani podaci ostaju nepromijenjeni (ne-destruktivna nova konfiguracija).`;
  form.id = `${a.id}-v2`;
  form.label = `${a.name} (nova konfiguracija)`;
  form.country = a.country ?? "";
  form.latitude = a.latitude;
  form.longitude = a.longitude;
  form.capacity_mw = a.capacity_mw;
  form.turbine_count = a.turbine_count;
  form.turbine_rated_mw = a.turbine_rated_mw;
  form.hub_height_m = a.hub_height_m;
  form.loss_factor = a.loss_factor;
  form.timezone = a.timezone;
  form.power_curve_id = a.power_curve_id ?? "";
  form.notes = `Izvedeno iz sitea "${a.id}" (ne-destruktivna revizija konfiguracije; original ostaje arhiviran).`;
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

// Correction A §6/§2.1 — SAME-SITE versioned configuration edit: changes go
// through append-only config revisions (old values recorded, effective-from
// stamped); archived runs are never modified. The edit form below edits the
// EXISTING site id — no clone needed for a genuine configuration update.
const editSiteId = ref<string | null>(null);
const editForm = reactive({
  capacity_mw: 0,
  turbine_count: 0,
  turbine_rated_mw: 0,
  hub_height_m: 90,
  loss_factor: 0,
  timezone: "Europe/Podgorica",
  allow_scaled_reference: false,
  regenerate_inventory: false,
  note: "",
});
const editBusy = ref(false);
const editError = ref<string | null>(null);
const editMessage = ref<string | null>(null);

const editRevisionsResource = useAbortableResource<ConfigRevisionsResponse | null>(
  async () => {
    if (!editSiteId.value) return null;
    return windyApi.configRevisions(editSiteId.value);
  },
  () => [editSiteId.value],
);
const editRevisions = computed(() => editRevisionsResource.data.value?.revisions ?? []);

const editLocations = useAbortableResource<LocationsResponse | null>(
  async () => {
    if (!editSiteId.value) return null;
    return windyApi.locations(editSiteId.value);
  },
  () => [editSiteId.value],
);

function startEdit(a: AssetSummary): void {
  editSiteId.value = a.id;
  editForm.capacity_mw = a.capacity_mw;
  editForm.turbine_count = a.turbine_count;
  editForm.turbine_rated_mw = a.turbine_rated_mw;
  editForm.hub_height_m = a.hub_height_m;
  editForm.loss_factor = a.loss_factor;
  editForm.timezone = a.timezone;
  // no silent default opt-in: the checkbox reflects the site's ACTUAL value
  editForm.allow_scaled_reference = !!(a as { allow_scaled_reference?: number | boolean }).allow_scaled_reference;
  editForm.regenerate_inventory = false;
  editForm.note = "";
  editError.value = null;
  editMessage.value = null;
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

async function saveEdit(): Promise<void> {
  if (!editSiteId.value) return;
  const target = editSiteId.value;
  editBusy.value = true;
  editError.value = null;
  editMessage.value = null;
  try {
    // Correction C §1: send ONLY actually-changed fields (compared against
    // the current asset) — an unchanged form must not trip the backend's
    // fleet-change guard or create an empty revision
    const current = assets.value.find((a) => a.id === target);
    const candidates: Record<string, unknown> = {
      capacity_mw: editForm.capacity_mw,
      turbine_count: editForm.turbine_count,
      turbine_rated_mw: editForm.turbine_rated_mw,
      hub_height_m: editForm.hub_height_m,
      loss_factor: editForm.loss_factor,
      timezone: editForm.timezone,
      allow_scaled_reference: editForm.allow_scaled_reference,
    };
    const changes: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(candidates)) {
      const cur = (current as unknown as Record<string, unknown>)?.[key];
      const bothNum = typeof value === "number" && typeof cur === "number";
      const differs = bothNum ? Math.abs(value - (cur as number)) > 1e-9 : value !== cur;
      if (differs) changes[key] = value;
    }
    if (editForm.regenerate_inventory) changes.regenerate_inventory = true;
    if (Object.keys(changes).length === 0) {
      editMessage.value = "Nema stvarne promjene vrijednosti — revizija nije kreirana.";
      return;
    }
    const res = await windyApi.configRevisionApply(target, { changes, note: editForm.note || undefined });
    editMessage.value =
      `Revizija ${res.revision} spremljena (na snazi od ${res.effective_from_utc}). Arhivirani run-ovi nisu mijenjani.` +
      (res.inventory_revision ? ` Inventar regeneriran (revizija ${res.inventory_revision}).` : "");
    await Promise.all([refresh(), editRevisionsResource.refresh(), editLocations.refresh()]);
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e);
  } finally {
    editBusy.value = false;
  }
}
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-6 sm:px-6 sm:py-6">
      <h1 class="text-paper-50 font-mono text-lg tracking-wide">Sites</h1>
      <p class="text-paper-400 font-mono text-[10px] leading-relaxed">
        Each site keeps its own forecasts, actuals, accuracy and backfill jobs — nothing is shared across sites. All data on the
        other screens belongs to the site chosen in the selector.
      </p>

      <CollapsibleSection title="Registered sites">
        <StateBlock v-if="error" kind="error">{{ error }}</StateBlock>
        <StateBlock v-else-if="loading" kind="loading" loading-py="py-8" caption="Loading…" />
        <div v-else-if="assets.length" class="overflow-x-auto">
          <table class="w-full font-mono text-[10px]">
            <thead>
              <tr class="text-paper-400 border-ink-700 border-b text-left">
                <th class="py-1.5 pr-3">id</th>
                <th class="py-1.5 pr-3">label</th>
                <th class="py-1.5 pr-3">lat / lon</th>
                <th class="py-1.5 pr-3">capacity</th>
                <th class="py-1.5 pr-3">turbines</th>
                <th class="py-1.5 pr-3">hub</th>
                <th class="py-1.5 pr-3">tz</th>
                <th class="py-1.5 pr-3">curve</th>
                <th class="py-1.5 pr-3">konfiguracija</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in assets" :key="a.id" class="border-ink-800 border-b">
                <td class="text-paper-100 py-1.5 pr-3">{{ a.id }}</td>
                <td class="py-1.5 pr-3">{{ a.name }}</td>
                <td class="text-paper-300 py-1.5 pr-3">{{ a.latitude }} / {{ a.longitude }}</td>
                <td class="text-paper-300 py-1.5 pr-3">{{ a.capacity_mw }} MW</td>
                <td class="text-paper-300 py-1.5 pr-3">{{ a.turbine_count }} × {{ a.turbine_rated_mw }}</td>
                <td class="text-paper-300 py-1.5 pr-3">{{ a.hub_height_m }} m</td>
                <td class="text-paper-300 py-1.5 pr-3">{{ a.timezone }}</td>
                <td class="py-1.5 pr-3">
                  <span
                    class="border px-1.5 py-0.5"
                    :class="a.curve_site_specific ? 'border-sodium-300/40 text-sodium-200' : 'border-heat-500/40 text-heat-300'"
                  >{{ a.curve_label ?? "—" }}{{ a.curve_reference_only ? " · reference/unverified" : "" }}</span>
                </td>
                <td class="space-x-1 py-1.5 pr-3 whitespace-nowrap">
                  <button
                    type="button"
                    class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 border px-2 py-0.5 font-mono text-[10px]"
                    :title="'Uredi konfiguraciju ISTOG sitea — append-only revizija, arhiva netaknuta'"
                    @click="startEdit(a)"
                  >Uredi (revizija)…</button>
                  <button
                    type="button"
                    class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]"
                    :title="'Nova konfiguracija iz ove elektrane — novi site id, original i arhiva ostaju netaknuti'"
                    @click="startClone(a)"
                  >Kloniraj…</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <StateBlock v-else kind="empty">No sites.</StateBlock>
      </CollapsibleSection>

      <CollapsibleSection title="Uređivanje konfiguracije postojećeg sitea (append-only revizije)" :default-open="false" lazy>
        <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
          Izmjena ide na ISTI site id kroz auditable revizije: stare vrijednosti se bilježe, mijenja se samo konfiguracija za
          buduće proračune; arhivirani run-ovi i stvarni podaci ostaju netaknuti. Restart ne poništava revizije i ne reseeda
          konfiguraciju. Za potpuno novi site koristite „Kloniraj…" u tablici.
        </p>
        <StateBlock v-if="!editSiteId" kind="empty" :relaxed="true">
          Odaberite „Uredi (revizija)…" uz site u tablici iznad.
        </StateBlock>
        <template v-else>
          <p class="text-sodium-200 font-mono text-[11px]">Site: {{ editSiteId }}</p>
          <div class="mt-2 grid gap-2 sm:grid-cols-2">
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">capacity MW</span>
              <input v-model.number="editForm.capacity_mw" type="number" step="0.1" min="0.1" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">turbine count</span>
              <input v-model.number="editForm.turbine_count" type="number" min="1" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">turbine rating MW</span>
              <input v-model.number="editForm.turbine_rated_mw" type="number" step="0.05" min="0.05" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">hub height m</span>
              <input v-model.number="editForm.hub_height_m" type="number" step="1" min="10" max="300" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">loss factor (0–0.999)</span>
              <input v-model.number="editForm.loss_factor" type="number" step="0.01" min="0" max="0.999" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">timezone (IANA)</span>
              <input v-model="editForm.timezone" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px] flex items-center gap-2">
              <input v-model="editForm.allow_scaled_reference" type="checkbox" class="accent-sodium-300" />
              <span class="text-paper-300">allow scaled reference (REFERENCE/UNVERIFIED opt-in)</span>
            </label>
            <label class="font-mono text-[10px] flex items-start gap-2">
              <input v-model="editForm.regenerate_inventory" type="checkbox" class="accent-sodium-300 mt-0.5" />
              <span class="text-paper-300">regeneriraj inventar turbina — NOVA inventarna revizija WTG-001..count @ novi rating; za fizicku promjenu flote obavezno; installed mora odgovarati capacity_mw; aktivne restrikcije na preživjelim jedinicama ostaju (provjeri % / MW limite protiv nove nazivne)</span>
            </label>
            <label class="font-mono text-[10px] sm:col-span-2">
              <span class="text-paper-400 block">napomena (audit trail)</span>
              <input v-model="editForm.note" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
            </label>
          </div>
          <div class="mt-3 flex items-center gap-3">
            <button
              type="button"
              class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px]"
              :disabled="editBusy"
              @click="saveEdit"
            >{{ editBusy ? "Spremam…" : "Spremi reviziju" }}</button>
          </div>
          <p v-if="editMessage" class="text-sodium-200 mt-2 font-mono text-[10px]">{{ editMessage }}</p>
          <p v-if="editError" class="text-heat-300 mt-2 font-mono text-[10px]">{{ editError }}</p>

          <h3 class="text-paper-100 mt-4 font-mono text-[11px]">Revizije konfiguracije ({{ editRevisions.length }})</h3>
          <div v-for="r in editRevisions" :key="r.revision" class="border-ink-700 mt-1 border px-2 py-1 font-mono text-[10px]">
            <span class="text-sodium-200">revizija {{ r.revision }}</span>
            <span class="text-paper-500"> · na snazi {{ r.effective_from_utc }}{{ r.note ? ` · ${r.note}` : "" }}</span>
            <div class="text-paper-300">
              <span v-for="(ch, field) in r.changes" :key="field" class="mr-3">{{ field }}: {{ ch.from }} → {{ ch.to }}</span>
            </div>
          </div>

          <h3 class="text-paper-100 mt-4 font-mono text-[11px]">Lokacije turbina (GPS/visina)</h3>
          <p class="text-paper-400 font-mono text-[10px]">{{ editLocations.data.value?.note ?? "" }}</p>
          <div v-if="editLocations.data.value && editLocations.data.value.n_locations > 0" class="mt-1 overflow-x-auto">
            <table class="w-full max-w-lg font-mono text-[10px]">
              <thead>
                <tr class="text-paper-400 border-ink-700 border-b text-left">
                  <th class="py-1 pr-3">unit</th>
                  <th class="py-1 pr-3">lat</th>
                  <th class="py-1 pr-3">lon</th>
                  <th class="py-1 pr-3">n/v (m)</th>
                  <th class="py-1 pr-3">izvor</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="loc in editLocations.data.value.locations" :key="loc.unit_id" class="border-ink-800 border-b">
                  <td class="text-paper-100 py-1 pr-3">{{ loc.unit_id }}</td>
                  <td class="text-paper-300 py-1 pr-3">{{ loc.latitude }}</td>
                  <td class="text-paper-300 py-1 pr-3">{{ loc.longitude }}</td>
                  <td class="text-paper-300 py-1 pr-3">{{ loc.elevation_m ?? "—" }}</td>
                  <td class="text-paper-400 py-1 pr-3">{{ loc.source }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </CollapsibleSection>

      <CollapsibleSection title="Add a site" :default-open="false">
        <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
          The power curve must be chosen explicitly — a generic curve is prominently labelled reference/unverified and its
          modelled MWh are never presented as calibrated production. Configuration is fixed after creation: to change it,
          use „Nova konfiguracija…" on a registered site — that creates a NEW site id (ne-destruktivna revizija), never an
          in-place rewrite of the original and its archives.
        </p>
        <p v-if="cloneNote" class="text-sodium-200 mb-2 font-mono text-[10px]">{{ cloneNote }}</p>
        <div class="grid gap-2 sm:grid-cols-2">
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">id (opciono; auto ako prazno)</span>
            <input v-model="form.id" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">label *</span>
            <input v-model="form.label" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">country</span>
            <input v-model="form.country" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">latitude * (−90…90)</span>
            <input v-model.number="form.latitude" type="number" step="0.0001" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">longitude * (−180…180)</span>
            <input v-model.number="form.longitude" type="number" step="0.0001" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">capacity MW *</span>
            <input v-model.number="form.capacity_mw" type="number" step="0.1" min="0.1" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">turbine count *</span>
            <input v-model.number="form.turbine_count" type="number" min="1" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">turbine rating MW *</span>
            <input v-model.number="form.turbine_rated_mw" type="number" step="0.1" min="0.1" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">hub height m * (10–300)</span>
            <input v-model.number="form.hub_height_m" type="number" step="1" min="10" max="300" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">loss factor (0–0.999)</span>
            <input v-model.number="form.loss_factor" type="number" step="0.01" min="0" max="0.999" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px]">
            <span class="text-paper-400 block">timezone (IANA) *</span>
            <input v-model="form.timezone" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
          </label>
          <label class="font-mono text-[10px] sm:col-span-2">
            <span class="text-paper-400 block">power curve * — explicit choice; generic curves are marked reference/unverified</span>
            <select v-model="form.power_curve_id" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]">
              <option value="" disabled>select a curve…</option>
              <option v-for="c in curves" :key="c.id" :value="c.id">
                {{ c.label }}{{ c.site_specific ? " (site-specific)" : " (generic — reference/unverified)" }}
              </option>
            </select>
          </label>
        </div>
        <div class="mt-3 flex items-center gap-3">
          <button
            type="button"
            class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px] transition-colors"
            :disabled="busy || !form.label || !form.power_curve_id"
            @click="create"
          >
            {{ busy ? "Creating…" : "Create site" }}
          </button>
        </div>
        <p v-if="createdNote" class="text-sodium-200 mt-2 font-mono text-[10px]">{{ createdNote }}</p>
        <p v-if="createError" class="text-heat-300 mt-2 font-mono text-[10px]">{{ createError }}</p>
      </CollapsibleSection>
    </main>
    <AppFooter />
  </div>
</template>
