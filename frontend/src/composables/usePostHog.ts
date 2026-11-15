// Windy ships no third-party analytics. Upstream meteocompare initialised
// PostHog here; the composable is kept as a no-op so the router and any
// leftover imports keep working without the dependency.

const noopClient = {
  capture: () => undefined,
  register: () => undefined,
};

export function usePostHog() {
  return { posthog: noopClient };
}
