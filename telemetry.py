"""
telemetry.py - System Resource Telemetry & Stage Timing Monitor

100% local, privacy-safe monitoring for CPU, RAM, and GPU (NVIDIA via nvidia-smi)
along with fine-grained stopwatch tracking for document parsing, OCR, and LLM inference.
"""

import time
import shutil
import subprocess
from typing import Dict, Any, Optional
import psutil


def get_system_metrics() -> Dict[str, Any]:
    """
    Collects instantaneous snapshot of CPU, RAM, and NVIDIA GPU metrics.
    Fast execution (<50ms), fully local, no external network calls.
    """
    # 1. CPU Metrics
    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_cores_logical = psutil.cpu_count(logical=True) or 1
    cpu_cores_physical = psutil.cpu_count(logical=False) or 1

    # 2. RAM Metrics
    vmem = psutil.virtual_memory()
    ram_total_gb = round(vmem.total / (1024 ** 3), 2)
    ram_used_gb = round(vmem.used / (1024 ** 3), 2)
    ram_available_gb = round(vmem.available / (1024 ** 3), 2)
    ram_percent = vmem.percent

    # 3. GPU Metrics (NVIDIA via nvidia-smi)
    gpu_info: Dict[str, Any] = {
        "has_gpu": False,
        "name": "N/D (GPU integrada o sin soporte NVIDIA)",
        "gpu_util_percent": 0.0,
        "vram_used_mb": 0.0,
        "vram_total_mb": 0.0,
        "vram_used_gb": 0.0,
        "vram_total_gb": 0.0,
        "vram_percent": 0.0,
        "temperature_c": None,
    }

    nvsmi = shutil.which("nvidia-smi")
    if nvsmi:
        try:
            cmd = [
                nvsmi,
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0 and res.stdout.strip():
                line = res.stdout.strip().split("\n")[0]
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    name = parts[0]
                    gpu_util = float(parts[1]) if parts[1].replace(".", "", 1).isdigit() else 0.0
                    mem_used = float(parts[2]) if parts[2].replace(".", "", 1).isdigit() else 0.0
                    mem_total = float(parts[3]) if parts[3].replace(".", "", 1).isdigit() else 0.0
                    temp = float(parts[4]) if len(parts) >= 5 and parts[4].replace(".", "", 1).isdigit() else None

                    vram_pct = round((mem_used / mem_total * 100), 1) if mem_total > 0 else 0.0

                    gpu_info = {
                        "has_gpu": True,
                        "name": name,
                        "gpu_util_percent": gpu_util,
                        "vram_used_mb": mem_used,
                        "vram_total_mb": mem_total,
                        "vram_used_gb": round(mem_used / 1024, 2),
                        "vram_total_gb": round(mem_total / 1024, 2),
                        "vram_percent": vram_pct,
                        "temperature_c": temp,
                    }
        except Exception:
            pass

    return {
        "cpu": {
            "percent": cpu_percent,
            "logical_cores": cpu_cores_logical,
            "physical_cores": cpu_cores_physical,
        },
        "ram": {
            "total_gb": ram_total_gb,
            "used_gb": ram_used_gb,
            "available_gb": ram_available_gb,
            "percent": ram_percent,
        },
        "gpu": gpu_info,
        "timestamp": time.time(),
    }


class StageStopwatch:
    """
    Stopwatch utility to accurately record timings across document processing stages.
    """

    def __init__(self):
        self.stage_timings: Dict[str, float] = {}
        self.total_start: Optional[float] = None
        self.total_elapsed: float = 0.0
        self._current_stage: Optional[str] = None
        self._current_stage_start: Optional[float] = None

    def start_total(self) -> None:
        self.total_start = time.time()
        self.stage_timings.clear()
        self.total_elapsed = 0.0

    def start_stage(self, stage_name: str) -> None:
        if self._current_stage and self._current_stage_start:
            self.stop_stage()
        self._current_stage = stage_name
        self._current_stage_start = time.time()

    def stop_stage(self) -> float:
        if self._current_stage and self._current_stage_start:
            elapsed = round(time.time() - self._current_stage_start, 2)
            self.stage_timings[self._current_stage] = (
                self.stage_timings.get(self._current_stage, 0.0) + elapsed
            )
            self._current_stage = None
            self._current_stage_start = None
            return elapsed
        return 0.0

    def finish_total(self) -> float:
        if self._current_stage:
            self.stop_stage()
        if self.total_start:
            self.total_elapsed = round(time.time() - self.total_start, 2)
        else:
            self.total_elapsed = round(sum(self.stage_timings.values()), 2)
        return self.total_elapsed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stages": {k: round(v, 2) for k, v in self.stage_timings.items()},
            "total_seconds": round(self.total_elapsed, 2),
        }


def render_telemetry_expander(
    latest_telemetry: Optional[Dict[str, Any]] = None,
    default_expanded: bool = False
) -> None:
    """
    Renders an interactive, collapsible visor displaying system hardware
    utilization (CPU, RAM, GPU/VRAM) and stopwatch stage metrics (parsing, LLM, etc.).
    """
    import streamlit as st
    import pandas as pd

    metrics = get_system_metrics()
    cpu = metrics["cpu"]
    ram = metrics["ram"]
    gpu = metrics["gpu"]

    # Header summary tags
    gpu_badge = f"🎮 GPU: {gpu['gpu_util_percent']}%" if gpu["has_gpu"] else "🎮 GPU: N/D"
    timings_badge = (
        f" | ⏱️ Lote previo: {latest_telemetry['total_seconds']}s"
        if (latest_telemetry and "total_seconds" in latest_telemetry)
        else ""
    )
    title = f"⚡ Telemetría del Sistema y Tiempos por Etapa (💻 CPU: {cpu['percent']}% | 🧠 RAM: {ram['percent']}% | {gpu_badge}{timings_badge})"

    with st.expander(title, expanded=default_expanded):
        # 1. Hardware Resource Metrics
        col_hdr, col_ref = st.columns([3, 1])
        with col_hdr:
            st.markdown("##### 💻 Recursos del Sistema en Tiempo Real")
        with col_ref:
            if st.button("🔄 Actualizar Recursos", key="btn_refresh_telemetry", width="stretch"):
                st.rerun()

        col_cpu, col_ram, col_gpu = st.columns(3)

        with col_cpu:
            st.metric(
                label="💻 CPU Total",
                value=f"{cpu['percent']}%",
                help=f"{cpu['logical_cores']} hilos lógicos ({cpu['physical_cores']} núcleos físicos)"
            )
            st.caption(f"{cpu['logical_cores']} hilos lógicos | {cpu['physical_cores']} núcleos físicos")
            st.progress(min(max(cpu["percent"] / 100.0, 0.0), 1.0))

        with col_ram:
            st.metric(
                label="🧠 Memoria RAM",
                value=f"{ram['used_gb']} GB",
                help=f"Uso: {ram['used_gb']} GB de {ram['total_gb']} GB ({ram['percent']}%)"
            )
            st.caption(f"{ram['percent']}% de {ram['total_gb']} GB ({ram['available_gb']} GB libres)")
            st.progress(min(max(ram["percent"] / 100.0, 0.0), 1.0))

        with col_gpu:
            if gpu["has_gpu"]:
                st.metric(
                    label=f"🎮 {gpu['name']}",
                    value=f"{gpu['gpu_util_percent']}%",
                    help=f"VRAM Usada: {gpu['vram_used_gb']} GB de {gpu['vram_total_gb']} GB ({gpu['vram_percent']}%)"
                )
                temp_str = f" | {gpu['temperature_c']}°C" if gpu.get("temperature_c") is not None else ""
                st.caption(f"VRAM: {gpu['vram_used_gb']} / {gpu['vram_total_gb']} GB ({gpu['vram_percent']}%){temp_str}")
                st.progress(min(max(gpu["vram_percent"] / 100.0, 0.0), 1.0))
            else:
                st.metric(
                    label="🎮 GPU Dedicada",
                    value="No detectada",
                    help="No se detectó GPU NVIDIA compatible o nvidia-smi"
                )
                st.caption("Procesamiento en CPU / Gráficos integrados")
                st.progress(0.0)

        st.divider()

        # 2. Processing Stage Timings
        st.markdown("##### ⏱️ Cronómetro de Etapas de Procesamiento")
        if latest_telemetry and "stages" in latest_telemetry and latest_telemetry["stages"]:
            stages = latest_telemetry["stages"]
            total_sec = max(latest_telemetry.get("total_seconds", 0.0), 0.001)

            st.caption(f"Tiempo total acumulado de la última ejecución: **{total_sec:.2f} segundos**")

            # Metrics row
            col_list = st.columns(len(stages) + 1)
            for idx, (stg_name, stg_time) in enumerate(stages.items()):
                pct = round((stg_time / total_sec) * 100, 1)
                with col_list[idx]:
                    st.metric(
                        label=stg_name,
                        value=f"{stg_time:.2f}s",
                        delta=f"{pct}% del total",
                        delta_color="off"
                    )

            with col_list[-1]:
                st.metric(
                    label="⏱️ Total del Lote",
                    value=f"{total_sec:.2f}s",
                    delta="100%",
                    delta_color="off"
                )

            # Table breakdown
            stage_rows = []
            for stg_name, stg_time in stages.items():
                pct = round((stg_time / total_sec) * 100, 1)
                stage_rows.append({
                    "Etapa de Procesamiento": stg_name,
                    "Duración (segundos)": f"{stg_time:.2f} s",
                    "Porcentaje (%)": f"{pct}%"
                })
            stage_rows.append({
                "Etapa de Procesamiento": "⏱️ TIEMPO TOTAL ACUMULADO",
                "Duración (segundos)": f"{total_sec:.2f} s",
                "Porcentaje (%)": "100.0%"
            })
            df_stages = pd.DataFrame(stage_rows)
            st.dataframe(df_stages, width="stretch", hide_index=True)

        else:
            st.info(
                "💡 Procesa uno o más estados de cuenta para registrar con precisión los tiempos "
                "de cada etapa: Inspección Pre-flight, Docling/OCR, Inferencia en Ollama y Normalización."
            )

