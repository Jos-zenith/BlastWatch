<script setup>
// A Chart.js chart that is rebuilt whenever `config` changes. Chart objects stay outside Vue's
// reactivity (markRaw), which would otherwise proxy every data point.
import { markRaw, onBeforeUnmount, onMounted, ref, watch } from "vue";
import Chart from "chart.js/auto";

const props = defineProps({ config: { type: Object, default: null }, height: { type: String, default: "200px" } });
const canvas = ref(null);
let chart = null;

function draw() {
  chart?.destroy();
  chart = null;
  if (props.config && canvas.value) chart = markRaw(new Chart(canvas.value, props.config));
}

onMounted(draw);
watch(() => props.config, draw);
onBeforeUnmount(() => chart?.destroy());
defineExpose({ chart: () => chart });
</script>

<template>
  <div class="chart-box" :style="{ height }"><canvas ref="canvas"></canvas></div>
</template>
