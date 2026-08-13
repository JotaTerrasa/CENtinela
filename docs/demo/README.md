# Evidencia de demostración

Esta carpeta permite revisar el MVP sin una nueva llamada a Codex. Las
capturas se obtuvieron del contenedor final en `http://127.0.0.1:8501` el 13 de
agosto de 2026, después de verificar la sesión ChatGPT/Codex, las siete fuentes
y el flujo de aceptación.

## Galería

| Pantalla | Evidencia |
|---|---|
| Login y creación de cuenta | [01-login.png](screenshots/01-login.png) |
| Dashboard con 53 publicaciones y cobertura 7/7 | [02-dashboard.png](screenshots/02-dashboard.png) |
| Alertas personalizadas | [03-alertas.png](screenshots/03-alertas.png) |
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
- [Resumen de validación](validation-summary.json)

Las capturas usan un usuario local desechable cuyo nombre no concede acceso a
ningún sistema externo. La base de datos, su contraseña y la sesión de
ChatGPT/Codex no forman parte del repositorio ni del ZIP.

Los portales oficiales y los modelos son sistemas vivos. Una repetición puede
recuperar publicaciones distintas o producir una redacción diferente; el
contrato de citas, los validadores, la topología LangGraph y la telemetría se
mantienen.
