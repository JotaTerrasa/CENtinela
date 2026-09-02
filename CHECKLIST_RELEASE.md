# Checklist de preparación de release

Runbook de cierre para publicar CENtinela sin confundir un artefacto validado
con una promesa de producción. Los checks se completan en orden: un
gate fallido detiene el etiquetado de la release.

Las casillas de este archivo son un procedimiento reutilizable, no el estado de
la última ejecución. El cumplimiento funcional vigente se consulta en
[`TRAZABILIDAD_CAPACIDADES.md`](TRAZABILIDAD_CAPACIDADES.md), y la evidencia concreta de
cada versión en su release de GitHub.

## Estado de referencia

Antes de cada publicación, verificar estas superficies del proyecto:

| Evidencia | Comprobación |
|---|---|
| Repositorio remoto | Apunta a [`JotaTerrasa/CENtinela`](https://github.com/JotaTerrasa/CENtinela). |
| Demo pública | Carga en modo de solo lectura y sin credenciales. |
| Documentación técnica | README, arquitectura, stack, trazabilidad y seguridad coinciden con el código. |
| Historial de versiones | La nueva release parte de un commit de `main` con CI verde. |
| Evidencia de interfaz | Capturas, manifiesto y artefactos están descritos en [`docs/demo/`](docs/demo/README.md). |

Cada release debe señalar a un commit que contenga también los diagramas, el
stack consolidado, la matriz de capacidades, [`GUION_DEMO.md`](GUION_DEMO.md)
y este checklist. Los tags existentes no se mueven ni se sobrescriben.

## Gate 1 · Alcance y repositorio

- [ ] `git status --short` no muestra cambios ajenos o sin revisar.
- [ ] La rama `main` local coincide con `origin/main`.
- [ ] El repositorio remoto pertenece a `JotaTerrasa`.
- [ ] README y documentación no prometen producción HA, asesoramiento jurídico
      ni equivalencia entre modelos.
- [ ] El titular ha decidido y declarado las condiciones de uso del repositorio;
      no se añade una licencia open source por defecto sin esa decisión.
- [ ] El historial no contiene `.env`, claves, sesiones, SQLite, índices Chroma
      ni informes privados.
- [ ] El commit que se va a etiquetar es exactamente el que se ha revisado.

Comandos de evidencia:

```bash
git status --short
git remote -v
git rev-parse HEAD
git rev-parse origin/main
git ls-files | grep -E '(^|/)(\.env|auth\.json|.*\.db|.*\.sqlite3?)$' || true
```

El resultado esperado del último comando es vacío. `.env.example` sí debe estar
versionado: contiene nombres de configuración, no credenciales reales.

## Gate 2 · Calidad reproducible

Ejecutar en Python 3.12 con `requirements-dev.txt` instalado:

```bash
python -m compileall -q app.py core scrapers agent rag scripts
ruff check .
pip-audit -r requirements.txt --progress-spinner off \
  --ignore-vuln CVE-2026-45830 \
  --ignore-vuln CVE-2026-45831 \
  --ignore-vuln CVE-2026-45833
pytest -q
cp .env.example .env
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.ollama.yml config --quiet
docker compose \
  -f docker-compose.yml \
  -f docker-compose.ollama.yml \
  -f docker-compose.ollama-gpu.yml \
  config --quiet
```

- [ ] Compilación correcta.
- [ ] Ruff sin incidencias.
- [ ] Auditoría sin hallazgos no documentados; las únicas excepciones son los
      tres avisos de servidor Chroma justificados en [`SECURITY.md`](SECURITY.md).
- [ ] Suite completa sin fallos; registrar el recuento emitido por esa
      ejecución, no copiar un número histórico.
- [ ] Los tres perfiles Compose son válidos.
- [ ] La ejecución de GitHub Actions correspondiente al commit final está en
      verde.

Si el número de tests de `docs/demo/validation-summary.json` es inferior al de
la suite actual, no modificar la evidencia histórica: ese JSON describe la
ejecución visual capturada en su fecha. En la release se informa por separado el
recuento del commit final.

## Gate 3 · Smoke funcional y evidencia

- [ ] El contenedor aparece healthy en `docker compose ps`.
- [ ] `/_stcore/health` responde correctamente.
- [ ] Login y registro local funcionan con un usuario desechable.
- [ ] Dashboard muestra publicaciones y permite abrir una URL oficial.
- [ ] Una regla de alerta se guarda y se recupera para el mismo usuario.
- [ ] Existe al menos un informe aprobado con citas navegables.
- [ ] El dictamen muestra puntuación y validación determinista.
- [ ] El Chat RAG responde desde el corpus y lista fuentes originales.
- [ ] Observabilidad muestra proveedor, modelo, rol, latencia y tokens o el
      estado explícito `not_reported`.
- [ ] La UI diferencia API, suscripción y cómputo self-hosted.
- [ ] `PUBLIC_DEMO_MODE` recorre las seis vistas sin crear una SQLite ni aceptar
      secretos; las acciones externas fallan también en servidor.
- [ ] Los exports del replay conservan tipo, origen, fecha de validación y
      SHA-256, y las capturas `.png` tienen magic bytes PNG.
- [ ] El panel Arquitectura refleja el proveedor realmente configurado.
- [ ] La URL pública, si se utiliza, muestra el banner **Demo pública**, no
      habilita llamadas generativas y no solicita login ni credenciales.

Evidencia mínima conservada:

- [`sample-report.md`](docs/demo/sample-report.md): informe aprobado.
- [`sample-report.json`](docs/demo/sample-report.json): dictamen y telemetría.
- [`sample-rag.json`](docs/demo/sample-rag.json): respuesta y fuentes.
- [`validation-summary.json`](docs/demo/validation-summary.json): condiciones de
  la captura.
- Nueve capturas enumeradas en [`docs/demo/README.md`](docs/demo/README.md).

## Gate 4 · Seguridad y privacidad

- [ ] La demo no comparte códigos device auth, tokens ni variables secretas.
- [ ] `OPENAI_API_KEY`, claves de gateway y sesiones Codex se inyectan en
      runtime; no están en imagen, Git, capturas o ZIP.
- [ ] Ollama se enlaza a loopback en el perfil local y no se presenta como API
      autenticada.
- [ ] La sesión ChatGPT/Codex se describe como válida para la demo, no como
      identidad de servicio multi-tenant.
- [ ] Se comprueba que el root filesystem es read-only, se eliminan
      capabilities y se activa `no-new-privileges`.
- [ ] Las cuentas y contraseñas de demo no se reutilizan en otros servicios.
- [ ] Ningún informe rechazado se presenta como resultado aprobado.
- [ ] Se mantiene el disclaimer: la cita acredita procedencia, no validez
      jurídica ni aplicabilidad a una inversión concreta.

Consultar [`SECURITY.md`](SECURITY.md) antes de compartir el repositorio con
terceros.

## Gate 5 · Ensayo de demostración

- [ ] Dos ensayos consecutivos de
      [`GUION_DEMO.md`](GUION_DEMO.md) duran entre 6:30 y 7:30.
- [ ] El Plan B funciona con el portátil sin Internet.
- [ ] Se han abierto previamente dos fuentes oficiales.
- [ ] El presentador puede explicar por qué LangGraph, por qué un Judge no
      garantiza verdad y por qué Codex no es la identidad cloud objetivo.
- [ ] Se han revisado las preguntas de
      [`REVISION_ARQUITECTURA.md`](REVISION_ARQUITECTURA.md).
- [ ] No quedan notificaciones, pestañas personales o terminales con secretos.
- [ ] El cierre distingue claramente MVP validado de roadmap productivo.

## Gate 6 · Release versionada

Crear una release solo después de que el commit final esté en `main` y su CI
esté en verde.

```bash
git switch main
git pull --ff-only origin main
git status --short
git tag --list 'v*'
gh run list --repo JotaTerrasa/CENtinela --branch main --limit 3
```

- [ ] `git status` está limpio.
- [ ] `HEAD` coincide con el commit verde de `origin/main`.
- [ ] El nuevo tag no existe antes de crearlo.
- [ ] El título sigue el formato `CENtinela vX.Y.Z — <resumen de cambios>`.
- [ ] La release no contiene bases de datos, credenciales, volúmenes ni índices.
- [ ] Se adjunta, si se distribuye un ZIP manual, su SHA-256.
- [ ] La release enlaza README, guía de demo, revisión de arquitectura,
      arquitectura cloud, seguridad y decisiones técnicas.

Texto breve recomendado para una release:

> Release de CENtinela: captura resiliente de siete fuentes regulatorias
> chilenas, dashboard y alertas por usuario, informe diario Planner–Executor
> con LLM-as-Judge, RAG trazable, observabilidad de tokens y costes, y backends
> intercambiables Codex, OpenAI, Ollama y vLLM. Incluye Docker Compose, pruebas
> automatizadas, evidencia visual y documentación de seguridad y evolución
> cloud. Es un MVP técnico; producción requiere SSO/RBAC, identidad de servicio,
> PostgreSQL, workers, secret manager y auditoría centralizada.

Las versiones publicadas se mantienen en
<https://github.com/JotaTerrasa/CENtinela/releases>.

## Paquete público de portfolio

Al compartir el proyecto, utilizar únicamente:

1. Repositorio: <https://github.com/JotaTerrasa/CENtinela>.
2. Releases versionadas: <https://github.com/JotaTerrasa/CENtinela/releases>.
3. Demo pública: <https://centinela-regulatory.streamlit.app/?embed=true>.
4. Instrucción de arranque: sección Docker del README.
5. Una frase de alcance: MVP ejecutable y trazable, no plataforma HA ni
   asesoramiento jurídico.

No distribuir `.env`, credenciales, bases de datos, índices ni volúmenes de
autenticación. GitHub ya ofrece ZIP y tarball por versión.

## Descripción breve para compartir

> CENtinela es un proyecto personal público de inteligencia regulatoria para el
> Sistema Eléctrico Nacional de Chile. El MVP integra captura de fuentes
> oficiales, dashboard y alertas, informe diario orquestado con LangGraph y
> LLM-as-Judge, chat RAG con citas y observabilidad de tokens, latencia y costes.
> Incluye una demo de solo lectura, ejecución Docker, pruebas automatizadas y
> una propuesta explícita de evolución cloud.

## Últimos diez minutos antes de publicar

- [ ] Abrir repositorio, demo y release en una ventana privada.
- [ ] Confirmar que el README se renderiza y que sus enlaces relativos funcionan.
- [ ] Confirmar que Actions muestra verde para el tag o commit publicado.
- [ ] Descargar el ZIP del tag y verificar que arranca desde un directorio
      limpio siguiendo el README.
- [ ] Probar cualquier enlace de vídeo sin sesión iniciada.
- [ ] Revisar título, descripción, enlaces y artefactos adjuntos.
- [ ] Publicar una sola versión y conservar exactamente el SHA etiquetado.
