# Defensa de CENtinela ante CTO

Guion para una exposición de 8–10 minutos. Describe la arquitectura final del
repositorio: orquestación determinista donde no aporta valor llamar a un modelo,
routing barato en los perfiles HTTP, RAG trazable y barreras de calidad antes de
persistir o distribuir un informe.

## Mensaje central

> CENtinela convierte siete canales regulatorios chilenos en una cadena de
> evidencia operable: captura, normaliza, prioriza, recupera, sintetiza, evalúa
> y cita. El código controla el proceso; según el perfil, el proveedor planifica,
> filtra, redacta, evalúa y responde sobre evidencia cerrada; el especialista
> conserva la decisión final.

Respuesta corta a “¿qué has construido?”:

> Un MVP end-to-end de inteligencia regulatoria para el SEN: dashboard,
> alertas, informe diario Planner-Executor, LLM-as-Judge, chat RAG, URLs
> trazables y observabilidad de tokens/costes. Puede operar con Codex, OpenAI API,
> Ollama o vLLM, manteniendo las mismas barreras de evidencia.

## Seis ideas que deben quedar claras

1. **Evidencia antes que generación.** Los modelos solo reciben fragmentos de un
   catálogo capturado y delimitado como datos no confiables.
2. **Determinismo donde es suficiente.** Codex evita dos procesos CLI para
   Planner/filtro; los perfiles HTTP usan el modelo barato exigido y vuelven al
   contrato determinista si falla.
3. **Routing por responsabilidad.** Cada proveedor define modelos de Planner,
   filtro/RAG, informe y Judge, con overrides independientes por rol.
4. **RAG configurable.** ChromaDB usa `local-hash-1536` por defecto y admite
   embeddings OpenAI-compatible sin mezclar espacios vectoriales.
5. **Calidad con efecto operativo.** Un informe rechazado queda registrado como
   ejecución rechazada, pero no se guarda como informe, no alimenta la memoria
   diaria y no se distribuye.
6. **Contabilidad honesta.** Se registran tokens por backend. API, suscripción y
   cómputo self-hosted se muestran separados; API cero no significa TCO cero.

## Guion hablado — objetivo: 9 minutos

### 0:00–0:40 · Apertura

**En pantalla:** acceso de CENtinela.

“Seguir regulación eléctrica chilena obliga a revisar organismos con formatos,
ritmos y disponibilidad distintos. Para solar, BESS, hidrógeno verde o data
centers, encontrar una publicación es solo el principio: hay que entender qué
cambió y poder demostrar de dónde sale cada conclusión.

CENtinela reduce ese trabajo sin separar la síntesis de la evidencia.”

### 0:40–1:30 · Criterio de producto

“No he construido un chatbot abierto. He construido un proceso regulatorio
asistido con tres principios: fuentes oficiales, trazabilidad extremo a extremo
y revisión humana.

Una cita confirma procedencia; no certifica una interpretación jurídica. El
producto acelera al analista, pero no reemplaza su responsabilidad.”

### 1:30–2:20 · Capacidades

**En pantalla:** Dashboard con la lectura ejecutiva y publicaciones capturadas.

“La plataforma consulta CEN, CNE, Ministerio de Energía, SEC, SEA, Senado y
Cámara. Normaliza título, contenido, fecha, temas, organismo y URL. SQLite
conserva el dominio transaccional y ChromaDB el índice RAG.

El primer screen resume qué ocurre, dónde mirar, cuál es el siguiente foco, la
frescura y si la cobertura es parcial. Sobre ese corpus ofrece alertas por
usuario, informe diario y preguntas ad-hoc con fuentes originales.”

### 2:20–4:05 · Arquitectura final

**En pantalla:** panel Arquitectura.

“El MVP es un monolito modular. Streamlit simplifica la demostración, mientras
los paquetes separan configuración, datos, captura, agente, RAG y
observabilidad.

LangGraph fija la topología Planner, Scraper, Executor, Evaluator y fin. En el
perfil Codex, esa topología no implica cuatro procesos: el Planner materializa
un contrato determinista porque las siete fuentes y el horizonte están acotados,
y el filtro prioriza alertas, palabras clave y activos objetivo. En OpenAI,
Ollama y vLLM ambos roles sí usan el modelo barato configurado, con validación de
JSON y fallback determinista. El Scraper reutiliza un snapshot fresco cuando
existe, consulta solo las fuentes que faltan, normaliza, persiste e indexa.

Sol se reserva para el artefacto de mayor valor: la redacción ejecutiva. Terra
evalúa relevancia, cobertura, claridad y trazabilidad, siempre combinada con la
validación local de citas. Luna se usa en el chat RAG para responder sobre los
fragmentos recuperados. En la ruta OpenAI, GPT-4o mini planifica y filtra y
GPT-4o redacta, cumpliendo el routing solicitado sin acoplar el grafo.”

### 4:05–5:05 · Runtime Codex y permisos

“Todas las llamadas generativas pasan por `CodexClient`. El cliente usa
`codex exec` en modo efímero, recibe el prompt por `stdin` y procesa eventos
JSONL.

La seguridad no depende del antiguo flag `--sandbox`. Cada ejecución activa
`--strict-config` y un perfil de permisos denominado `centinela_runtime`. El
perfil permite únicamente lectura mínima en el directorio de trabajo aislado,
deniega el repositorio, `/app` y `CODEX_HOME`, deshabilita red para comandos del
agente, no hereda el entorno del proceso y usa una lista PATH mínima. También
fija `approval_policy=never`, ignora configuración y reglas personales y fuerza
el método de login ChatGPT. Si `codex login status` indica API key u otro método,
el frontend rechaza esa sesión.”

### 5:05–6:40 · Demostración funcional

#### Dashboard y alertas

“Cada fila mantiene la URL primaria. Un fallo de un organismo no invalida el
lote: la cobertura parcial y la incidencia quedan visibles. Las alertas combinan
organismos y palabras clave, pertenecen al usuario y alimentan la prioridad
determinista.”

#### Informe diario

“Sol redacta únicamente sobre el catálogo permitido y cada afirmación material
debe terminar en `[Fuente | URL]`. Una barrera local rechaza URLs desconocidas o
líneas sin evidencia. Existe un único intento acotado de corrección; si hace
falta, el flujo puede sustituir el borrador por una síntesis extractiva citada y
pedir a Terra una nueva evaluación.

La decisión de Terra no es decorativa. Si el resultado no queda aprobado, la
ejecución termina como `rejected`, el frontend no la llama completada y el
contenido no se guarda en `reports`, no se exporta y no entra en la memoria del
día. Solo un informe aprobado llega al historial.”

#### Chat RAG

“Chroma recupera fragmentos con embeddings locales. Luna responde solo sobre
esos fragmentos y devuelve respuesta y fuentes por separado. Si falla el índice
vectorial, la recuperación lexical mantiene una salida trazable dentro de una
ejecución ya autorizada.”

### 6:40–7:35 · Observabilidad y economía

**En pantalla:** Observabilidad, detalle por llamada.

“El adaptador no estima tokens por caracteres. Lee `turn.completed.usage` del
JSONL y normaliza entrada, salida, caché y razonamiento cuando están disponibles.
Registra modelo, rol, latencia, estado, método de autenticación, perfil de
permisos y error sanitizado.

La sesión pertenece a ChatGPT/Codex. Ese modo no ofrece una tarifa API
atribuible por turno, por lo que la interfaz muestra coste Codex N/A, no cero
económico. Los campos numéricos de compatibilidad permanecen en cero junto a
`billing_mode=subscription` y `cost_attribution=not_attributable`.

Para planificación financiera existe una simulación separada: coste mensual
interno dividido por ejecuciones previstas. Es opcional, no se persiste, no
modifica la telemetría y no se presenta como factura ni precio por token.”

### 7:35–8:35 · Seguridad y producción

“En Docker, device auth guarda la sesión en un volumen exclusivo `CODEX_HOME`.
Ese volumen contiene credenciales, es escribible para renovación y se trata
como una contraseña: nunca entra en la imagen, Git, tickets ni otros
contenedores.

El perfil de permisos reduce la superficie accesible incluso dentro del
contenedor. Aun así, una sesión interactiva y un volumen local son adecuados
para una demo privada, no una identidad empresarial multi-tenant. Producción
requiere SSO para usuarios, identidad Codex dedicada o mecanismo empresarial
autorizado, rotación, revocación, vault, PostgreSQL, workers y auditoría
inmutable.”

### 8:35–9:00 · Cierre

“CENtinela no compite por generar el texto más convincente. Compite por ofrecer
la cadena de evidencia más defendible. El código restringe y prioriza; Codex
redacta y evalúa; la URL y el especialista conservan el control.”

## Recorrido exacto de demo — 5 minutos

| Tiempo | Pantalla | Acción | Evidencia |
|---:|---|---|---|
| 0:00–0:25 | Acceso | Iniciar sesión | Separación por usuario y sesión Codex ChatGPT |
| 0:25–1:15 | Dashboard | Mostrar foco, frescura, cobertura y abrir URL | Corpus real y trazabilidad |
| 1:15–1:40 | Alertas | Abrir regla BESS/transmisión | Priorización determinista |
| 1:40–2:55 | Informe | Abrir un informe aprobado, cita y Judge | Barrera de distribución |
| 2:55–3:35 | Chat RAG | Ejecutar pregunta corta | Luna, retrieval local y fuentes |
| 3:35–4:30 | Observabilidad | Abrir llamadas Sol/Terra/Luna | Tokens, permisos y coste N/A |
| 4:30–5:00 | Arquitectura | Mostrar routing y límites | Criterio técnico |

La operación en vivo preferida es una consulta RAG sobre un índice preparado.
El scraping completo y la generación del informe dependen de terceros y deben
quedar validados antes de la exposición. Nunca utilizar como muestra un informe
rechazado salvo que se quiera demostrar explícitamente la barrera de calidad.

## Preparación técnica

### Ruta Docker recomendada

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec centinela codex login --device-auth
docker compose exec centinela codex login status
.venv/bin/python -m pytest -q
```

`codex login status` debe confirmar explícitamente **ChatGPT**. Una sesión por
API key no satisface el perfil de ejecución. No mostrar el código device auth,
el contenido de `/home/centinela/.codex` ni ningún token durante la demo.

En la aplicación:

1. Crear un usuario de demo con contraseña no reutilizada.
2. Actualizar las fuentes con antelación y confirmar publicaciones persistidas.
3. Crear la alerta `BESS y transmisión`.
4. Generar un informe y comprobar que el Judge lo aprueba.
5. Confirmar que el informe aprobado aparece en historial y descarga.
6. Formular dos preguntas RAG y abrir sus fuentes.
7. Revisar tokens, `auth_method=chatgpt`, `permission_profile` y coste N/A.
8. Dejar preparados Dashboard, Informe, Chat, Observabilidad y Arquitectura.

### Treinta minutos antes

```bash
docker compose ps
docker compose exec centinela codex login status
docker compose logs --tail=100 centinela
```

- Abrir <http://127.0.0.1:8501>.
- Confirmar acceso efectivo a Sol, Terra y Luna.
- Abrir dos URLs oficiales.
- Verificar que la cobertura parcial, si existe, está explicada.
- Conservar un informe **aprobado** y una respuesta RAG ya validados.
- Revisar que no haya notificaciones ni pestañas con información privada.
- No borrar volúmenes ni reconstruir después de validar la demo.

## Plan de contingencia

### Falla una fuente pública

> “La captura está aislada por organismo. El lote continúa, la cobertura parcial
> queda visible y el snapshot permite seguir trabajando. No sustituimos la
> fuente por contenido inventado.”

Usar el snapshot existente y no dedicar la demo a reintentos.

### Codex no está autenticado

Comprobar `docker compose exec centinela codex login status`. Si la sesión
expiró, repetir device auth. Sin una sesión ChatGPT confirmada, CENtinela bloquea
informe y chat; dashboard, scraping, alertas e índice local siguen disponibles.
No copiar `auth.json` desde una máquina personal durante la exposición.

### Codex alcanza límites o no responde

> “La capa de evidencia continúa operativa, pero no presento un fallback como si
> fuera una ejecución generativa autorizada. Muestro el último informe aprobado
> y la traza fallida.”

El fallback protege una ejecución ya iniciada; no sustituye la autenticación ni
permite distribuir un resultado que Terra no haya aprobado.

### Un modelo no está disponible

> “Los modelos se solicitan explícitamente por responsabilidad. El error queda
> observable y no se cambia silenciosamente de modelo durante una ejecución
> auditada.”

### Falla la recuperación vectorial

> “El sistema intenta recuperación lexical sobre el corpus local y etiqueta el
> modo de respuesta. No presenta el fallback como búsqueda vectorial.”

### El Judge rechaza el informe

> “El rechazo demuestra que el control tiene efecto: la ejecución se marca
> `rejected`, el informe no se distribuye, no se guarda en el historial y no
> contamina la memoria del día.”

Mostrar la traza rechazada en Observabilidad y continuar con un informe aprobado
anterior.

### Tokens aparecen en cero

> “Verifico si el CLI publicó `turn.completed.usage`; no estimo tokens desde
> caracteres. El coste Codex es N/A porque no existe una tarifa atribuible por
> turno bajo esta sesión.”

## Preguntas difíciles y respuestas defendibles

### 1. ¿Por qué LangGraph si el flujo es lineal?

Porque aporta estado tipado, nodos inspeccionables y topología testeable. La
secuencia es fija, pero los estados de captura, redacción, evaluación, rechazo y
persistencia tienen efectos distintos que deben auditarse.

### 2. ¿Por qué el Planner de Codex no llama a un modelo?

Porque no existe una decisión abierta que justifique el coste y la latencia: las
siete fuentes, el horizonte inicial y los límites ya están definidos. El Planner
materializa ese contrato de forma determinista. OpenAI/Ollama/vLLM sí activan el
modelo barato del rol; su JSON se valida y el mismo contrato actúa como fallback.

### 3. ¿Cómo funciona el filtro del perfil Codex sin Luna?

Normaliza términos, aplica alertas del usuario, palabras clave y taxonomía de
activos, limita volumen y conserva evidencia cuando existe duda. Es reproducible
y testeable. En los perfiles HTTP se aplica después el filtro generativo del
modelo barato; si falla, se conserva el conjunto seguro anterior. Luna se
reserva por defecto para el chat RAG en Codex.

### 4. ¿Por qué tres tiers Codex si el grafo tiene cuatro nodos?

Sol produce la síntesis de mayor valor, Terra evalúa con un rol distinto y Luna
responde preguntas breves sobre contexto recuperado. Planner/filtro son
deterministas solo en este perfil; OpenAI y los backends abiertos sí asignan el
modelo barato a ambos roles.

### 5. ¿Por qué Codex CLI en lugar de una API?

Es una decisión posterior y explícita del perfil de entrega: reutilizar la
cuenta Codex del operador y no gestionar API keys. El adaptador aporta ejecución
no interactiva, JSONL, uso y salida estructurada. Aceptamos mayor latencia de
proceso y menor idoneidad para multi-tenancy; producción debe reevaluar identidad
y SLA.

### 6. ¿Cómo se autentica sin una clave en `.env`?

`codex login` establece una sesión ChatGPT/Codex. En Docker se usa device auth y
el caché queda en un volumen `CODEX_HOME`. El frontend exige que
`codex login status` confirme ChatGPT y rechaza el modo API key.

### 7. ¿Qué aporta el perfil de permisos?

Es una política fail-closed que reúne filesystem y red en una única frontera de
mínimo privilegio. Permite leer únicamente el directorio aislado necesario,
deniega repositorio y credenciales, elimina herencia de entorno y evita
aprobaciones interactivas. No se mezcla con el mecanismo heredado `--sandbox`.

### 8. ¿Ese volumen de autenticación es un vault?

No. Es un secreto persistente adecuado para una demo privada. Acceso al daemon
Docker implica riesgo potencial sobre el volumen. Producción exige identidad
dedicada, rotación, revocación, auditoría y aislamiento.

### 9. ¿Qué evita URLs inventadas?

El Executor recibe un catálogo cerrado. Una barrera local exige que cada cita
coincida exactamente con organismo y URL capturados. Una cita desconocida
impide aprobar el resultado.

### 10. ¿El Judge garantiza verdad jurídica?

No. Verifica una rúbrica y se combina con reglas locales de trazabilidad. Una
interpretación puede seguir siendo incorrecta; el especialista decide vigencia
y aplicabilidad.

### 11. ¿Qué ocurre con un informe rechazado?

No se guarda como informe, no se exporta, no alimenta memoria y no aparece como
completado. La ejecución y el motivo permanecen en Observabilidad para auditoría.

### 12. ¿Los embeddings locales son realmente semánticos?

Son hashing lexical enriquecido, no un transformer. Capturan términos,
bigramas, variantes ortográficas y subpalabras de forma reproducible. Su límite
ante paráfrasis debe medirse con Recall@k.

### 13. ¿Cómo se contabilizan tokens?

El cliente consume `turn.completed.usage` del JSONL y normaliza entrada, salida,
caché y razonamiento. Planner y filtro deterministas no generan llamadas ni
tokens de modelo.

### 14. ¿Por qué el coste Codex es N/A?

La sesión ChatGPT/Codex no entrega una tarifa por turno atribuible. Los campos
numéricos de compatibilidad no son una factura. La UI muestra N/A y, si negocio
lo desea, calcula aparte una imputación interna basada en coste mensual y
volumen esperado.

### 15. ¿Qué ocurre si falla el proveedor?

Se bloquean las capacidades generativas afectadas: informe, Judge o chat. Siguen
operativos login, dashboard, scraping, alertas, persistencia e índice local. Un
fallback interno no se presenta como respuesta generativa aprobada. El backend
puede cambiarse por configuración entre Codex, OpenAI, Ollama y vLLM.

### 16. ¿Cumple el PDF oficial?

Sí en alcance funcional: agente orquestado, fuentes chilenas, dashboard,
alertas, RAG, citas, observabilidad y documentación. El PDF oficial es agnóstico
respecto a un proveedor o familia concreta. La ruta OpenAI conserva GPT-4o mini
para planificación/filtrado y GPT-4o para redacción, mientras que la factoría
demuestra portabilidad a Codex y modelos abiertos.

### 17. ¿Cuál es el mayor riesgo al escalar?

Usar una sesión interactiva como identidad compartida y ejecutar un proceso CLI
por turno. Antes de escalar hay que resolver identidad de servicio, aislamiento,
concurrencia, límites, revocación y observabilidad corporativa.

### 18. ¿Cómo medirías valor de negocio?

Tiempo ahorrado por analista, recall de publicaciones relevantes, demora entre
publicación y revisión, correcciones por informe, tasa de aprobación, clics en
evidencia, alertas accionables, tokens por informe y tasa de ejecuciones
completadas.

## Afirmaciones que deben evitarse

No decir:

- “Planner, filtro, Executor y Judge llaman siempre a un modelo”.
- “Luna planifica y filtra el informe estándar”.
- “Codex usa `--sandbox read-only`”.
- “Codex es gratis” o “el coste total es cero”.
- “USD/CLP coincide con la factura”.
- “La imputación interna es una tarifa del proveedor”.
- “Sin Codex se genera igualmente el informe”.
- “El informe rechazado queda en historial”.
- “Las citas demuestran que la interpretación es correcta”.
- “El Judge elimina alucinaciones”.
- “Siempre recupera las siete fuentes”.
- “Está listo para producción” o “es multi-tenant”.
- “El PDF exige una familia GPT o una API concreta”.

Sustituir por:

- “En Codex, Planner y filtro son deterministas; en los perfiles HTTP se activa
  el modelo barato con fallback”.
- “Sol redacta, Terra evalúa y Luna responde RAG”.
- “Perfil de permisos estricto, aislado y sin configuración heredada”.
- “Coste atribuible N/A; imputación interna opcional y separada”.
- “Solo los informes aprobados se persisten y alimentan memoria”.
- “Controles para reducir y detectar contenido no respaldado”.
- “MVP end-to-end con límites y roadmap explícitos”.

## Cierre de 20 segundos

> CENtinela demuestra que la IA regulatoria aporta valor cuando la síntesis no
> oculta la evidencia. El código hace determinista lo repetible; Codex se reserva
> para razonar sobre el catálogo; Terra decide si el resultado puede avanzar; el
> especialista conserva la decisión final.
