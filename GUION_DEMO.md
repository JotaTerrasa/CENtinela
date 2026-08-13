# Guion de demostración de CENtinela

Recorrido reproducible de **7 minutos** para presentar el MVP sin depender de
que las siete webs públicas o el proveedor de IA respondan durante la reunión.
La demo enseña primero el valor y después la arquitectura. El discurso técnico
ampliado y las preguntas de comité están en
[`DEFENSA_CTO.md`](DEFENSA_CTO.md).

## Resultado que debe recordar la audiencia

> CENtinela transforma publicaciones oficiales dispersas en decisiones
> regulatorias trazables: cada insight conserva su fuente, cada informe pasa una
> barrera de calidad y cada llamada de IA deja una traza de uso y coste.

La demo no pretende probar que un modelo “sabe regulación”. Debe probar que el
sistema **restringe, cita, evalúa y permite auditar** lo que el modelo produce.

## Preparación del entorno

### El día anterior

1. Levantar la aplicación y conservar sus volúmenes:

   ```bash
   cp .env.example .env
   docker compose up -d --build
   docker compose exec centinela codex login --device-auth
   docker compose exec centinela codex login status
   ```

2. Confirmar que el estado de login indica ChatGPT. No mostrar el código de
   device auth, tokens ni el contenido del volumen `centinela-codex-auth`.
3. Crear un usuario local exclusivo para la demo, con una contraseña no
   reutilizada.
4. Actualizar fuentes, crear la alerta `Activos prioritarios` y generar un informe
   que termine **aprobado**.
5. Ejecutar la pregunta RAG preparada y abrir al menos dos URLs oficiales.
6. Dejar abiertas, en este orden, las pantallas Dashboard, Informe, Chat RAG,
   Observabilidad y Arquitectura.
7. Guardar localmente la evidencia de [`docs/demo/`](docs/demo/README.md) como
   plan de respaldo. No reconstruir ni eliminar volúmenes tras esta validación.

Las pruebas se ejecutan desde el entorno de desarrollo, no desde la imagen de
runtime:

```bash
source .venv/bin/activate
python -m compileall -q app.py core scrapers agent rag scripts
ruff check .
pytest -q
```

### Treinta minutos antes

```bash
docker compose ps
docker compose exec centinela codex login status
docker compose logs --tail=100 centinela
curl --fail --silent http://127.0.0.1:8501/_stcore/health
```

El gate está superado si el contenedor está healthy, la aplicación responde,
la sesión esperada está activa y existe un informe aprobado. También hay que
cerrar notificaciones, terminales y pestañas que puedan mostrar información
privada.

## Recorrido cronometrado — 7 minutos

### 0:00–0:35 · Problema y propuesta

**Pantalla:** acceso o Dashboard ya autenticado.

**Decir:**

> Un analista de activos solares, BESS, hidrógeno verde o data centers necesita
> vigilar organismos que publican con formatos y ritmos distintos. CENtinela
> reúne esa evidencia y la convierte en alertas, informes y respuestas citadas.
> No es un chatbot abierto: es un proceso regulatorio asistido y auditable.

**Demostrar:** el producto está centrado en el mercado eléctrico chileno y
separa la sesión del usuario de la identidad del proveedor de IA.

### 0:35–1:35 · Dashboard y fuentes oficiales

**Pantalla:** Dashboard.

**Acción en vivo:** mostrar cobertura y frescura; filtrar por BESS o transmisión;
abrir una publicación en su URL oficial.

**Acción en replay:** mostrar primero las dos cifras separadas —34 citas de 6
organismos conservadas y 53 publicaciones/7 fuentes en el snapshot de aceptación—;
abrir una URL oficial. No presentar las fechas N/D como pérdida del sistema: no
formaban parte del artefacto exportado.

**Decir:**

> La captura consulta CEN, CNE, Ministerio de Energía, SEC, SEA, Senado y
> Cámara. Normaliza organismo, fecha, título, temas, contenido y URL. Si una
> fuente falla, el lote continúa y la cobertura parcial queda explícita; nunca
> se inventa una noticia para rellenar el panel.

**Evidencia visible:** tabla de citas reales, URL primaria y alcance declarado.
El catálogo conservado contiene 34 citas de 6 organismos; el resumen histórico
acredita que el snapshot original tuvo 53 publicaciones y cobertura 7/7. No se
debe fusionar ambos conjuntos ni prometer ese mismo número en una nueva captura.

### 1:35–2:05 · Alertas personalizadas

**Pantalla:** Alertas.

**Acción:** abrir la regla simulada `Activos prioritarios` y enseñar palabras
clave y organismos seleccionados.

**Decir:**

> Las alertas pertenecen al usuario y no son solo un filtro visual: sus términos
> alimentan la priorización del informe. La selección es reproducible y queda
> separada de la redacción generativa.

**Evidencia visible:** en vivo, regla persistida y coincidencias identificables;
en replay, configuración UI simulada, no persistente y explícitamente separada
de la evidencia histórica.

### 2:05–3:35 · Informe diario y barrera de calidad

**Pantalla:** Informe diario, usando un informe aprobado ya preparado.

**Acción:** leer una sola afirmación; abrir su cita `[Fuente | URL]`; enseñar el
dictamen y la puntuación del Judge.

**Decir:**

> LangGraph fija Planner, Scraper, Executor y Evaluator. El redactor solo recibe
> un catálogo cerrado de evidencia. Después, una validación local rechaza citas
> desconocidas o afirmaciones materiales sin fuente y el Judge puntúa
> relevancia, cobertura, claridad y trazabilidad. Un rechazo tiene efecto real:
> no se guarda como informe válido, no se exporta y no alimenta la memoria.

**Evidencia visible:** cita navegable, veredicto, puntuación y ausencia de URLs
desconocidas. El artefacto de respaldo fue aprobado con 78/100; se presenta como
una ejecución concreta, no como una garantía general de calidad.

### 3:35–4:35 · Chat RAG con trazabilidad

**Pantalla:** Chat RAG.

**Acción:** formular esta pregunta preparada:

> ¿Qué informan la CNE y el SEA sobre subestaciones digitales y proyectos
> evaluados en Coquimbo?

Abrir las dos fuentes devueltas.

**Decir:**

> ChromaDB recupera los fragmentos y el modelo responde solo sobre ese contexto.
> Las fuentes se devuelven como objetos separados y CENtinela inserta citas
> verificadas; no confía en una URL escrita libremente por el modelo. Si cambia
> el modelo de embeddings, la identidad del índice obliga a reindexar y evita
> mezclar espacios vectoriales.

**Evidencia visible:** respuesta acotada y enlaces originales de CNE y SEA.

### 4:35–5:40 · Observabilidad y tokenomics

**Pantalla:** Observabilidad y detalle de una ejecución.

**Acción:** abrir las llamadas de informe y Judge; señalar proveedor, modelo,
rol, tokens, latencia y modo de facturación.

**Decir:**

> La telemetría registra tokens reportados por el backend, latencia, estado y
> rol. Separamos tres conceptos: coste API, suscripción Codex y cómputo
> self-hosted. En una sesión ChatGPT/Codex el coste por turno es no atribuible,
> por eso mostramos N/A; en OpenAI se calcula por tokens y en Ollama o vLLM el
> coste API es cero, pero la infraestructura se contabiliza aparte.

**Evidencia visible:** `prompt_tokens`, `completion_tokens`, modelo efectivo y
categoría económica. No describir cero API como coste total cero.

### 5:40–6:35 · Portabilidad y arquitectura cloud

**Pantalla:** Arquitectura.

**Acción:** recorrer el grafo y la tabla de routing Codex/OpenAI/Ollama/vLLM.

**Decir:**

> La topología y los contratos no cambian con el proveedor. Codex es la ruta
> validada para esta demo; OpenAI ofrece un servicio gestionado; Ollama sirve
> desarrollo y pilotos locales; y vLLM tras un gateway privado es la ruta
> recomendada para inferencia cloud con GPU. Los modelos se eligen por rol, no
> como una dependencia fija de la aplicación.

**Evidencia visible:** Planner, Scraper, Executor, Judge y providers por rol.

### 6:35–7:00 · Cierre

**Decir:**

> El diferencial no es generar el texto más convincente, sino conservar la
> cadena de evidencia más defendible. CENtinela reduce el trabajo repetitivo;
> las fuentes oficiales y el especialista mantienen la decisión final.

Finalizar en Arquitectura o volver al Dashboard. No terminar en una terminal.

## Plan de contingencia

| Incidencia | Acción durante la demo | Mensaje defendible |
|---|---|---|
| Falla una web oficial | Usar el snapshot persistido y mostrar cobertura parcial | “El fallo queda visible; el sistema no completa el corpus con contenido inventado.” |
| La sesión Codex expiró | No improvisar credenciales; mostrar el último informe aprobado | “Las funciones generativas se bloquean, pero evidencia, alertas y auditoría siguen disponibles.” |
| Un modelo no responde | Abrir la traza fallida y continuar con evidencia estática | “No hacemos un cambio silencioso de modelo en una ejecución auditada.” |
| El Judge rechaza | Enseñar el rechazo y luego un informe aprobado anterior | “El control tiene efecto operativo: el borrador no se distribuye.” |
| Falla ChromaDB | Mostrar recuperación lexical etiquetada o `sample-rag.json` | “El modo alternativo permanece acotado al corpus y no se presenta como búsqueda vectorial.” |
| La red de la sala falla | Usar capturas y JSON de `docs/demo/` | “Esta es una ejecución validada; distingo evidencia grabada de una llamada en vivo.” |
| Los tokens aparecen en cero | Revisar `token_usage_status` | “No estimamos tokens por caracteres; indicamos cuando el proveedor no reporta uso.” |

## Plan B sin servicios externos

Seguir exactamente el mismo recorrido con estas evidencias:

1. [`02-dashboard.png`](docs/demo/screenshots/02-dashboard.png).
2. [`03-alertas.png`](docs/demo/screenshots/03-alertas.png).
3. [`04-informe-aprobado.png`](docs/demo/screenshots/04-informe-aprobado.png) y
   [`sample-report.md`](docs/demo/sample-report.md).
4. [`05-judge.png`](docs/demo/screenshots/05-judge.png).
5. [`06-chat-rag.png`](docs/demo/screenshots/06-chat-rag.png) y
   [`sample-rag.json`](docs/demo/sample-rag.json).
6. [`08-observabilidad-detalle.png`](docs/demo/screenshots/08-observabilidad-detalle.png).
7. [`09-arquitectura.png`](docs/demo/screenshots/09-arquitectura.png).

Hay que decir al principio que se muestran artefactos de una ejecución real ya
validada. Nunca simular que una captura está ocurriendo en vivo.

## Ensayo y criterio de aprobado

La demo está lista cuando dos ensayos consecutivos duran entre 6:30 y 7:30,
cada afirmación técnica puede señalarse en pantalla y el presentador completa el
Plan B sin buscar archivos. Si una sección se alarga, recortar primero Alertas y
la explicación de embeddings; no recortar informe, citas, Judge ni
observabilidad.
