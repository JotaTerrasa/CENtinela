# Seguridad y modelo de amenazas

## Alcance del proyecto

CENtinela es un MVP local para analizar información regulatoria pública. La
imagen Docker expone Streamlit únicamente en `127.0.0.1:8501`; no debe publicarse
directamente en Internet ni tratarse como un sistema de decisión jurídica. Los
datos internos o privados de terceros quedan explícitamente fuera de alcance.

La única excepción publicable sin completar el hardening productivo es
`PUBLIC_DEMO_MODE`: un replay inmutable que verifica y carga en memoria
artefactos públicos de `docs/demo`. No inicializa SQLite, no prepara rutas de
runtime ni crea identidades por visitante. Bloquea en servidor scraping,
indexación y llamadas generativas, y
rechaza secretos de proveedores y credenciales bootstrap. La UI lo etiqueta
como evidencia histórica y marca la alerta aparte como simulación UI. No es un
entorno para introducir información confidencial ni una autorización para
exponer el modo interactivo del MVP.

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
| Exposición accidental de la demo | El modo público no ofrece registro/login ni persistencia por visitante, rechaza credenciales de modelos y bootstrap, valida hashes de los artefactos y se niega a arrancar con `APP_ENV=production`. |
| Avisos de seguridad de Chroma | Se fija `chromadb==0.6.3`, fuera del rango de CVE-2026-45829, y Chroma funciona exclusivamente mediante `PersistentClient`: no existe servidor Chroma, API `/api/v2`, autenticación Chroma ni multi-tenancy expuestos. Los avisos CVE-2026-45830, CVE-2026-45831 y CVE-2026-45833 afectan esas rutas de servidor y no publican todavía una versión corregida; CI ignora únicamente esos identificadores y mantiene visibles las excepciones. Nunca se debe exponer esta dependencia como servicio. |

### Excepciones temporales de auditoría

`pip-audit` excluye de su código de salida tres avisos concretos de Chroma, sin
silenciar otros hallazgos:

- [CVE-2026-45830](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr):
  autorización entre tenants para usuarios autenticados del servidor;
- [CVE-2026-45831](https://github.com/advisories/GHSA-xph7-9rjv-w5fr):
  alcance de permisos en `SimpleRBACAuthorizationProvider`;
- [CVE-2026-45833](https://github.com/advisories/GHSA-36p7-vc44-83pf):
  inyección mediante actualización de colecciones en la API `/api/v2`.

El MVP no inicia ni expone esas superficies. Los avisos deben revisarse en cada
actualización de dependencias y las excepciones se eliminarán cuando exista una
versión corregida compatible. Si se separa Chroma como servicio, el despliegue
queda bloqueado hasta migrar a una versión corregida y validar aislamiento,
autenticación y autorización.

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
