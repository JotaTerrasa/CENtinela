# Declaración de uso de inteligencia artificial

## Alcance

Durante el desarrollo de CENtinela se utilizó **OpenAI Codex** como herramienta
de ingeniería asistida. Su participación incluyó generación y refactorización de
código, diseño de pruebas, revisión de seguridad y reproducibilidad, redacción de
documentación y validación automatizada de la interfaz local.

El uso de IA no sustituye la responsabilidad sobre la entrega. Las decisiones de
arquitectura, los límites conocidos y los criterios de aceptación quedan
expuestos en `DECISIONES_TECNICAS.md`; el comportamiento entregado se comprueba
mediante pruebas deterministas, ejecución Docker y una prueba end-to-end con
fuentes públicas.

## IA utilizada dentro del producto

CENtinela utiliza Codex CLI autenticado mediante ChatGPT como único runtime
generativo:

- planificación y filtrado deterministas por defecto, sin consumir una llamada
  generativa para decisiones repetitivas;
- Luna para las consultas RAG;
- Sol para la síntesis del informe ejecutivo;
- Terra para la evaluación LLM-as-Judge.

La recuperación vectorial previa a la generación emplea embeddings hash locales
y ChromaDB. Las URLs no las genera el modelo: proceden del catálogo capturado y
se validan localmente antes de aceptar una respuesta o un informe.

## Datos y privacidad

El desarrollo y las pruebas utilizan únicamente código del proyecto y contenido
regulatorio público de organismos chilenos. No se utilizaron datos internos de
Grenergy. Las credenciales de ChatGPT/Codex se mantienen fuera del repositorio,
del ZIP y de las trazas de aplicación.

## Controles de calidad aplicados

- Suite automatizada y compilación de módulos.
- Construcción y healthcheck de la imagen Docker.
- Verificación de enlaces y citas contra el catálogo de evidencia.
- Evaluación determinista y LLM-as-Judge del informe.
- Revisión visual de login, dashboard, alertas, informe, RAG, observabilidad y
  arquitectura.
- Escaneo de secretos y exclusión de datos, cachés y autenticación del paquete de
  entrega.

## Limitación

Las salidas generativas pueden contener interpretaciones incompletas aunque sus
citas sean válidas. CENtinela es una ayuda a la inteligencia regulatoria: la
aplicabilidad jurídica, técnica y económica debe ser revisada por un especialista.
