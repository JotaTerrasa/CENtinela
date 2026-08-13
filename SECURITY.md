# Seguridad y modelo de amenazas

## Alcance de esta entrega

CENtinela es un MVP local para analizar información regulatoria pública. La
imagen Docker expone Streamlit únicamente en `127.0.0.1:8501`; no debe publicarse
directamente en Internet ni tratarse como un sistema de decisión jurídica. Los
datos internos de Grenergy quedan explícitamente fuera de alcance.

## Activos protegidos

- Sesión ChatGPT administrada por Codex CLI y claves/gateway de proveedores HTTP.
- Contraseñas y sesiones de usuarios de la aplicación.
- Base SQLite, índice Chroma y artefactos de informes.
- Integridad de las citas, URLs y métricas de ejecución.

## Amenazas principales y controles

| Amenaza | Control implementado |
|---|---|
| Prompt injection dentro de una web oficial | El contenido capturado se trata como evidencia, no como instrucciones; las fuentes pertenecen a un registro cerrado y las URLs citadas se validan contra el catálogo de la ejecución. |
| Un modelo intenta explorar el repositorio o las credenciales | Cada `codex exec` usa configuración estricta, ejecución efímera y un perfil de permisos dedicado: deniega `/app`, el proyecto y `CODEX_HOME`; solo reabre en lectura un directorio de trabajo vacío. |
| Exfiltración por red o variables de entorno | En Codex, las herramientas del agente no tienen red y el shell recibe solo un `PATH` mínimo. Los proveedores HTTP deben quedar restringidos por egress allowlist y gateway en el despliegue. |
| Endpoint Ollama/vLLM expuesto | El perfil local enlaza Ollama a loopback; producción exige red privada, gateway autenticado, TLS, cuotas y NetworkPolicy. |
| Clave API filtrada en configuración o health checks | Las claves se modelan como `SecretStr`, no forman parte de `public_dict`, no se pasan a la caché de UI y los errores del SDK se sanitizan. |
| Configuración local menos restrictiva | Se ignoran la configuración y reglas del usuario, se aplica `approval_policy="never"` y se fuerza autenticación ChatGPT en cada llamada. |
| Fuga de secretos por argumentos o logs | El prompt viaja por `stdin`; credenciales, contraseñas y contenido del fichero de autenticación no se leen ni se registran. Los errores se sanitizan antes de persistirse. |
| Cita inventada o informe débil | Una barrera determinista comprueba todas las afirmaciones materiales y URLs; después Terra actúa como LLM-as-Judge. Los informes rechazados se etiquetan como tales y no se reutilizan como memoria aprobada. |
| Fuerza bruta o robo de la base local | Las contraseñas se almacenan con PBKDF2-HMAC y sal individual; el contenedor corre como usuario no root. En producción se sustituirá por SSO y un gestor de secretos. |
| Dependencia vulnerable de Chroma | Se fija `chromadb==0.6.3`, fuera del rango afectado por CVE-2026-45829, y Chroma funciona embebido: no existe servidor Chroma expuesto. La telemetría se desactiva en `PersistentClient`, PostHog se fija a una versión compatible y un volumen nuevo evita modificar índices legacy 1.x. |

## Límites conocidos

- La autenticación propia de Streamlit es deliberadamente simple y local. No
  incorpora MFA, recuperación de cuenta, bloqueo por intentos ni revocación
  centralizada.
- La sesión ChatGPT reside en un volumen Docker. Quien administre Docker en el
  host debe considerarse un administrador de confianza.
- Ollama no incorpora autenticación en su API local. El puerto de demo solo se
  enlaza a `127.0.0.1` y nunca debe abrirse directamente a Internet.
- Los modelos abiertos y sus imágenes requieren revisión de licencia, SBOM,
  procedencia de pesos y fijación por digest antes de producción.
- Una cita correcta prueba procedencia, no que la interpretación sea jurídicamente
  concluyente. Todo informe requiere revisión humana antes de una decisión.
- El bloqueo de red se aplica a las herramientas que pudiera invocar el agente;
  Codex CLI conserva la conectividad imprescindible con el servicio de OpenAI.

## Paso a producción

Antes de un despliegue corporativo: SSO/OIDC y RBAC, secretos gestionados,
egress allowlist, base de datos administrada, cifrado y backups, rate limiting,
retención y borrado auditables, escaneo SBOM/SCA continuo, observabilidad
centralizada y revisión de privacidad/DPIA.

## Reporte responsable

No abras un issue público con credenciales ni datos personales. Comunica el
hallazgo al propietario del repositorio con pasos de reproducción mínimos y sin
incluir secretos.
