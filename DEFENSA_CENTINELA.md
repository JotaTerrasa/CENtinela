# Defensa de CENtinela

Este nombre se conserva por compatibilidad con entregas anteriores.

La defensa vigente está en [`DEFENSA_CTO.md`](DEFENSA_CTO.md). Refleja la
arquitectura final:

- Planner y filtro deterministas por defecto;
- Codex Sol para redacción, Terra para LLM-as-Judge y Luna para chat RAG;
- perfil `centinela_runtime` de permisos mínimos, sin el mecanismo heredado
  `--sandbox`;
- bloqueo de informe y chat sin una sesión ChatGPT/Codex confirmada;
- coste atribuible N/A e imputación interna opcional claramente separada;
- persistencia, exportación y memoria únicamente para informes aprobados.

También incluye el guion de demo, contingencias, preguntas difíciles y la
explicación de que el PDF oficial es agnóstico respecto al proveedor/modelo; el
perfil Codex-only responde a una decisión posterior del usuario.
