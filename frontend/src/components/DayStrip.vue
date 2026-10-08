<script setup>
import { T } from "../lib/i18n.js";
import { dayName, LEVEL_COLORS } from "../lib/format.js";

defineProps({ days: { type: Array, required: true }, big: Boolean });

// Why a day got its level: the points behind the score, so equal wet hours with different
// levels (a longer unbroken run, rain, cloud) are visible rather than unexplained.
function explain(d) {
  const p = d.points || {};
  const parts = [
    `${T("why_run")(d.longest_wet_run)} +${p.run ?? 0}`,
    `${T("why_hours")(d.leaf_wet_hours)} +${p.hours ?? 0}`,
    `${T("why_rain")(d.rain_mm)} +${p.rain ?? 0}`,
    `${T("why_cloud")(d.mean_cloud_pct ?? "–")} +${p.cloud ?? 0}`,
  ];
  const mult = d.susceptibility != null && d.susceptibility !== 1 ? ` × ${d.susceptibility}` : "";
  const ens = d.p_high != null ? `\n${T("p_high_tip")(Math.round(d.p_high * 100), d.ens_members)}` : "";
  const b = d.blocks;
  const blk = b && b.total ? `\n${T("blocks_tip")(b.High, b.total, b.high_blocks)}` : "";
  return `${dayName(d.date)} · ${T("level_" + d.level)} · ${T("score")} ${Math.round(d.score)}/100 = ${parts.join(", ")}${mult}${ens}${blk}`;
}
</script>

<template>
  <span class="day-strip" :class="{ big }">
    <span v-for="d in days" :key="d.date" class="day" :class="d.horizon" :style="{ '--c': LEVEL_COLORS[d.level] }"
          :title="explain(d)">
      <b>{{ dayName(d.date) }}</b>
      <template v-if="big">
        <i>{{ T("level_" + d.level) }}</i>
        <small>{{ Math.round(d.score) }} · {{ d.longest_wet_run }} h</small>
        <small v-if="d.p_high != null" class="ens">{{ T("p_high")(Math.round(d.p_high * 100)) }}</small>
        <small v-if="d.blocks?.total" class="blk">{{ T("blocks_high")(d.blocks.High, d.blocks.total) }}</small>
      </template>
    </span>
  </span>
</template>
