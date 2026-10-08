<script setup>
import { computed, onMounted } from "vue";
import { useRoute } from "vue-router";
import { lang, setLang, T } from "./lib/i18n.js";
import { live, startLive } from "./lib/live.js";

const route = useRoute();
// The officer view is bilingual; the operator and research pages are in English.
const sub = computed(() => (route.meta.sub === "tagline" ? T("tagline") : route.meta.sub));
const liveText = computed(() => (live.disabled ? "Static" : live.connected ? T("live_on") : T("live_off")));
const query = computed(() => (route.query.live === "0" ? { live: "0" } : {}));

onMounted(() => {
  document.documentElement.lang = lang.value;
  startLive();
});
</script>

<template>
  <header>
    <div>
      <h1>BlastWatch</h1>
      <p class="sub">{{ sub }}</p>
    </div>
    <nav class="top-nav">
      <span class="live-pill" :class="{ on: live.connected }"
            :title="live.connected ? 'Connected: updates appear as they happen' : 'Connection lost; retrying'">
        <i></i><span class="live-text">{{ liveText }}</span>
      </span>
      <RouterLink :to="{ path: '/', query }">{{ T("nav_officer") }}</RouterLink>
      <RouterLink :to="{ path: '/live', query }">{{ T("nav_live") }}</RouterLink>
      <RouterLink :to="{ path: '/confidence', query }">{{ T("nav_confidence") }}</RouterLink>
      <RouterLink :to="{ path: '/research', query }">{{ T("research") }}</RouterLink>
      <div class="lang" role="group" aria-label="Language">
        <button :aria-pressed="lang === 'en'" @click="setLang('en')">English</button>
        <button :aria-pressed="lang === 'ta'" @click="setLang('ta')">தமிழ்</button>
      </div>
    </nav>
  </header>

  <RouterView v-slot="{ Component }">
    <Transition name="view" mode="out-in">
      <component :is="Component" />
    </Transition>
  </RouterView>

  <div id="toasts" role="status" aria-live="polite">
    <div v-for="t in live.toasts" :key="t.id" class="toast" :class="[t.tone, { out: t.out }]">{{ t.text }}</div>
  </div>
</template>
