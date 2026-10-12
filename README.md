# Index scrapers

Extractores Python 3.12 para Idealista, Fotocasa, Pisos.com y Habitaclia. `httpx` descarga HTTP y `selectolax` lee HTML y datos estructurados. Las imágenes se sirven desde el portal; el runner calcula su hash perceptual en memoria, sin guardarlas. El repositorio presupone el acuerdo de extracción y republicación descrito en la especificación de Index.

## Estado verificado

Hay pruebas sobre tarjetas reales de los cuatro portales y fichas reales de Idealista, Fotocasa y Habitaclia. Los HTML `.synthetic.html` son ejemplos inventados expresamente identificados; no demuestran acceso a producción. Las capturas `tests/fixtures/captures/` incluyen procedencia y fecha.

El 12 de octubre de 2026 Fotocasa y Habitaclia devolvieron HTML útil mediante HTTP. Idealista devolvió un desafío HTTP 403 y Pisos.com cerró la conexión HTTP. Las tarjetas de estos dos portales se verificaron mediante capturas reales del navegador proporcionadas durante el desarrollo. El runner detecta los bloqueos y avisa: no resuelve CAPTCHA, no exporta cookies ni reutiliza perfiles de navegador. La cobertura operativa de los cuatro portales sigue pendiente del acceso HTTP autorizado.

## Uso local

```sh
python -m pip install '.[test,images]'
python -m pytest -q
python -m index_scrapers.cli initial --operation rent --no-delivery
python -m index_scrapers.cli snapshot
```

Para entregar a la API se definen `INGEST_URL` (URL HTTPS completa de `/ingest`) e `INGEST_TOKEN` en el entorno, o en GitHub Actions Secrets. No se guardan en archivos ni en argumentos. `python -m index_scrapers.cli deliver` entrega la cola ya persistida sin volver a extraer. El contrato privado completo vive en `index-web/docs/codex/INGEST.md`.

La CLI también admite `discover`, `prices`, `withdrawals`, `health`, `restore` y `snapshot`. `--max-pages` limita páginas por destino; `--limit` limita fichas revisadas por ejecución. Un informe `health.json` recoge cero resultados, datos incompletos y errores por tipo, sin cuerpos HTTP ni secretos. Un error da salida 1; la cola conserva lo pendiente.

## Catálogo, deduplicación y entrega

`state/catalog.sqlite` contiene el catálogo de todos los portales, hashes, comprobaciones, cursores de carga inicial y cola. SQLite agrupa cambio de catálogo y nueva revisión pendiente en la misma transacción; WAL y copias mediante la API de backup evitan copiar un fichero a medio escribir.

La deduplicación compara cada candidato con el catálogo persistido completo y conserva el ULID del anuncio conocido. Sólo envía cambios de contenido, precio o estado. Actualizar `last_seen` o `last_checked` localmente no crea escrituras en D1: allí representan la última observación enviada por un cambio. La evaluación de deduplicación requiere la muestra y revisión independiente indicadas por el proyecto; las pruebas sintéticas no sustituyen esa medición.

La API recibe como máximo cinco listings por lote. Sólo `accepted:[{id,outcome:'written'|'noop'}]` elimina las revisiones enviadas; `pending`, errores, respuestas inválidas y el límite diario permanecen en cola. Varias revisiones de un anuncio se entregan por orden para conservar el historial de precios. Un ack de una revisión antigua nunca elimina una más reciente.

Un anuncio se retira después de dos HTTP 404/410 consecutivos, incluso entre runs diferentes. HTTP 403/429/5xx, errores de red y desafíos no retiran el anuncio y rompen esa racha. Con una comprobación diaria, dos negativas pueden tardar 24–48 horas: el objetivo de retirada en menos de 24 horas de la especificación requiere resolver esta discrepancia y medir la cobertura de los lotes, sin cambiarla silenciosamente.

## Actions y persistencia

Los workflows `scrape.yml` y `health.yml` comparten un único grupo de concurrencia, `queue: max` y `cancel-in-progress: false`. El workflow reutilizable `run.yml` sólo opera en la rama por defecto. Los PR ejecutan exclusivamente tests y no publican estado.

Cada run restaura el último snapshot de la rama por defecto y del propio repositorio, comprueba SHA-256 e integridad SQLite y aborta si falta, está corrupto o caducó. No vuelve a un estado viejo ni crea un catálogo vacío automáticamente. En la primera ejecución manual de `Collect Index` se activa `initialize_state`; sólo se permite si nunca hubo un snapshot. Para una recuperación posterior se necesita una copia válida, sin reinicializar la cola.

El orden es: extraer sin entregar → publicar snapshot PRE → entregar sólo si PRE se publicó → publicar snapshot FINAL. Se guarda estado también tras errores de extracción. Si falla FINAL después de que la API escriba, el siguiente run reenvía exactamente la cola de PRE, con los mismos IDs, y la API confirma `noop`. Un run que muera antes de publicar PRE puede perder observaciones aún no persistidas, pero no entrega nuevos IDs sin una copia recuperable.

Se retienen tres snapshots comprimidos, con siete días de retención; los anteriores se eliminan sólo después de publicar el nuevo. El tamaño del catálogo sin comprimir se limita a 64 MiB: superar esa capacidad detiene la publicación y exige revisar almacenamiento y volumen; no se descartan anuncios ni pendientes. Los artefactos comparten la cuota gratuita de la cuenta. La caducidad exige recuperar estado explícitamente. Estas limitaciones impiden prometer cobertura de toda España sin medir volumen real.

Frecuencias configuradas: novedades de alquiler cada diez minutos, venta cada treinta; precios de alquiler cada seis horas y venta cada veinticuatro; retiradas y salud cada veinticuatro horas. Los minutos se desplazan de `:00`. GitHub puede retrasar o descartar ejecuciones, por lo que las frecuencias no constituyen un SLA. [Concurrencia de Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency), [eventos programados](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [artefactos](https://docs.github.com/en/rest/actions/artifacts).

Si hay cero resultados, campos anómalos o un fallo de restauración/entrega, se abre un issue de salud. Se reutiliza un único issue abierto para evitar mensajes repetidos; el enlace al run permite consultar cada diagnóstico.

## Carga inicial y ampliación

`config/targets.json` contiene rutas verificadas y códigos INE de Madrid, Barcelona, Valencia, Sevilla, Málaga y Bilbao. Fotocasa y Habitaclia tienen alquiler y venta de las seis; Idealista y Pisos.com conservan destinos de alquiler de Madrid como comprobaciones de los bloqueos reales. `config/targets.source.json` conserva los intentos, códigos observados y correcciones de formato (por ejemplo, `8019` → `08019`, `buy` → `sale`), sin presentar los intentos fallidos como éxitos.

`data/municipalities.csv` es la relación INE 2026 oficial, con 8.132 municipios y columnas `province,municipality,name`; su fuente y checksum están en `municipalities.source.json`. Se valida cada destino contra ese fichero. Los slugs y rutas se configuran después de verificarlos en el portal: no se fabrican URLs a partir de códigos INE.

La carga prioriza alquiler antes de venta; dentro de cada operación, las seis ciudades, después destinos marcados `priority: capital`, y finalmente el resto. `initial` guarda el enlace de paginación observado y reanuda por destino; `discover` empieza por los anuncios recientes. Para ampliar, se añade un destino con portal, operación, código INE real, slug y URL confirmada; se ejecuta `health --no-delivery` y se comprueba su municipio/operación de origen antes de activar la carga. El dataset nacional permite ampliar, pero la lista inicial no equivale a cobertura nacional completada.

Los scripts manuales `tests/capture_*.py`, `reconcile_targets.py` y `finalize_targets.py` documentan las solicitudes y generación inicial de datos. No los ejecutan los workflows ni los tests ordinarios.
