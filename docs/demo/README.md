# Evidencia de demostración

Esta carpeta permite revisar el MVP sin una nueva llamada a un modelo. El
informe y la consulta RAG proceden de una ejecución Codex real validada el 13 de
agosto de 2026. Las capturas se regeneraron después de incorporar la factoría
Codex/OpenAI/Ollama/vLLM y muestran dos superficies distintas:

- `01-login.png`: modo interactivo privado, antes de autenticar al usuario;
- `02`–`09`: `PUBLIC_DEMO_MODE`, que verifica y carga los artefactos en memoria,
  sin SQLite ni identidad por visitante, y bloquea scraping, indexación e
  inferencia nuevos.

El banner amarillo y los controles deshabilitados impiden confundir el replay
público con una ejecución generativa en vivo. La alerta es una configuración UI
simulada, declarada en el manifest y rotulada como tal; no se presenta como
evidencia histórica.

## Galería

| Pantalla | Evidencia |
|---|---|
| Login y creación de cuenta | [01-login.png](screenshots/01-login.png) |
| Dashboard: 34 citas/6 organismos conservados y snapshot original 53/7 | [02-dashboard.png](screenshots/02-dashboard.png) |
| Configuración de alerta simulada, en solo lectura | [03-alertas.png](screenshots/03-alertas.png) |
| Informe regulatorio aprobado y descargable | [04-informe-aprobado.png](screenshots/04-informe-aprobado.png) |
| Evaluación LLM-as-Judge 78/100 | [05-judge.png](screenshots/05-judge.png) |
| Chat RAG con citas CNE y SEA | [06-chat-rag.png](screenshots/06-chat-rag.png) |
| Observabilidad y tokenomics | [07-observabilidad.png](screenshots/07-observabilidad.png) |
| Detalle por ejecución y llamada | [08-observabilidad-detalle.png](screenshots/08-observabilidad-detalle.png) |
| Arquitectura y controles | [09-arquitectura.png](screenshots/09-arquitectura.png) |

## Artefactos auditables

- [Informe aprobado en Markdown](sample-report.md)
- [Informe, citas, dictamen y métricas en JSON](sample-report.json)
- [Consulta RAG real y fuentes](sample-rag.json)
- [Resumen de la ejecución de aceptación original](validation-summary.json)
- [Manifest de integridad y traza por llamada](replay-manifest.json)

La captura de acceso no contiene credenciales. El replay es de solo lectura, no
concede acceso a ningún sistema externo, no prepara rutas de runtime y no
escribe actividad de visitantes.
La base de datos de ejecución, contraseñas y sesión ChatGPT/Codex no forman
parte del repositorio ni de los paquetes.

El bundle permite reproducir la interfaz y verificar hashes, salidas y
telemetría conservada. No permite reejecutar la cadena de evidencia: el catálogo
JSON no contiene extractos, fechas por artículo ni la fila CEN del snapshot.
Por eso el Dashboard muestra N/D también para el método de captura y separa el
catálogo 34/6 del resultado de aceptación 53/7, sin reconstruir datos ausentes.
La respuesta RAG se muestra como redacción generada con sus citas; no se
reutiliza como si fuera el pasaje recuperado, porque ese pasaje no se conservó.

El manifest distingue tres relojes: latencia de pared de la ejecución, latencia
de cada nodo y latencia de cada llamada. El `metrics.latency_seconds` del informe
es la suma de las dos llamadas generativas; la tabla de Observabilidad muestra
en la fila principal el tiempo de pared y en el detalle el tiempo por llamada.

`validation-summary.json` conserva las condiciones históricas de la aceptación;
por eso su recuento de pruebas no se reescribe al crecer la suite. Los portales
oficiales y los modelos son sistemas vivos: una ejecución interactiva nueva
puede recuperar otras publicaciones o producir otra redacción. El contrato de
citas, los validadores, la topología LangGraph y la telemetría se mantienen.
