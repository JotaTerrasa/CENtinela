# Defensa de CENtinela

Este nombre se conserva por compatibilidad con entregas anteriores.

La defensa vigente está en [`DEFENSA_CTO.md`](DEFENSA_CTO.md). Refleja la
arquitectura final:

- Planner y filtro deterministas en Codex, generativos con fallback en los
  perfiles OpenAI/Ollama/vLLM;
- provider factory para Codex, OpenAI API, Ollama y vLLM, con modelos por rol;
- perfil `centinela_runtime` de permisos mínimos, sin el mecanismo heredado
  `--sandbox`;
- bloqueo de informe, Judge y chat si el proveedor/modelo no está listo;
- coste API, suscripción e infraestructura self-hosted claramente separados;
- persistencia, exportación y memoria únicamente para informes aprobados.

También incluye el guion de demo, contingencias, preguntas difíciles y la
explicación de que el PDF oficial es agnóstico respecto al proveedor/modelo y la
arquitectura ya demuestra portabilidad sin perder la ruta Codex validada.
