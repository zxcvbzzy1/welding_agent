<script setup>
import RobotSimulator from '@/components/RobotSimulator.vue'
</script>
<template>
  <main class="robot-page">
    <header class="robot-heading"><div><span>ROBOT WORKSPACE</span><h1>机械臂 <small>FAIRINO FR5 · V6</small></h1><p>六轴模型与关节姿态预览</p></div><a-tag color="blue">离线交互展示</a-tag></header>
    <RobotSimulator />
  </main>
</template>
<style scoped>
.robot-page { max-width: 1680px; margin: auto; padding: 28px 32px; }
.robot-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.robot-heading span { color: #64748b; font-size: 10px; font-weight: 750; letter-spacing: 2px; }
h1 { font-size: 28px; margin: 4px 0; } small { margin-left: 14px; font-size: 14px; color: #64748b; font-weight: 500; }
p { color: #64748b; margin: 4px 0 20px; font-size: 13px; }
@media(max-width: 900px) { .robot-page { padding: 20px 14px; } small { display: block; margin: 6px 0 0; } }
</style>
