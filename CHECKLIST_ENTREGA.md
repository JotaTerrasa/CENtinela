# Checklist de entrega profesional

Runbook de cierre para publicar y enviar CENtinela sin confundir un artefacto
validado con una promesa de producción. Los checks se completan en orden: un
gate fallido detiene el etiquetado de la release.

## Estado auditado de partida

Auditoría realizada el 13 de agosto de 2026 sobre el repositorio público
[`JotaTerrasa/CENtinela`](https://github.com/JotaTerrasa/CENtinela):

| Evidencia | Estado observado | Referencia |
|---|---|---|
| PR multi-provider | Fusionada | [PR #1](https://github.com/JotaTerrasa/CENtinela/pull/1) |
| Baseline de `main` | `72fb23e7d593094e231a702c4931e417703eee15` | [commit](https://github.com/JotaTerrasa/CENtinela/commit/72fb23e7d593094e231a702c4931e417703eee15) |
| CI de la fusión | Correcta | [run 31743116440](https://github.com/JotaTerrasa/CENtinela/actions/runs/31743116440) |
| Release GitHub | No existía al iniciar esta auditoría | Debe crearse después del último commit documental |
| Evidencia de interfaz | Disponible | [`docs/demo/`](docs/demo/README.md) |

La release final debe señalar a un commit que contenga también
[`GUION_DEMO.md`](GUION_DEMO.md) y este checklist; por tanto, será descendiente
del baseline, no necesariamente el propio `72fb23e`.

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
pip-audit -r requirements.txt --progress-spinner off
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
- [ ] Auditoría sin vulnerabilidades conocidas en dependencias de runtime.
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

## Gate 5 · Ensayo de presentación

- [ ] Dos ensayos consecutivos de
      [`GUION_DEMO.md`](GUION_DEMO.md) duran entre 6:30 y 7:30.
- [ ] El Plan B funciona con el portátil sin Internet.
- [ ] Se han abierto previamente dos fuentes oficiales.
- [ ] El presentador puede explicar por qué LangGraph, por qué un Judge no
      garantiza verdad y por qué Codex no es la identidad cloud objetivo.
- [ ] Se han ensayado las preguntas de [`DEFENSA_CTO.md`](DEFENSA_CTO.md).
- [ ] No quedan notificaciones, pestañas personales o terminales con secretos.
- [ ] El cierre distingue claramente MVP validado de roadmap productivo.

## Gate 6 · Release `v1.0.0`

Crear la release solo después de que el commit final esté en `main` y su CI esté
en verde.

```bash
git switch main
git pull --ff-only origin main
git status --short
git tag --list v1.0.0
gh run list --repo JotaTerrasa/CENtinela --branch main --limit 3
```

- [ ] `git status` está limpio.
- [ ] `HEAD` coincide con el commit verde de `origin/main`.
- [ ] El tag `v1.0.0` no existe antes de crearlo.
- [ ] El título es `CENtinela v1.0.0 — MVP de inteligencia regulatoria`.
- [ ] La release no contiene bases de datos, credenciales, volúmenes ni índices.
- [ ] Se adjunta, si se distribuye un ZIP manual, su SHA-256.
- [ ] La release enlaza README, guía de demo, defensa, arquitectura cloud,
      seguridad y decisiones técnicas.

Texto breve recomendado para la release:

> Primera entrega evaluable de CENtinela: captura resiliente de siete fuentes
> regulatorias chilenas, dashboard y alertas por usuario, informe diario
> Planner–Executor con LLM-as-Judge, RAG trazable, observabilidad de tokens y
> costes, y backends intercambiables Codex, OpenAI, Ollama y vLLM. Incluye Docker
> Compose, pruebas automatizadas, evidencia visual y documentación de seguridad
> y evolución cloud. Es un MVP técnico; producción requiere SSO/RBAC, identidad
> de servicio, PostgreSQL, workers, secret manager y auditoría centralizada.

Enlace esperado tras la publicación:
<https://github.com/JotaTerrasa/CENtinela/releases/tag/v1.0.0>.

## Paquete que recibe el evaluador

El mensaje de entrega debe contener únicamente:

1. Repositorio: <https://github.com/JotaTerrasa/CENtinela>.
2. Release versionada: <https://github.com/JotaTerrasa/CENtinela/releases/tag/v1.0.0>.
3. Instrucción de arranque: sección Docker del README.
4. Guion o vídeo de demo, si el canal de entrega lo admite.
5. Una frase de alcance: MVP ejecutable y trazable, no plataforma HA ni
   asesoramiento jurídico.

No adjuntar `.env`, credenciales, base de datos de la demo ni el volumen de
autenticación. GitHub ya ofrece ZIP y tarball del tag, por lo que un ZIP manual
solo es necesario si la convocatoria lo exige.

## Mensaje de envío listo para adaptar

**Asunto:** Prueba técnica AI Architect — CENtinela — Jaime Terrasa

> Hola,
>
> Comparto CENtinela, mi propuesta de inteligencia regulatoria para el Sistema
> Eléctrico Nacional de Chile. El MVP integra captura de fuentes oficiales,
> dashboard y alertas, informe diario orquestado con LangGraph y LLM-as-Judge,
> chat RAG con citas y observabilidad de tokens, latencia y costes.
>
> Repositorio: https://github.com/JotaTerrasa/CENtinela
>
> Release reproducible:
> https://github.com/JotaTerrasa/CENtinela/releases/tag/v1.0.0
>
> La solución incluye instrucciones Docker, pruebas automatizadas, evidencia de
> una ejecución validada y una propuesta de evolución cloud con OpenAI, Ollama
> o vLLM. En el README se detallan tanto las decisiones como los límites del MVP.
>
> Quedo disponible para presentar la demo y profundizar en arquitectura,
> seguridad, evaluación y estrategia de costes.
>
> Un saludo,
> Jaime Terrasa

No añadir un enlace de vídeo hasta comprobar permisos en una ventana privada.

## Últimos diez minutos antes de enviar

- [ ] Abrir repositorio y release en una ventana privada.
- [ ] Confirmar que el README se renderiza y que sus enlaces relativos funcionan.
- [ ] Confirmar que Actions muestra verde para el tag o commit entregado.
- [ ] Descargar el ZIP del tag y verificar que arranca desde un directorio
      limpio siguiendo el README.
- [ ] Probar cualquier enlace de vídeo sin sesión iniciada.
- [ ] Revisar destinatario, asunto, firma y adjuntos.
- [ ] Enviar una sola versión, registrar fecha/hora y conservar exactamente el
      SHA entregado.
